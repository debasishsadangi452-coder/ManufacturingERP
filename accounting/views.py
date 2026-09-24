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


from .general_ledger import get_account_ledger, get_general_ledger_summary


class GeneralLedgerViewSet(viewsets.ViewSet):
    """
    Blueprint #9: General Ledger Layer.
    Derives account-level transaction ledgers, running balances, opening/closing
    balances, and company-wide General Ledger summaries directly from posted
    double-entry journal entries.
    """
    permission_classes = [IsFinanceOrAdmin]

    def list(self, request):
        """
        GET /api/accounting/general-ledger/
        If `account` query parameter is provided, returns the detailed account ledger.
        Otherwise returns the full company General Ledger summary.
        """
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        account_id = request.query_params.get("account")
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        fiscal_year_id = request.query_params.get("fiscal_year")
        accounting_period_id = request.query_params.get("accounting_period")
        status_param = request.query_params.get("status")
        search = request.query_params.get("search")
        category = request.query_params.get("category")

        try:
            if account_id:
                ledger = get_account_ledger(
                    company=company,
                    account_id=account_id,
                    start_date=start_date,
                    end_date=end_date,
                    fiscal_year_id=fiscal_year_id,
                    accounting_period_id=accounting_period_id,
                    status=status_param,
                    search=search,
                )
                return Response(ledger, status=status.HTTP_200_OK)
            else:
                summary = get_general_ledger_summary(
                    company=company,
                    fiscal_year_id=fiscal_year_id,
                    accounting_period_id=accounting_period_id,
                    start_date=start_date,
                    end_date=end_date,
                    category=category,
                    search=search,
                )
                return Response(summary, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/general-ledger/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        fiscal_year_id = request.query_params.get("fiscal_year")
        accounting_period_id = request.query_params.get("accounting_period")
        category = request.query_params.get("category")
        search = request.query_params.get("search")

        try:
            res = get_general_ledger_summary(
                company=company,
                fiscal_year_id=fiscal_year_id,
                accounting_period_id=accounting_period_id,
                start_date=start_date,
                end_date=end_date,
                category=category,
                search=search,
            )
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="ledger")
    def account_ledger(self, request, pk=None):
        """GET /api/accounting/general-ledger/{account_id}/ledger/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        fiscal_year_id = request.query_params.get("fiscal_year")
        accounting_period_id = request.query_params.get("accounting_period")
        status_param = request.query_params.get("status")
        search = request.query_params.get("search")

        try:
            res = get_account_ledger(
                company=company,
                account_id=pk,
                start_date=start_date,
                end_date=end_date,
                fiscal_year_id=fiscal_year_id,
                accounting_period_id=accounting_period_id,
                status=status_param,
                search=search,
            )
            return Response(res, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


from .receivables import (
    get_ar_summary,
    get_ar_aging_report,
    get_ar_invoices,
    post_invoice_to_ar,
    record_and_allocate_ar_payment,
    get_customer_ar_statement,
)


class AccountsReceivableViewSet(viewsets.ViewSet):
    """
    Accounts Receivable Subledger API (Blueprint Section No. 10).
    Provides customer receivables ledger, deterministic AR aging calculations,
    payment allocation, and atomic double-entry posting through the Double-Entry Engine (#8).
    """
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/receivables/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_ar_summary(company=company, as_of_date=as_of_date)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="aging")
    def aging_report(self, request):
        """GET /api/accounting/receivables/aging/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_ar_aging_report(company=company, as_of_date=as_of_date)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="invoices")
    def invoices(self, request):
        """GET /api/accounting/receivables/invoices/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        customer_id = request.query_params.get("customer_id")
        status_filter = request.query_params.get("status")
        search = request.query_params.get("search")
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_ar_invoices(
                company=company,
                customer_id=customer_id,
                status_filter=status_filter,
                search=search,
                as_of_date=as_of_date,
            )
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="post-invoice")
    def post_invoice(self, request):
        """POST /api/accounting/receivables/post-invoice/ {"invoice_id": 123}"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        invoice_id = request.data.get("invoice_id")
        if not invoice_id:
            return Response({"error": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            posted_entry = post_invoice_to_ar(invoice_id=invoice_id, user=request.user, company=company)
            return Response({
                "message": f"Invoice INV-{invoice_id} successfully posted to General Ledger.",
                "journal_entry_id": posted_entry.id,
                "journal_entry_number": posted_entry.entry_number,
                "posted_at": posted_entry.posted_at,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="record-payment")
    def record_payment(self, request):
        """
        POST /api/accounting/receivables/record-payment/
        """
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        customer_id = request.data.get("customer_id")
        amount = request.data.get("amount")
        payment_date = request.data.get("payment_date")
        method = request.data.get("method", "bank_transfer")
        reference = request.data.get("reference", "")
        allocations = request.data.get("allocations")
        invoice_id = request.data.get("invoice_id")
        bank_account_id = request.data.get("bank_account_id")

        if not allocations and invoice_id:
            allocations = [{"invoice_id": invoice_id, "amount": amount}]

        if not customer_id or not amount:
            return Response({"error": "customer_id and amount are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            res = record_and_allocate_ar_payment(
                customer_id=customer_id,
                amount=amount,
                user=request.user,
                company=company,
                payment_date=payment_date,
                method=method,
                reference=reference,
                allocations=allocations,
                bank_account_id=bank_account_id,
            )
            je = res["journal_entry"]
            return Response({
                "message": f"Payment successfully recorded, allocated, and posted to General Ledger.",
                "journal_entry_id": je.id,
                "journal_entry_number": je.entry_number,
                "payments_created": res["payments"],
                "total_allocated": res["total_allocated"],
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="customer/(?P<customer_id>[^/.]+)/statement")
    def customer_statement(self, request, customer_id=None):
        """GET /api/accounting/receivables/customer/{customer_id}/statement/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            res = get_customer_ar_statement(
                customer_id=customer_id,
                company=company,
                start_date=start_date,
                end_date=end_date,
            )
            return Response(res, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


from .payables import (
    get_ap_summary,
    get_ap_aging_report,
    get_ap_bills,
    post_bill_to_ap,
    record_and_allocate_ap_payment,
    get_vendor_ap_statement,
)


class AccountsPayableViewSet(viewsets.ViewSet):
    """
    Accounts Payable Subledger API (Blueprint Section No. 11).
    Provides vendor payables ledger, deterministic AP aging calculations,
    payment allocation, and atomic double-entry posting through the Double-Entry Engine (#8).
    """
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/payables/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_ap_summary(company=company, as_of_date=as_of_date)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="aging")
    def aging_report(self, request):
        """GET /api/accounting/payables/aging/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_ap_aging_report(company=company, as_of_date=as_of_date)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="bills")
    def bills(self, request):
        """GET /api/accounting/payables/bills/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        vendor_id = request.query_params.get("vendor_id")
        status_filter = request.query_params.get("status")
        search = request.query_params.get("search")
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_ap_bills(
                company=company,
                vendor_id=vendor_id,
                status_filter=status_filter,
                search=search,
                as_of_date=as_of_date,
            )
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="post-bill")
    def post_bill(self, request):
        """POST /api/accounting/payables/post-bill/ {"bill_id": 123, "expense_account_id": 45}"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        bill_id = request.data.get("bill_id")
        expense_account_id = request.data.get("expense_account_id")
        if not bill_id:
            return Response({"error": "bill_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            posted_entry = post_bill_to_ap(
                bill_id=bill_id,
                user=request.user,
                company=company,
                expense_account_id=expense_account_id,
            )
            return Response({
                "message": f"Bill #{bill_id} successfully posted to General Ledger.",
                "journal_entry_id": posted_entry.id,
                "journal_entry_number": posted_entry.entry_number,
                "posted_at": posted_entry.posted_at,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="record-payment")
    def record_payment(self, request):
        """
        POST /api/accounting/payables/record-payment/
        """
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        vendor_id = request.data.get("vendor_id")
        amount = request.data.get("amount")
        payment_date = request.data.get("payment_date")
        method = request.data.get("method", "bank_transfer")
        reference = request.data.get("reference", "")
        allocations = request.data.get("allocations")
        bill_id = request.data.get("bill_id")
        bank_account_id = request.data.get("bank_account_id")

        if not allocations and bill_id:
            allocations = [{"bill_id": bill_id, "amount": amount}]

        if not vendor_id or not amount:
            return Response({"error": "vendor_id and amount are required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            res = record_and_allocate_ap_payment(
                vendor_id=vendor_id,
                amount=amount,
                user=request.user,
                company=company,
                payment_date=payment_date,
                method=method,
                reference=reference,
                allocations=allocations,
                bank_account_id=bank_account_id,
            )
            je = res["journal_entry"]
            return Response({
                "message": f"Vendor payment successfully recorded, allocated, and posted to General Ledger.",
                "journal_entry_id": je.id,
                "journal_entry_number": je.entry_number,
                "payments_created": res["payments"],
                "total_allocated": res["total_allocated"],
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="vendor/(?P<vendor_id>[^/.]+)/statement")
    def vendor_statement(self, request, vendor_id=None):
        """GET /api/accounting/payables/vendor/{vendor_id}/statement/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")

        try:
            res = get_vendor_ap_statement(
                vendor_id=vendor_id,
                company=company,
                start_date=start_date,
                end_date=end_date,
            )
            return Response(res, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

from .sales_accounting import (
    get_sales_accounting_summary,
    get_sales_accounting_invoices,
    get_sales_accounting_preview,
    post_sales_invoice_to_accounting,
    reverse_sales_invoice_accounting,
)


class SalesAccountingViewSet(viewsets.ViewSet):
    """
    Sales-to-Accounting Subledger API (Blueprint Section No. 12).
    Connects operational sales invoices, line items, revenue recognition, tax accounting,
    and Accounts Receivable directly to the Double-Entry Engine (#8) and General Ledger (#9).
    """
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/sales/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_sales_accounting_summary(company=company, as_of_date=as_of_date)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="invoices")
    def invoices(self, request):
        """GET /api/accounting/sales/invoices/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        search = request.query_params.get("search")
        status_filter = request.query_params.get("status")
        posted_filter = request.query_params.get("posted")
        try:
            res = get_sales_accounting_invoices(
                company=company,
                search=search,
                status_filter=status_filter,
                posted_filter=posted_filter,
            )
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="preview")
    def preview(self, request, pk=None):
        """POST /api/accounting/sales/{id}/preview/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        revenue_account_id = request.data.get("revenue_account_id")
        tax_account_id = request.data.get("tax_account_id")
        tax_amount = request.data.get("tax_amount")
        try:
            res = get_sales_accounting_preview(
                invoice_id=pk,
                company=company,
                revenue_account_id=revenue_account_id,
                tax_account_id=tax_account_id,
                tax_amount=tax_amount,
            )
            return Response(res, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="post")
    def post_invoice(self, request, pk=None):
        """POST /api/accounting/sales/{id}/post/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        revenue_account_id = request.data.get("revenue_account_id")
        tax_account_id = request.data.get("tax_account_id")
        tax_amount = request.data.get("tax_amount")
        try:
            posted_je = post_sales_invoice_to_accounting(
                invoice_id=pk,
                user=request.user,
                company=company,
                revenue_account_id=revenue_account_id,
                tax_account_id=tax_account_id,
                tax_amount=tax_amount,
            )
            return Response({
                "message": f"Invoice INV-{pk} posted successfully to General Ledger.",
                "journal_entry_id": posted_je.id,
                "journal_entry_number": posted_je.entry_number,
                "transaction_date": posted_je.transaction_date.isoformat(),
                "status": posted_je.status,
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_invoice(self, request, pk=None):
        """POST /api/accounting/sales/{id}/reverse/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        reason = request.data.get("reason", "")
        try:
            res = reverse_sales_invoice_accounting(
                invoice_id=pk,
                user=request.user,
                company=company,
                reason=reason,
            )
            reversal_je = res.get("reversal_journal_entry")
            return Response({
                "message": f"Invoice INV-{pk} reversed and cancelled successfully.",
                "invoice_id": res["invoice_id"],
                "status": res["status"],
                "reversal_journal_entry_id": reversal_je.id if reversal_je else None,
                "reversal_journal_entry_number": reversal_je.entry_number if reversal_je else None,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)






