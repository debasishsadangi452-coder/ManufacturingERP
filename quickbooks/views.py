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
ALLOWED_RETURN_PATHS = {"/settings", "/onboarding", "/accounting"}


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
    from accounting.models import JournalEntry
    from inventory.models import Item
    from procurement.models import Bill, PurchaseOrder, Vendor, VendorPayment
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
        "bill_payment": VendorPayment.objects.filter(company=company),
        "journal_entry": JournalEntry.objects.filter(company=company),
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


# ---------------------------------------------------------------------------
# Accounting > QuickBooks tab
# ---------------------------------------------------------------------------

def _active_connection(request):
    return QuickBooksConnection.objects.filter(company=request.user.company, is_active=True).first()


def _sync_counts(company):
    """Per record type: how many exist in the ERP and how many reached QuickBooks."""
    from django.db.models import Q
    from accounting.models import JournalEntry
    from inventory.models import Item
    from procurement.models import Bill, PurchaseOrder, Vendor, VendorPayment
    from sales.models import Customer, CustomerPayment, Invoice, SalesOrder
    from .push import QB_JOURNAL_SOURCES

    journal_to_qb = Q(source_module__in=QB_JOURNAL_SOURCES) | Q(
        source_module="reversal", reversal_of__source_module__in=QB_JOURNAL_SOURCES
    )
    posted_journals = JournalEntry.objects.filter(company=company, status__in=["posted", "reversed"])
    groups = [
        ("customer", "Customers", Customer.objects.filter(company=company)),
        ("vendor", "Vendors", Vendor.objects.filter(company=company)),
        ("item", "Items", Item.objects.filter(company=company)),
        ("sales_order", "Sales orders (as Estimates)",
         SalesOrder.objects.filter(customer__company=company).exclude(status__in=["draft", "cancelled"])),
        ("purchase_order", "Purchase orders",
         PurchaseOrder.objects.filter(vendor__company=company, items__isnull=False)
         .exclude(status__in=["draft", "cancelled"]).distinct()),
        ("invoice", "Invoices", Invoice.objects.filter(company=company).exclude(status="cancelled")),
        ("bill", "Vendor bills", Bill.objects.filter(company=company).exclude(status="cancelled")),
        ("payment", "Customer payments", CustomerPayment.objects.filter(company=company)),
        ("bill_payment", "Bill payments", VendorPayment.objects.filter(company=company, bill__isnull=False)),
        ("journal_entry", "Journal entries (ERP-only)", posted_journals.filter(journal_to_qb)),
    ]
    rows = []
    for key, label, queryset in groups:
        total = queryset.count()
        sent = queryset.exclude(quickbooks_id="").count()
        rows.append({"entity_type": key, "label": label, "total": total, "sent": sent, "pending": total - sent})
    local_only = posted_journals.exclude(journal_to_qb).count()
    return rows, local_only


@api_view(["GET"])
@permission_classes([IsFinanceOrAdmin])
def overview(request):
    """Everything the Accounting > QuickBooks tab shows, in one call."""
    from .models import QuickBooksSyncError
    from .serializers import QuickBooksSyncErrorSerializer

    company = request.user.company
    connection = _active_connection(request)
    rows, local_only = _sync_counts(company)
    runs = QuickBooksSyncRun.objects.filter(company=company).order_by("-started_at")[:10]
    errors = QuickBooksSyncError.objects.filter(company=company).order_by("-created_at")[:30]
    return Response({
        "configured": bool(settings.QUICKBOOKS_CONFIG["CLIENT_ID"] and settings.QUICKBOOKS_CONFIG["CLIENT_SECRET"]),
        "environment": settings.QUICKBOOKS_CONFIG["ENVIRONMENT"],
        "connected": bool(connection),
        "connection": QuickBooksConnectionSerializer(connection).data if connection else None,
        "entities": rows,
        "local_only_journal_entries": local_only,
        "recent_sync_runs": QuickBooksSyncRunSerializer(runs, many=True).data,
        "errors": QuickBooksSyncErrorSerializer(errors, many=True).data,
        "error_count": QuickBooksSyncError.objects.filter(company=company).count(),
    })


