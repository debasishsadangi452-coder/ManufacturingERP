from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.filters import SearchFilter, OrderingFilter
from datetime import date, timedelta
import calendar

from core.tenancy import CompanyScopedMixin
from accounts.permission import IsFinanceOrAdmin
from .models import FiscalYear, AccountingPeriod, AccountType, Account, AccountingSettings
from .serializers import (
    FiscalYearSerializer,
    AccountingPeriodSerializer,
    AccountTypeSerializer,
    AccountSerializer,
    AccountTreeSerializer,
    AccountingSettingsSerializer,
)
from .seeds import (
    ensure_account_types,
    seed_standard_chart_of_accounts,
    seed_standard_fiscal_year,
)


class FiscalYearViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """CRUD operations for Fiscal Years."""
    company_field = "company"
    queryset = FiscalYear.objects.prefetch_related("periods").all()
    serializer_class = FiscalYearSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ["is_closed"]
    ordering_fields = ["start_date", "end_date", "name"]
    ordering = ["-start_date"]

    @action(detail=True, methods=["post"])
    def generate_periods(self, request, pk=None):
        """Generates standard monthly periods for this fiscal year."""
        fy = self.get_object()
        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        created = 0
        curr = fy.start_date
        period_num = 1

        while curr <= fy.end_date and period_num <= 12:
            last_day = calendar.monthrange(curr.year, curr.month)[1]
            p_end = min(date(curr.year, curr.month, last_day), fy.end_date)
            p_name = f"{month_names[curr.month - 1]} {curr.year}"
            
            _, was_created = AccountingPeriod.objects.get_or_create(
                company=fy.company,
                fiscal_year=fy,
                period_number=period_num,
                defaults={
                    "name": p_name,
                    "start_date": curr,
                    "end_date": p_end,
                    "status": "open",
                }
            )
            if was_created:
                created += 1
            curr = p_end + timedelta(days=1)
            period_num += 1

        return Response({
            "status": "success",
            "message": f"Generated {created} periods for fiscal year {fy.name}.",
            "total_periods": AccountingPeriod.objects.filter(fiscal_year=fy).count()
        })

    @action(detail=True, methods=["post"])
    def close_year(self, request, pk=None):
        """Finalizes and closes the fiscal year."""
        fy = self.get_object()
        fy.is_closed = True
        fy.save()
        # Optionally close all periods
        fy.periods.update(status="closed")
        return Response({"status": "success", "message": f"Fiscal year {fy.name} is now closed."})

    @action(detail=True, methods=["post"])
    def reopen_year(self, request, pk=None):
        """Reopens a closed fiscal year."""
        fy = self.get_object()
        fy.is_closed = False
        fy.save()
        return Response({"status": "success", "message": f"Fiscal year {fy.name} has been reopened."})


class AccountingPeriodViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """CRUD and status lifecycle for Accounting Periods."""
    company_field = "company"
    queryset = AccountingPeriod.objects.select_related("fiscal_year").all()
    serializer_class = AccountingPeriodSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ["fiscal_year", "status", "period_number"]
    ordering_fields = ["start_date", "period_number"]
    ordering = ["fiscal_year__start_date", "period_number"]

    @action(detail=True, methods=["post"])
    def lock(self, request, pk=None):
        period = self.get_object()
        period.status = "locked"
        period.save()
        return Response({"status": "success", "message": f"Period {period.name} locked."})

    @action(detail=True, methods=["post"])
    def unlock(self, request, pk=None):
        period = self.get_object()
        period.status = "open"
        period.save()
        return Response({"status": "success", "message": f"Period {period.name} unlocked."})

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        period = self.get_object()
        period.status = "closed"
        period.save()
        return Response({"status": "success", "message": f"Period {period.name} closed."})


