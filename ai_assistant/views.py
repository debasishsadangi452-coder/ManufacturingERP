from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from groq import Groq
import logging
import json
from .tools import TOOLS_DEFINITION, TOOL_MAP
from .agents import get_agent, get_agent_tools, build_system_prompt, agents_public_list, DEFAULT_AGENT
from .agent_tools import get_digital_twin_snapshot
from .permissions import HasPremiumAIPlan
from .quota import get_company_subscription, quota_exceeded, consume_ai_message
from django.conf import settings
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)

def get_erp_state_summary(company=None):
    """Helper to gather critical data points for AI analysis (per company)."""
    from inventory.models import Stock
    from procurement.models import PurchaseOrder
    from production.models import ProductionOrder
    from maintenance.models import Equipment
    
    low_stock = Stock.objects.filter(quantity__lt=100, item__company=company)
    pending_po = PurchaseOrder.objects.filter(status='pending', vendor__company=company).count()
    running_prod = ProductionOrder.objects.filter(status='running', recipe__product__company=company).count()
    unhealthy_equip = Equipment.objects.filter(health__lt=70, line__company=company).count()
    
    summary = {
        "low_stock_items": [s.item.name for s in low_stock[:5]],
        "pending_purchase_orders": pending_po,
        "active_production_batches": running_prod,
        "equipment_requiring_maintenance": unhealthy_equip,
    }
    return summary

class InsightsView(APIView):
    permission_classes = [IsAuthenticated, HasPremiumAIPlan]

    def get(self, request):
        # Insights are informational and regenerated on a timer by the frontend;
        # cache per company so idle dashboards don't burn LLM tokens every poll.
        from django.core.cache import cache
        cache_key = f"ai_insights_v1_{getattr(request.user, 'company_id', 'none')}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached, status=status.HTTP_200_OK)

        state = get_erp_state_summary(company=request.user.company)

        # Rule-based insights: derived directly from live ERP data, no LLM
        # call. The LLM token budget is reserved exclusively for the AI chat.
        insights = []
        low = state.get("low_stock_items") or []
        if low:
            names = ", ".join(low[:3])
            insights.append({
                "title": "Inventory Below Minimum",
                "prediction": (
                    f"{len(low)} item(s) are critically low ({names}). "
                    "Production runs that depend on them may be blocked - raise purchase orders now."
                ),
                "impact": "critical",
                "confidence": 97,
            })
        unhealthy = state.get("equipment_requiring_maintenance", 0)
        if unhealthy:
            insights.append({
                "title": "Equipment Failure Risk",
                "prediction": (
                    f"{unhealthy} machine(s) report health below 70%. "
                    "Schedule predictive maintenance to avoid unplanned line downtime."
                ),
                "impact": "high",
                "confidence": 92,
            })
        pending_po = state.get("pending_purchase_orders", 0)
        if pending_po:
            insights.append({
                "title": "Purchase Orders Awaiting Approval",
                "prediction": (
                    f"{pending_po} purchase order(s) are pending approval. "
                    "Delays here extend supplier lead times and risk stock-outs."
                ),
                "impact": "medium",
                "confidence": 90,
            })
        running = state.get("active_production_batches", 0)
        if running:
            insights.append({
                "title": "Production Lines Active",
                "prediction": (
                    f"{running} production batch(es) currently running. "
                    "Ensure raw material reservations cover the full run quantities."
                ),
                "impact": "medium",
                "confidence": 88,
            })
        if not insights:
            insights.append({
                "title": "All Systems Nominal",
                "prediction": "No critical risks detected across inventory, production, procurement or maintenance.",
                "impact": "medium",
                "confidence": 95,
            })

        payload = {"insights": insights[:3], "raw_state": state}
        cache.set(cache_key, payload, timeout=300)  # 5 min per company
        return Response(payload, status=status.HTTP_200_OK)


