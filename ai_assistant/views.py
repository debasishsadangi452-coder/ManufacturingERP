from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from groq import Groq
import logging
import json
from .tools import TOOLS_DEFINITION, TOOL_MAP
from django.conf import settings
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)

def get_erp_state_summary():
    """Helper to gather critical data points for AI analysis."""
    from inventory.models import Stock
    from procurement.models import PurchaseOrder
    from production.models import ProductionOrder
    from maintenance.models import Equipment
    
    low_stock = Stock.objects.filter(quantity__lt=100)
    pending_po = PurchaseOrder.objects.filter(status='pending').count()
    running_prod = ProductionOrder.objects.filter(status='running').count()
    unhealthy_equip = Equipment.objects.filter(health__lt=70).count()
    
    summary = {
        "low_stock_items": [s.item.name for s in low_stock[:5]],
        "pending_purchase_orders": pending_po,
        "active_production_batches": running_prod,
        "equipment_requiring_maintenance": unhealthy_equip,
    }
    return summary

class InsightsView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        state = get_erp_state_summary()

        config = getattr(settings, 'AI_CONFIG', {})
        api_key = config.get('GROQ_API_KEY')
        if not api_key:
            return Response({
                "insights": [{"title": "AI Not Configured", "prediction": "Add GROQ_API_KEY to your .env to enable AI insights.", "impact": "medium", "confidence": 99}],
                "raw_state": state
            }, status=status.HTTP_200_OK)

        client = Groq(api_key=api_key)

        prompt = f"""
        Given the following state of a Manufacturing ERP, provide 3 short, actionable AI insights or predictions.
        Focus on automation and risk mitigation.
        
        ERP STATE:
        {json.dumps(state)}
        
        FORMAT: Return a JSON list of objects with fields 'title', 'prediction', 'impact' (critical/high/medium), and 'confidence' (70-99).
        """
        
        model = config.get('MODEL', "llama-3.1-8b-instant")
        logger.info(f"AI Insights using model: {model}")
        try:
            # We try JSON mode if the model might support it, but we prepare for failure
            try:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                )
            except Exception as json_mode_err:
                logger.warning(f"JSON mode failed for {model}: {json_mode_err}. Retrying without JSON mode.")
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                )
            
            content = response.choices[0].message.content
            
            # Robust JSON parsing: AI sometimes wraps in ```json ... ```
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                content = json_match.group(0)
                
            insights = json.loads(content).get('insights', [])
            
            return Response({
                "insights": insights,
                "raw_state": state
            }, status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"AI Insights total failure: {e}")
            # Fallback dummy insights so the UI doesn't break with a 500
            return Response({
                "insights": [
                    {
                        "title": "Data Syncing",
                        "prediction": "AI insights are currently updating. Please check back in a few moments.",
                        "impact": "medium",
                        "confidence": 99
                    }
                ],
                "raw_state": state
            }, status=status.HTTP_200_OK)


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
    permission_classes = [IsAuthenticated]

    def post(self, request):
        user_message = request.data.get('message', '').strip()
        # history: list of {"role": "user"|"assistant", "content": str}
        history = request.data.get('history', [])
        image_data = request.data.get('image')  # optional base64 data-URL

        if not user_message and not image_data:
            return Response({"error": "Message or image is required"}, status=status.HTTP_400_BAD_REQUEST)

        config = getattr(settings, 'AI_CONFIG', {})
        client = Groq(api_key=config.get('GROQ_API_KEY'))
        model = config.get('MODEL', "llama-3.1-8b-instant")

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

        system_prompt = (
            "You are the AI brain of a brewery operations platform called Brewmaster. You have full access to every department of the business — inventory, production, procurement, quality, sales, workforce, logistics, finance, and maintenance.\n\n"
            "Your job is simple: the user tells you what they want done in plain English, and you figure out the right API calls to make it happen.\n\n"
            "HOW YOU WORK\n"
            "- Read the user's request and understand the business intent behind it\n"
            "- Decide which department(s) are involved and which API actions are needed\n"
            "- Perform tasks STEP BY STEP. Do not try to execute complex multi-stage tasks in a single turn. Chain them logically.\n"
            "- Execute those API calls in the right order, handling dependencies (e.g. create an order before adding items to it)\n"
            "- Summarize what you did and what the result was in plain language\n"
            "- If something fails, tell the user clearly what went wrong and what they can try next\n\n"
            "WHAT YOU KNOW ABOUT THE BUSINESS\n"
            "This is a brewery. Users include managers, floor supervisors, HR staff, finance teams, and warehouse workers. They are not technical. They speak in business terms, not API terms. \"Reorder hops\" means procurement. \"Where is my delivery?\" means logistics. \"Who is working tonight?\" means workforce. Map business language to the right actions.\n\n"
            "HOW TO HANDLE API CALLS\n"
            "- NATIVE TOOL CALLING ONLY: You MUST use the provided tool-calling interface for all actions. Never type tags like '<function=...>' or '{{tool:...}}' in your response.\n"
            "- NO HALLUCINATIONS: ONLY use the tools provided in your tools definition. If a tool doesn't exist, tell the user you can't do it.\n"
            "- NO MIXED CONTENT: Never mix conversational text and tool calls in the same turn.\n"
            "- TOKEN EFFICIENCY: Keep reasoning brief. Summarize large data.\n"
            "- Always prefer doing over asking — if you have enough information, act\n"
            "- If a request is ambiguous, ask one clarifying question, not five\n"
            "- If an API requires multiple things (arguments) and you don't have them all, give the user an option to fill them by options or by typing.\n"
            "- Chain calls when needed (e.g. fetch warehouse ID first, then check stock)\n"
            "- If a call returns an error, interpret it in human terms\n\n"
            "WHAT YOU NEVER DO\n"
            "- Never expose raw API responses or technical error codes to the user\n"
            "- Never make destructive actions (delete, deactivate) without confirming first\n"
            "- Never guess IDs — always look them up via the appropriate GET call first\n"
            "- Never tell the user \"I cannot do that\" if the API supports it — figure it out\n\n"
            "YOUR PERSONALITY\n"
            "You are calm, direct, and efficient. You speak like a trusted operations manager, not a chatbot. No filler phrases. No excessive confirmations. Just get things done and report back clearly."
        )

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
                        tools=TOOLS_DEFINITION,
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
                        "actions": all_actions
                    }, status=status.HTTP_200_OK)

                # Append assistant's tool-calling turn and process each call
                messages.append(response_message)

                for tool_call in tool_calls:
                    function_name = tool_call.function.name
                    function_to_call = TOOL_MAP.get(function_name)

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
                                "actions": final_actions
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
