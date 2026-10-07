from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permission import IsAdmin, IsFinance, IsFinanceOrAdmin, IsProduction, IsQuality, IsSales, IsStore

from . import executive, kpis, traceability


def _company(request):
    return getattr(request.user, "company", None)


class TraceabilityView(APIView):
    """TB-17: GET ?type=<sales_order|production_plan|production_order|product|lot|dispatch|invoice>&value=..."""
    permission_classes = [IsAdmin | IsSales | IsStore | IsProduction | IsQuality | IsFinance]

    def get(self, request):
        search_type = request.query_params.get("type", "sales_order")
        value = request.query_params.get("value", "").strip()
        if not value:
            return Response({"error": "Enter something to search for."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            return Response(traceability.trace(_company(request), search_type, value))
        except ValueError as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class ExecutiveDashboardView(APIView):
    """TB-16: management dashboard."""
    permission_classes = [IsAdmin | IsFinanceOrAdmin]

    def get(self, request):
        """?start=YYYY-MM-DD&end=YYYY-MM-DD (default: this month to date)."""
        from django.utils.dateparse import parse_date
        start = parse_date(request.query_params.get("start") or "") if request.query_params.get("start") else None
        end = parse_date(request.query_params.get("end") or "") if request.query_params.get("end") else None
        return Response(executive.executive_dashboard(_company(request), start, end))


class KPIDefinitionsView(APIView):
    """TB-20: the KPI formulas every dashboard uses."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        """?dashboard=<management|production_manager|order_status|wip|material_availability|fg_availability|supply_chain>"""
        return Response(kpis.definitions(request.query_params.get("dashboard")))


ACCOUNT_ROLES = [
    ("raw_material_inventory", "Raw Material Inventory", "inventory_raw_material_account", "1210"),
    ("wip_inventory", "WIP Inventory", "manufacturing_wip_account", "1220"),
    ("finished_goods_inventory", "Finished Goods Inventory", "inventory_finished_goods_account", "1230"),
    ("cogs", "Cost of Goods Sold", "inventory_cogs_account", "5050"),
    ("labour", "Direct Labour / Manpower Resource Cost", "manufacturing_labor_account", None),
    ("overhead", "Manufacturing Overhead / Machine Resource Cost", "manufacturing_overhead_account", None),
    ("scrap", "Scrap", "manufacturing_scrap_account", None),
    ("variance", "Manufacturing Variance", "manufacturing_variance_account", None),
    ("inventory_adjustment", "Inventory Adjustment", "inventory_adjustment_account", None),
]


class AccountingMappingView(APIView):
    """TB-15: which GL account each manufacturing / sales posting role uses.

    Mappings are edited in Accounting Settings; this view shows the effective
    account per role and whether it is configured or the standard default."""
    permission_classes = [IsFinanceOrAdmin]

    def get(self, request):
        from accounting.models import Account, AccountingSettings
        company = _company(request)
        settings = AccountingSettings.objects.filter(company=company).first()
        rows = []
        for key, label, field, default_code in ACCOUNT_ROLES:
            account = getattr(settings, field, None) if settings else None
            source = "configured"
            if account is None and default_code:
                account = Account.objects.filter(company=company, code=default_code).first()
                source = "default" if account else "missing"
            elif account is None:
                source = "not_configured"
            rows.append({
                "role": key, "label": label, "settings_field": field, "source": source,
                "account": {"id": account.id, "code": account.code, "name": account.name} if account else None,
            })
        for key, label, getter in (
            ("accounts_receivable", "Accounts Receivable", "get_ar_account"),
            ("sales_revenue", "Sales Revenue", "get_sales_revenue_account"),
        ):
            from accounting import receivables
            try:
                account = getattr(receivables, getter)(company)
                rows.append({"role": key, "label": label, "settings_field": None, "source": "default",
                             "account": {"id": account.id, "code": account.code, "name": account.name}})
            except Exception as e:
                rows.append({"role": key, "label": label, "settings_field": None, "source": "missing",
                             "account": None, "error": str(e)})
        return Response({
            "accounting_enabled": bool(settings),
            "auto_post_enabled": bool(settings and settings.auto_post_enabled),
            "postings": [
                {"event": "Goods receipt", "entry": "Dr Raw Material Inventory / Cr GRNI"},
                {"event": "Production (after QA decision)", "entry": "Dr WIP / Cr Raw Material, Labour, Overhead; Dr Finished Goods (+ Scrap) / Cr WIP"},
                {"event": "Dispatch confirmed", "entry": "Dr COGS / Cr Finished Goods Inventory"},
                {"event": "Invoice", "entry": "Dr Accounts Receivable / Cr Sales Revenue"},
                {"event": "Customer payment", "entry": "Dr Bank / Cr Accounts Receivable"},
            ],
            "mappings": rows,
        })