def extract_po_from_image(client, image_data: str, config: dict) -> str | None:
    """
    Call a vision-capable model to pull purchase order details out of an image.
    Returns plain text describing what was found, or None on failure.
    """
    vision_model = config.get('VISION_MODEL', 'meta-llama/llama-4-scout-17b-16e-instruct')

    if not image_data.startswith('data:'):
        image_data = f'data:image/jpeg;base64,{image_data}'

    prompt = (
        "This image is a client purchase order document. "
        "Extract the following details exactly as shown:\n"
        "1. Customer / company name\n"
        "2. Each product ordered — name and quantity\n"
        "3. Requested delivery date (if shown)\n"
        "4. Any special notes or instructions\n\n"
        "Reply in plain text with clear labels. If a field is not visible, say 'Not shown'."
    )

    try:
        response = client.chat.completions.create(
            model=vision_model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data}},
                ],
            }],
            max_tokens=600,
        )
        return response.choices[0].message.content
    except Exception as exc:
        logger.error(f"Vision extraction failed ({vision_model}): {exc}")
        return None


class ChatView(APIView):
    """
    Stateful AI chat endpoint. Accepts full conversation history so that
    multi-turn confirmation flows work correctly.
    """
    permission_classes = [IsAuthenticated, HasPremiumAIPlan]

    def post(self, request):
        user_message = request.data.get('message', '').strip()
        # history: list of {"role": "user"|"assistant", "content": str}
        history = request.data.get('history', [])
        image_data = request.data.get('image')  # optional base64 data-URL
        # Which AI teammate is answering (plant-manager, procurement, finance, ...)
        agent_slug = request.data.get('agent') or DEFAULT_AGENT
        agent = get_agent(agent_slug)
        agent_tools_definition, agent_tool_map = get_agent_tools(agent_slug)

        if not user_message and not image_data:
            return Response({"error": "Message or image is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Monthly AI message quota (Premium AI plan)
        subscription = get_company_subscription(request.user)
        if quota_exceeded(subscription):
            return Response({
                "error": "AI quota exceeded",
                "detail": (
                    f"Your company has used all {subscription.ai_monthly_message_limit} "
                    "AI messages for this billing period. The quota resets when the "
                    "period renews."
                ),
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        config = getattr(settings, 'AI_CONFIG', {})
        api_key = config.get('GROQ_API_KEY')
        if not api_key:
            return Response({
                "response": (
                    f"⚙️ {agent['name']} is not connected to an AI model yet. "
                    "Add GROQ_API_KEY to the backend .env file and restart the server to enable the AI Team."
                ),
                "refresh": False,
                "actions": [],
                "agent": agent_slug,
                "agent_name": agent["name"],
            }, status=status.HTTP_200_OK)
        client = Groq(api_key=api_key)
        model = config.get('MODEL', "llama-3.1-8b-instant")

        # Count the message once we know a real AI call will be made
        consume_ai_message(subscription)

        # If the user uploaded an image, extract PO details first using a vision model,
        # then inject the result into the user message so the main tool-calling model
        # can present it for confirmation and create the sales order.
        if image_data:
            extracted = extract_po_from_image(client, image_data, config)
            if extracted is None:
                return Response(
                    {"error": "Could not read the image. Please try again with a clearer photo."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user_message = (
                f"The user uploaded a photo of a client purchase order. "
                f"Here is the information extracted from the image:\n\n{extracted}\n\n"
                f"Present these details clearly to the user and ask for confirmation before "
                f"creating the sales order. First look up the customer by name (use list_customers), "
                f"and look up each item by name (use list_items). "
                f"If the customer does not exist, offer to create them."
            )
        logger.info(f"AI Chat using model: {model}")

        # Persona + operating rules for the selected AI teammate
        system_prompt = build_system_prompt(agent_slug)

        messages = [{"role": "system", "content": system_prompt}]
        
        import re
        legacy_pattern = re.compile(r'<function=.*?>.*?</function>', re.DOTALL)

        # Keep only the last 3 turns of history to save tokens
        for turn in history[-3:]:
            role = turn.get('role')
            content = turn.get('content', '')
            if role in ('user', 'assistant') and content:
                # Sanitize content: remove legacy function calls from history to avoid AI confusion
                clean_content = legacy_pattern.sub('', content).strip()
                if clean_content:
                    messages.append({"role": role, "content": clean_content})

        messages.append({"role": "user", "content": user_message})

        try:
            # Track if any data-altering tool was successfully executed
            data_changed = False
            write_tools = {
                "create_item", "create_warehouse", "create_vendor", "create_customer", 
                "create_sales_order", "create_production_order", "adjust_stock", 
                "create_purchase_order", "schedule_maintenance", "create_expense_request", 
                "approve_expense_request", "update_production_status", "update_purchase_order_status",
                "delete_warehouse", "delete_item", "delete_vendor", "delete_customer",
                "record_quality_check", "create_recipe", "submit_leave_request", 
                "update_vehicle_status", "create_goods_receipt"
            }

            # Agentic loop: resolve tool calls until the AI produces a final text reply
            all_actions = []
            for _ in range(8):
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tools=agent_tools_definition,
                        tool_choice="auto",
                        max_tokens=512,
                    )
                except Exception as e:
                    error_msg = str(e)
                    if "tool call validation failed" in error_msg.lower() or "failed to call a function" in error_msg.lower():
                        logger.warning(f"Groq tool error, asking AI to retry: {error_msg}")
                        messages.append({
                            "role": "user", 
                            "content": f"SYSTEM ERROR: {error_msg}. Please correct your tool arguments, avoid mixing text with tool calls, and ONLY use tools from the provided definitions."
                        })
                        continue
                    # Log the error and raise to avoid useless fallback on small TPM models
                    logger.error(f"Groq primary model error: {e}")
                    raise e

                response_message = response.choices[0].message
                tool_calls = getattr(response_message, 'tool_calls', None)

                # No tool calls → final text answer
                if not tool_calls:
                    final_text = response_message.content or "I could not generate a response. Please try again."
                    # CLEANUP: Remove any legacy function tags if they accidentally leaked
                    final_text = re.sub(r'<function=.*?>.*?</function>', '', final_text)
                    final_text = re.sub(r'<function=.*?>', '', final_text)
                    final_text = re.sub(r'{{tool:.*?}}', '', final_text)
                    
                    return Response({
                        "response": final_text.strip(),
                        "refresh": data_changed,
                        "actions": all_actions,
                        "agent": agent_slug,
                        "agent_name": agent["name"],
                    }, status=status.HTTP_200_OK)

                # Append assistant's tool-calling turn and process each call
                messages.append(response_message)

                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_to_call = agent_tool_map.get(function_name)

                    try:
                        function_args = json.loads(tool_call.function.arguments or '{}')
                        if not isinstance(function_args, dict):
                            function_args = {}
                    except (json.JSONDecodeError, TypeError):
                        function_args = {}

                    # --- SAFETY CHECK: Human-in-the-loop for write operations ---
                    if function_name in write_tools:
                        # Check if the user's latest message is a clear confirmation
                        confirm_keywords = ['yes', 'confirm', 'proceed', 'ok', 'do it', 'go ahead', 'correct', 'shall i proceed']
                        is_confirmed = any(word in user_message.lower() for word in confirm_keywords)
                        
                        if not is_confirmed:
                            # Instead of executing, we stop and ask the user to confirm.
                            # We check if the AI already asked. If not, we force a question.
                            # Always generate a clean, JSON-free summary for write operations
                            summary_items = []
                            for k, v in function_args.items():
                                label = k.replace('_', ' ').title()
                                value = v
                                # Special handling for warehouse name lookup
                                if k == 'warehouse_id':
                                    label = "Warehouse"
                                    try:
                                        from inventory.models import Warehouse
                                        wh = Warehouse.objects.get(id=v)
                                        value = wh.name
                                    except: pass
                                summary_items.append(f"• **{label}**: {value}")
                            
                            details = "\n".join(summary_items)
                            ai_text = f"I've prepared the details for this action. Please review them:\n\n{details}\n\nShall I proceed? (Or tell me what to change)"
                            
                            # Combine any existing actions (like warehouse buttons) with confirmation buttons
                            final_actions = all_actions + [
                                {"label": "✅ Yes, proceed", "action": "confirm_suggested_action"},
                                {"label": "✏️ Edit details", "action": "edit_action_details"}
                            ]

                            return Response({
                                "response": ai_text,
                                "refresh": False,
                                "actions": final_actions,
                                "agent": agent_slug,
                                "agent_name": agent["name"],
                            }, status=status.HTTP_200_OK)
                    # ------------------------------------------------------------
                    
                    # Also detect if the AI is listing warehouses to show buttons
                    if function_name == "list_warehouses":
                        try:
                            wh_data = json.loads(function_to_call(request.user))
                            if isinstance(wh_data, list):
                                for wh in wh_data:
                                    all_actions.append({
                                        "label": f"📍 {wh['name']}",
                                        "action": "select_warehouse",
                                        "data": {"warehouseId": wh['id'], "warehouseName": wh['name']}
                                    })
                        except: pass

                    if function_to_call:
                        logger.info(f"[AI Tool] {function_name}({function_args})")
                        function_args.pop('user', None)
                        try:
                            function_response = function_to_call(request.user, **function_args)
                            # If tool returned success, mark data as changed for frontend refresh
                            if function_name in write_tools:
                                res_data = json.loads(function_response)
                                if "success" in res_data and res_data["success"]:
                                    data_changed = True
                                    
                                    # Proactively suggest next steps via buttons instead of listing tool names in text
                                    if function_name == "create_item":
                                        all_actions.append({
                                            "label": "📦 Adjust Stock", 
                                            "action": "adjust_stock_flow", 
                                            "data": {"itemId": res_data.get('item_id'), "itemName": res_data.get('name')}
                                        })
                                        all_actions.append({"label": "📊 Check Inventory Status", "action": "check_stock"})
                            # Template tools (uniform procurement) return a fixed
                            # 'template'. Relay it VERBATIM and stop — no second
                            # LLM round, so it can't be paraphrased or truncated.
                            try:
                                res_data = json.loads(function_response)
                            except (json.JSONDecodeError, TypeError):
                                res_data = {}
                            if isinstance(res_data, dict) and res_data.get("template"):
                                if function_name in ("procure_item", "receive_procurement", "add_vendor_price"):
                                    data_changed = True
                                payload = {
                                    "response": res_data["template"],
                                    "refresh": data_changed,
                                    "actions": all_actions,
                                    "agent": agent_slug,
                                    "agent_name": agent["name"],
                                }
                                # Missing vendor/price → send form metadata so the
                                # UI renders an inline fill-in form.
                                if res_data.get("form"):
                                    payload["form"] = res_data["form"]
                                return Response(payload, status=status.HTTP_200_OK)
                        except Exception as tool_err:
                            logger.error(f"[AI Tool] {function_name} crashed: {tool_err}")
                            function_response = json.dumps({
                                "error": f"Tool '{function_name}' failed: {str(tool_err)}"
                            })
                    else:
                        logger.warning(f"[AI Tool] Unknown tool: {function_name}")
                        function_response = json.dumps({"error": f"Unknown tool: {function_name}"})

                    messages.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": function_response,
                    })

            return Response({
                "response": (
                    "I ran several analysis steps but could not produce a final answer. "
                    "Please try rephrasing your question."
                )
            }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.exception("Groq Integration Error")
            return Response({
                "error": "AI Assistant error",
                "details": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class AgentListView(APIView):
    """The AI Team roster: names, outcomes and sample questions per agent."""
    permission_classes = [IsAuthenticated, HasPremiumAIPlan]

    def get(self, request):
        return Response({"agents": agents_public_list()}, status=status.HTTP_200_OK)


class DigitalTwinView(APIView):
    """One-screen factory snapshot: sales, profit, production, inventory,
    machine health, procurement and attendance."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            return Response(get_digital_twin_snapshot(request.user), status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Digital twin snapshot failed")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class QuickProcureView(APIView):
    """Deterministic (no-LLM) submit for the inline 'add vendor & coupon
    price' form: saves the vendor price, then places the purchase order and
    returns the fixed procurement template."""
    permission_classes = [IsAuthenticated, HasPremiumAIPlan]

    def post(self, request):
        from .agent_tools import add_vendor_price, procure_item
        vendor_name = (request.data.get("vendor_name") or "").strip()
        item = (request.data.get("item") or "").strip()
        unit_price = request.data.get("unit_price")
        quantity = request.data.get("quantity")
        lead_time_days = request.data.get("lead_time_days") or 7

        if not (vendor_name and item and unit_price and quantity):
            return Response(
                {"error": "vendor_name, item, unit_price and quantity are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        add_res = json.loads(add_vendor_price(request.user, vendor_name, item, unit_price, lead_time_days))
        # If saving the price failed, surface that template
        if "already" not in (add_res.get("template", "")) and "SAVED" not in add_res.get("template", ""):
            return Response({"response": add_res.get("template", "Could not save vendor price.")},
                            status=status.HTTP_200_OK)

        po_res = json.loads(procure_item(request.user, item, quantity))
        return Response({"response": po_res.get("template", "Order could not be placed."), "refresh": True},
                        status=status.HTTP_200_OK)