class AccountTypeViewSet(viewsets.ReadOnlyModelViewSet):
    """System account types categorized into 5 fundamental accounting classes."""
    queryset = AccountType.objects.all()
    serializer_class = AccountTypeSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, SearchFilter]
    filterset_fields = ["category", "normal_balance"]
    search_fields = ["name", "code_prefix", "description"]

    @action(detail=False, methods=["post"], permission_classes=[IsFinanceOrAdmin])
    def seed_system_types(self, request):
        types = ensure_account_types()
        return Response({"status": "success", "message": f"Ensured {len(types)} system account types."})


class AccountViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Chart of Accounts CRUD, hierarchy tree, and seeding."""
    company_field = "company"
    queryset = Account.objects.select_related("account_type", "parent").all()
    serializer_class = AccountSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["account_type", "account_type__category", "is_active", "parent"]
    search_fields = ["code", "name", "description"]
    ordering_fields = ["code", "name", "created_at"]
    ordering = ["code"]

    @action(detail=False, methods=["get"])
    def tree(self, request):
        """Returns the Chart of Accounts in a nested tree structure (root accounts first)."""
        roots = self.get_queryset().filter(parent__isnull=True).order_by("code")
        serializer = AccountTreeSerializer(roots, many=True, context={"request": request})
        return Response(serializer.data)

    @action(detail=False, methods=["post"])
    def seed_standard_coa(self, request):
        """Seeds standard manufacturing chart of accounts for current company."""
        company = request.user.company
        if not company:
            return Response(
                {"error": "User does not belong to a registered company."},
                status=status.HTTP_400_BAD_REQUEST
            )
        created_count = seed_standard_chart_of_accounts(company)
        return Response({
            "status": "success",
            "message": f"Successfully seeded {created_count} standard manufacturing accounts.",
            "total_accounts": Account.objects.filter(company=company).count()
        })


class AccountingSettingsViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """Company Accounting Settings."""
    company_field = "company"
    queryset = AccountingSettings.objects.select_related(
        "current_fiscal_year", "retained_earnings_account"
    ).all()
    serializer_class = AccountingSettingsSerializer
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["get", "post"])
    def current(self, request):
        """Retrieve or update accounting settings for current company."""
        company = request.user.company
        if not company:
            return Response({"error": "No company associated with user"}, status=status.HTTP_400_BAD_REQUEST)

        settings_obj, _ = AccountingSettings.objects.get_or_create(
            company=company,
            defaults={"default_currency": "USD"}
        )

        if request.method == "POST":
            serializer = self.get_serializer(settings_obj, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response(serializer.data)

        serializer = self.get_serializer(settings_obj)
        return Response(serializer.data)


class AccountingFoundationSummaryViewSet(viewsets.ViewSet):
    """Dashboard KPIs for Accounting Foundation."""
    permission_classes = [IsFinanceOrAdmin]

    def list(self, request):
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "No company associated with user"}, status=status.HTTP_400_BAD_REQUEST)

        total_accounts = Account.objects.filter(company=company).count()
        active_accounts = Account.objects.filter(company=company, is_active=True).count()

        # Breakdown by category
        categories = ["asset", "liability", "equity", "revenue", "expense"]
        category_counts = {}
        for cat in categories:
            category_counts[cat] = Account.objects.filter(
                company=company,
                account_type__category=cat,
                is_active=True
            ).count()

        settings_obj = AccountingSettings.objects.filter(company=company).first()
        current_fy = settings_obj.current_fiscal_year if settings_obj else None
        if not current_fy:
            today = date.today()
            current_fy = FiscalYear.objects.filter(
                company=company,
                start_date__lte=today,
                end_date__gte=today,
                is_closed=False
            ).first()

        current_period = None
        if current_fy:
            today = date.today()
            current_period = AccountingPeriod.objects.filter(
                company=company,
                fiscal_year=current_fy,
                start_date__lte=today,
                end_date__gte=today
            ).first()

        total_fiscal_years = FiscalYear.objects.filter(company=company).count()
        total_periods = AccountingPeriod.objects.filter(company=company).count()

        return Response({
            "total_accounts": total_accounts,
            "active_accounts": active_accounts,
            "category_counts": category_counts,
            "total_fiscal_years": total_fiscal_years,
            "total_periods": total_periods,
            "current_fiscal_year": {
                "id": current_fy.id,
                "name": current_fy.name,
                "start_date": current_fy.start_date,
                "end_date": current_fy.end_date,
                "is_closed": current_fy.is_closed,
            } if current_fy else None,
            "current_period": {
                "id": current_period.id,
                "name": current_period.name,
                "period_number": current_period.period_number,
                "status": current_period.status,
            } if current_period else None,
            "default_currency": settings_obj.default_currency if settings_obj else "USD",
            "lock_date": settings_obj.lock_date if settings_obj else None,
            "setup_complete": total_accounts > 0 and total_fiscal_years > 0,
        })


from .erp_context import get_company_erp_context


class ERPContextViewSet(viewsets.ViewSet):
    """Audits and provides live integration context linking operational ERP modules to Accounting."""
    permission_classes = [IsFinanceOrAdmin]

    def list(self, request):
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "No company associated with user"}, status=status.HTTP_400_BAD_REQUEST)
        context_data = get_company_erp_context(company)
        return Response(context_data)


from .models import JournalEntry, JournalEntryLine
from .serializers import JournalEntrySerializer, JournalEntryLineSerializer
from .engine import post_journal_entry, reverse_journal_entry
from django.core.exceptions import ValidationError as DjangoValidationError


class JournalEntryViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Manages double-entry Journal Entries.
    Supports draft creation, line manipulation, atomic posting, and reversals.
    """
    queryset = JournalEntry.objects.all().prefetch_related("lines__account")
    serializer_class = JournalEntrySerializer
    permission_classes = [IsFinanceOrAdmin]

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)

        period_param = self.request.query_params.get("accounting_period")
        if period_param:
            qs = qs.filter(accounting_period_id=period_param)

        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                models.Q(entry_number__icontains=search)
                | models.Q(reference__icontains=search)
                | models.Q(description__icontains=search)
            )

        start_date = self.request.query_params.get("start_date")
        if start_date:
            qs = qs.filter(transaction_date__gte=start_date)

        end_date = self.request.query_params.get("end_date")
        if end_date:
            qs = qs.filter(transaction_date__lte=end_date)

        return qs

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()
        if instance.status != "draft":
            return Response(
                {"error": f"Cannot delete a journal entry with status '{instance.status}'. Only draft entries may be deleted."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="post")
    def post_entry(self, request, pk=None):
        """Atomically post a draft journal entry."""
        entry = self.get_object()
        company = getattr(request.user, "company", None)
        try:
            posted_entry = post_journal_entry(entry.id, request.user, company=company)
            serializer = self.get_serializer(posted_entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_entry(self, request, pk=None):
        """Create an atomic reversal of a posted journal entry."""
        entry = self.get_object()
        company = getattr(request.user, "company", None)
        reason = request.data.get("reason", "")
        reversal_date = request.data.get("transaction_date")
        try:
            reversal_entry = reverse_journal_entry(
                entry.id, request.user, reason=reason, reversal_date=reversal_date, company=company
            )
            serializer = self.get_serializer(reversal_entry)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="validate-balance")
    def validate_balance(self, request, pk=None):
        """Checks if the draft entry is mathematically balanced and ready for posting."""
        entry = self.get_object()
        try:
            entry.validate_double_entry()
            return Response({
                "is_balanced": True,
                "total_debit": entry.total_debit,
                "total_credit": entry.total_credit,
                "difference": Decimal("0.00"),
                "status": "ready_to_post",
            })
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            diff = abs(entry.total_debit - entry.total_credit)
            return Response({
                "is_balanced": False,
                "total_debit": entry.total_debit,
                "total_credit": entry.total_credit,
                "difference": diff,
                "errors": msg,
            }, status=status.HTTP_200_OK)


