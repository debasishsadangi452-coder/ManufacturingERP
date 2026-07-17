from urllib.parse import urlsplit

from django.conf import settings
from django.core import signing
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from accounts.models import Company, User
from accounts.permission import IsFinanceOrAdmin

from .models import QuickBooksConnection, QuickBooksSyncRun
from .push import PUSH_HANDLERS, push_all, push_object
from .serializers import QuickBooksConnectionSerializer, QuickBooksSyncRunSerializer
from .services import (
    QuickBooksAPIError,
    QuickBooksConfigError,
    apply_token_payload,
    build_authorization_url,
    exchange_code_for_tokens,
    fetch_company_info,
    get_config,
    sync_master_data,
)


# Paths on the frontend that the OAuth callback may send the user back to.
ALLOWED_RETURN_PATHS = {"/settings", "/onboarding"}


def _frontend_redirect(return_path=""):
    frontend_redirect = settings.QUICKBOOKS_CONFIG["FRONTEND_REDIRECT_URI"]
    if return_path in ALLOWED_RETURN_PATHS:
        parts = urlsplit(frontend_redirect)
        return f"{parts.scheme}://{parts.netloc}{return_path}"
    return frontend_redirect


@api_view(["GET"])
@permission_classes([IsFinanceOrAdmin])
def connect(request):
    if not request.user.company:
        return Response({"detail": "User is not assigned to a company."}, status=status.HTTP_400_BAD_REQUEST)

    return_path = request.query_params.get("return_path", "")
    if return_path not in ALLOWED_RETURN_PATHS:
        return_path = ""

    try:
        state = signing.dumps(
            {
                "company_id": request.user.company_id,
                "user_id": request.user.id,
                "environment": get_config()["ENVIRONMENT"],
                "return_path": return_path,
            },
            salt="quickbooks-oauth-state",
        )
        return Response({
            "authorization_url": build_authorization_url(state),
            "environment": get_config()["ENVIRONMENT"],
        })
    except QuickBooksConfigError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@permission_classes([AllowAny])
def callback(request):
    code = request.query_params.get("code")
    realm_id = request.query_params.get("realmId")
    state = request.query_params.get("state")
    frontend_redirect = _frontend_redirect()

    if not code or not realm_id or not state:
        return HttpResponseRedirect(f"{frontend_redirect}?quickbooks=error")

    try:
        state_data = signing.loads(state, salt="quickbooks-oauth-state", max_age=600)
        frontend_redirect = _frontend_redirect(state_data.get("return_path", ""))
        company = Company.objects.get(id=state_data["company_id"])
        user = User.objects.filter(id=state_data["user_id"], company=company).first()
        payload = exchange_code_for_tokens(code)

        connection, _ = QuickBooksConnection.objects.get_or_create(
            company=company,
            defaults={
                "realm_id": realm_id,
                "environment": state_data.get("environment", "sandbox"),
                "connected_by": user,
            },
        )
        connection.realm_id = realm_id
        connection.environment = state_data.get("environment", "sandbox")
        connection.connected_by = user
        connection.is_active = True
        apply_token_payload(connection, payload)
        connection.save()

        try:
            fetch_company_info(connection)
        except QuickBooksAPIError:
            pass

        # Create onboarding record for this company
        from inventory.models import QuickBooksOnboarding
        QuickBooksOnboarding.objects.get_or_create(
            company=company,
            defaults={"status": "classification"}
        )

        return HttpResponseRedirect(f"{frontend_redirect}?quickbooks=connected")
    except Exception as e:
        import traceback
        print(f"QB Callback Error: {str(e)}")
        print(traceback.format_exc())
        return HttpResponseRedirect(f"{frontend_redirect}?quickbooks=error")


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def status_view(request):
    connection = QuickBooksConnection.objects.filter(company=request.user.company, is_active=True).first()
    latest_runs = QuickBooksSyncRun.objects.filter(company=request.user.company)[:5]
    return Response({
        "configured": bool(settings.QUICKBOOKS_CONFIG["CLIENT_ID"] and settings.QUICKBOOKS_CONFIG["CLIENT_SECRET"]),
        "environment": settings.QUICKBOOKS_CONFIG["ENVIRONMENT"],
        "connected": bool(connection),
        "connection": QuickBooksConnectionSerializer(connection).data if connection else None,
        "recent_sync_runs": QuickBooksSyncRunSerializer(latest_runs, many=True).data,
    })