@api_view(["GET"])
@permission_classes([IsFinanceOrAdmin])
def quickbooks_accounts(request):
    """QuickBooks chart of accounts, for the account-mapping dropdowns."""
    from .push import list_quickbooks_accounts

    connection = _active_connection(request)
    if not connection:
        return Response({"detail": "QuickBooks is not connected."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        accounts = list_quickbooks_accounts(connection)
    except QuickBooksAPIError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    return Response([
        {"id": str(a.get("Id")), "name": a.get("FullyQualifiedName") or a.get("Name", ""), "type": a.get("AccountType", "")}
        for a in sorted(accounts, key=lambda a: (a.get("AccountType", ""), a.get("Name", "")))
    ])


@api_view(["GET", "POST"])
@permission_classes([IsFinanceOrAdmin])
def account_mappings(request):
    """GET: every ERP account with its QuickBooks mapping.
    POST {"account", "quickbooks_account_id", "quickbooks_account_name", "quickbooks_account_type"}
    sets a mapping; an empty quickbooks_account_id clears it."""
    from accounting.models import Account
    from .models import QuickBooksAccountMapping

    company = request.user.company
    if request.method == "POST":
        account = Account.objects.filter(company=company, pk=request.data.get("account")).first()
        if not account:
            return Response({"detail": "Account not found."}, status=status.HTTP_404_NOT_FOUND)
        qb_id = str(request.data.get("quickbooks_account_id") or "").strip()
        if not qb_id:
            QuickBooksAccountMapping.objects.filter(account=account).delete()
        else:
            QuickBooksAccountMapping.objects.update_or_create(
                account=account,
                defaults={
                    "company": company,
                    "quickbooks_account_id": qb_id,
                    "quickbooks_account_name": request.data.get("quickbooks_account_name", ""),
                    "quickbooks_account_type": request.data.get("quickbooks_account_type", ""),
                    "auto_mapped": False,
                },
            )
    mappings = {m.account_id: m for m in QuickBooksAccountMapping.objects.filter(company=company)}
    rows = []
    for account in Account.objects.filter(company=company).select_related("account_type").order_by("code"):
        m = mappings.get(account.id)
        rows.append({
            "account": account.id,
            "code": account.code,
            "name": account.name,
            "type": account.account_type.name if account.account_type_id else "",
            "is_header": account.is_header,
            "quickbooks_account_id": m.quickbooks_account_id if m else "",
            "quickbooks_account_name": m.quickbooks_account_name if m else "",
            "auto_mapped": m.auto_mapped if m else False,
        })
    return Response(rows)


@api_view(["POST"])
@permission_classes([IsFinanceOrAdmin])
def auto_map_accounts(request):
    """Map every unmapped ERP ledger (leaf) account automatically."""
    from accounting.models import Account
    from .push import auto_map_account, list_quickbooks_accounts

    connection = _active_connection(request)
    if not connection:
        return Response({"detail": "QuickBooks is not connected."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        qb_accounts = list_quickbooks_accounts(connection)
    except QuickBooksAPIError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
    mapped = unmapped = 0
    for account in Account.objects.filter(company=request.user.company, is_active=True).select_related("account_type"):
        if account.is_header:
            continue
        if auto_map_account(connection, account, qb_accounts):
            mapped += 1
        else:
            unmapped += 1
    return Response({"mapped": mapped, "unmapped": unmapped})


@api_view(["POST"])
@permission_classes([IsFinanceOrAdmin])
def retry_errors(request):
    """Retry every failed push (and failed deletion) recorded for the company."""
    from .models import QuickBooksSyncError
    from .push import safe_delete, safe_push

    connection = _active_connection(request)
    if not connection:
        return Response({"detail": "QuickBooks is not connected."}, status=status.HTTP_400_BAD_REQUEST)
    company = request.user.company
    fixed = failed = skipped = 0
    for error in list(QuickBooksSyncError.objects.filter(company=company).order_by("created_at")):
        payload = error.payload or {}
        if payload.get("operation") == "delete":
            error.delete()
            ok = safe_delete(connection, error.entity_type, payload.get("resource", ""), error.quickbooks_id,
                             local_id=payload.get("local_id"))
        else:
            queryset = _pushable_queryset(error.entity_type, company)
            obj = queryset.filter(pk=payload.get("local_id")).first() if queryset is not None else None
            error.delete()
            if obj is None:
                skipped += 1
                continue
            ok = safe_push(connection, error.entity_type, obj)
        if ok:
            fixed += 1
        else:
            failed += 1
    return Response({"fixed": fixed, "failed": failed, "skipped": skipped})