@api_view(["POST"])
@permission_classes([IsFinanceOrAdmin])
def disconnect(request):
    connection = QuickBooksConnection.objects.filter(company=request.user.company, is_active=True).first()
    if connection:
        connection.is_active = False
        connection.save(update_fields=["is_active"])
    return Response({"connected": False})


@api_view(["POST"])
@permission_classes([IsFinanceOrAdmin])
def sync(request):
    connection = QuickBooksConnection.objects.filter(company=request.user.company, is_active=True).first()
    if not connection:
        return Response({"detail": "QuickBooks is not connected."}, status=status.HTTP_400_BAD_REQUEST)

    sync_type = request.data.get("sync_type", "all")
    if sync_type not in {"company", "customers", "vendors", "items", "sales", "estimates", "invoices", "payments", "procurement", "purchase_orders", "bills", "all"}:
        return Response(
            {"detail": f"sync_type must be one of: company, customers, vendors, items, sales, estimates, invoices, payments, procurement, purchase_orders, bills, all."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        run = sync_master_data(connection, sync_type=sync_type)
        return Response(QuickBooksSyncRunSerializer(run).data)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except QuickBooksAPIError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


def _pushable_queryset(entity_type, company):
    """Company-scoped queryset for each entity type that can be pushed."""
    from inventory.models import Item
    from procurement.models import Bill, PurchaseOrder, Vendor
    from sales.models import Customer, CustomerPayment, Invoice, SalesOrder

    querysets = {
        "customer": Customer.objects.filter(company=company),
        "vendor": Vendor.objects.filter(company=company),
        "item": Item.objects.filter(company=company),
        "item_quantity": Item.objects.filter(company=company),
        "sales_order": SalesOrder.objects.filter(customer__company=company),
        "purchase_order": PurchaseOrder.objects.filter(vendor__company=company),
        "invoice": Invoice.objects.filter(company=company),
        "bill": Bill.objects.filter(company=company),
        "payment": CustomerPayment.objects.filter(company=company),
    }
    return querysets.get(entity_type)


@api_view(["POST"])
@permission_classes([IsFinanceOrAdmin])
def push(request):
    """Manually push one ERP record to QuickBooks.

    Payload: {"entity_type": "customer", "id": 3}
    """
    connection = QuickBooksConnection.objects.filter(company=request.user.company, is_active=True).first()
    if not connection:
        return Response({"detail": "QuickBooks is not connected."}, status=status.HTTP_400_BAD_REQUEST)

    entity_type = request.data.get("entity_type", "")
    if entity_type not in PUSH_HANDLERS:
        return Response(
            {"detail": f"entity_type must be one of: {', '.join(sorted(PUSH_HANDLERS))}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    queryset = _pushable_queryset(entity_type, request.user.company)
    obj = queryset.filter(id=request.data.get("id")).first()
    if not obj:
        return Response({"detail": "Record not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        push_object(connection, entity_type, obj)
    except QuickBooksAPIError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response({
        "entity_type": entity_type,
        "id": obj.id,
        "quickbooks_id": obj.quickbooks_id,
    })


@api_view(["POST"])
@permission_classes([IsFinanceOrAdmin])
def push_all_view(request):
    """Backfill every ERP record for the company into QuickBooks."""
    connection = QuickBooksConnection.objects.filter(company=request.user.company, is_active=True).first()
    if not connection:
        return Response({"detail": "QuickBooks is not connected."}, status=status.HTTP_400_BAD_REQUEST)
    run = push_all(connection)
    return Response(QuickBooksSyncRunSerializer(run).data)
