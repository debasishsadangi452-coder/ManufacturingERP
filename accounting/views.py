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


from .models import JournalEntry, JournalEntryLine, JournalEntryAttachment, JournalEntryAuditLog
from .serializers import (
    JournalEntrySerializer,
    JournalEntryLineSerializer,
    JournalEntryAttachmentSerializer,
    JournalEntryAuditLogSerializer,
)
from .engine import post_journal_entry, reverse_journal_entry, record_journal_audit_log
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser


class JournalEntryViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Manages double-entry Journal Entries (Blueprint #8 & #20).
    Supports draft creation, line manipulation, approval workflow (submit, approve, reject),
    atomic posting, attachments, audit logs, and reversals.
    """
    queryset = JournalEntry.objects.all().prefetch_related(
        "lines__account",
        "attachments__uploaded_by",
        "audit_logs__performed_by",
    )
    serializer_class = JournalEntrySerializer
    permission_classes = [IsFinanceOrAdmin]

    def get_queryset(self):
        qs = super().get_queryset()
        status_param = self.request.query_params.get("status")
        if status_param and status_param != "all":
            qs = qs.filter(status=status_param)

        entry_type_param = self.request.query_params.get("entry_type")
        if entry_type_param and entry_type_param != "all":
            qs = qs.filter(entry_type=entry_type_param)

        source_module_param = self.request.query_params.get("source_module")
        if source_module_param and source_module_param != "all":
            qs = qs.filter(source_module=source_module_param)

        period_param = self.request.query_params.get("accounting_period")
        if period_param:
            qs = qs.filter(accounting_period_id=period_param)

        search = self.request.query_params.get("search")
        if search:
            qs = qs.filter(
                models.Q(entry_number__icontains=search)
                | models.Q(reference__icontains=search)
                | models.Q(description__icontains=search)
                | models.Q(explanation__icontains=search)
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
        if instance.status not in ["draft", "rejected"]:
            return Response(
                {"error": f"Cannot delete a journal entry with status '{instance.status}'. Only draft or rejected entries may be deleted."},
                status=status.HTTP_400_BAD_REQUEST
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit_entry(self, request, pk=None):
        """Submit a draft journal entry for approval."""
        entry = self.get_object()
        try:
            entry.submit_for_approval(user=request.user)
            serializer = self.get_serializer(entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve_entry(self, request, pk=None):
        """Approve a submitted journal entry."""
        entry = self.get_object()
        try:
            entry.approve_entry(user=request.user)
            serializer = self.get_serializer(entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject_entry(self, request, pk=None):
        """Reject a submitted journal entry with reason."""
        entry = self.get_object()
        reason = request.data.get("reason", "")
        try:
            entry.reject_entry(user=request.user, reason=reason)
            serializer = self.get_serializer(entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="revert-draft")
    def revert_to_draft(self, request, pk=None):
        """Revert a submitted or rejected journal entry back to draft."""
        entry = self.get_object()
        try:
            entry.revert_to_draft(user=request.user)
            serializer = self.get_serializer(entry)
            return Response(serializer.data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="post")
    def post_entry(self, request, pk=None):
        """Atomically post a draft or approved journal entry."""
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

    @action(detail=True, methods=["post"], url_path="upload-attachment", parser_classes=[MultiPartParser, FormParser])
    def upload_attachment(self, request, pk=None):
        """Uploads a supporting document to the journal entry."""
        entry = self.get_object()
        uploaded_file = request.FILES.get("file")
        if not uploaded_file:
            return Response({"error": "No file uploaded."}, status=status.HTTP_400_BAD_REQUEST)

        description = request.data.get("description", "")
        attachment = JournalEntryAttachment.objects.create(
            company=entry.company,
            journal_entry=entry,
            file=uploaded_file,
            filename=uploaded_file.name,
            file_size=uploaded_file.size,
            file_type=uploaded_file.content_type or "",
            description=description,
            uploaded_by=request.user,
        )

        record_journal_audit_log(
            entry,
            action="ATTACHMENT_ADDED",
            user=request.user,
            details={
                "attachment_id": attachment.id,
                "filename": attachment.filename,
                "file_size": attachment.file_size,
            }
        )

        serializer = JournalEntryAttachmentSerializer(attachment)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["delete"], url_path=r"attachments/(?P<attachment_id>\d+)")
    def delete_attachment(self, request, pk=None, attachment_id=None):
        """Deletes a supporting document attachment from the journal entry."""
        entry = self.get_object()
        try:
            attachment = entry.attachments.get(id=attachment_id, company=entry.company)
            filename = attachment.filename
            attachment.delete()
            record_journal_audit_log(
                entry,
                action="ATTACHMENT_REMOVED",
                user=request.user,
                details={"attachment_id": int(attachment_id), "filename": filename}
            )
            return Response({"message": "Attachment deleted successfully."}, status=status.HTTP_204_NO_CONTENT)
        except JournalEntryAttachment.DoesNotExist:
            return Response({"error": "Attachment not found."}, status=status.HTTP_404_NOT_FOUND)

    @action(detail=True, methods=["get"], url_path="audit-trail")
    def audit_trail(self, request, pk=None):
        """Returns the immutable chronological audit log for the journal entry."""
        entry = self.get_object()
        logs = entry.audit_logs.all().order_by("-timestamp")
        serializer = JournalEntryAuditLogSerializer(logs, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


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


from .purchase_accounting import (
    get_purchase_accounting_summary,
    get_purchase_accounting_bills,
    get_purchase_accounting_preview,
    post_purchase_to_accounting,
    reverse_purchase_accounting,
)


class PurchaseAccountingViewSet(viewsets.ViewSet):
    """
    Purchase-to-Accounting Subledger API (Blueprint Section No. 13).
    Connects operational procurement vendor bills, line items, material/expense allocation,
    input tax accounting, and Accounts Payable directly to the Double-Entry Engine (#8) and General Ledger (#9).
    """
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/purchases/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_purchase_accounting_summary(company=company, as_of_date=as_of_date)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="bills")
    def bills(self, request):
        """GET /api/accounting/purchases/bills/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        search = request.query_params.get("search")
        status_filter = request.query_params.get("status")
        posted_filter = request.query_params.get("posted")
        try:
            res = get_purchase_accounting_bills(
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
        """POST /api/accounting/purchases/{id}/preview/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        expense_account_id = request.data.get("expense_account_id")
        tax_account_id = request.data.get("tax_account_id")
        tax_amount = request.data.get("tax_amount")
        try:
            res = get_purchase_accounting_preview(
                bill_id=pk,
                company=company,
                expense_account_id=expense_account_id,
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
    def post_bill(self, request, pk=None):
        """POST /api/accounting/purchases/{id}/post/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        expense_account_id = request.data.get("expense_account_id")
        tax_account_id = request.data.get("tax_account_id")
        tax_amount = request.data.get("tax_amount")
        try:
            posted_je = post_purchase_to_accounting(
                bill_id=pk,
                user=request.user,
                company=company,
                expense_account_id=expense_account_id,
                tax_account_id=tax_account_id,
                tax_amount=tax_amount,
            )
            return Response({
                "message": f"Bill #{pk} posted successfully to General Ledger.",
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
    def reverse_bill(self, request, pk=None):
        """POST /api/accounting/purchases/{id}/reverse/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        reason = request.data.get("reason", "")
        try:
            res = reverse_purchase_accounting(
                bill_id=pk,
                user=request.user,
                company=company,
                reason=reason,
            )
            reversal_je = res.get("reversal_journal_entry")
            return Response({
                "message": f"Bill #{pk} reversed and cancelled successfully.",
                "bill_id": res["bill_id"],
                "status": res["status"],
                "reversal_journal_entry_id": reversal_je.id if reversal_je else None,
                "reversal_journal_entry_number": reversal_je.entry_number if reversal_je else None,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


from .inventory_accounting import (
    get_inventory_policy,
    resolve_inventory_asset_account,
    resolve_inventory_clearing_account,
    resolve_inventory_cogs_account,
    resolve_inventory_adjustment_account,
    resolve_inventory_write_off_account,
    get_inventory_accounting_preview,
    post_inventory_movement_to_accounting,
    reverse_inventory_accounting,
    post_inventory_valuation_event,
    get_inventory_accounting_summary,
    get_inventory_accounting_movements,
)


class InventoryAccountingViewSet(viewsets.ViewSet):
    """
    Inventory-to-Accounting Subledger API (Blueprint Section No. 14).
    Connects operational inventory movements (receipt, issue, transfer, adjustment,
    write-off, and revaluation) directly to the Double-Entry Engine (#8) and General Ledger (#9).
    """
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/inventory/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_date = request.query_params.get("as_of_date")
        try:
            res = get_inventory_accounting_summary(company=company, as_of_date=as_of_date)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="movements")
    def movements(self, request):
        """GET /api/accounting/inventory/movements/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        search = request.query_params.get("search")
        movement_type = request.query_params.get("movement_type")
        status_filter = request.query_params.get("status")
        try:
            res = get_inventory_accounting_movements(
                company=company,
                search=search,
                movement_type=movement_type,
                status_filter=status_filter,
            )
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="preview")
    def preview(self, request, pk=None):
        """POST /api/accounting/inventory/{id}/preview/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        inv_account_id = request.data.get("inventory_account_id")
        offset_account_id = request.data.get("offset_account_id")
        unit_cost = request.data.get("unit_cost_override")
        try:
            res = get_inventory_accounting_preview(
                movement_id=pk,
                company=company,
                inventory_account_id=inv_account_id,
                offset_account_id=offset_account_id,
                unit_cost_override=unit_cost,
            )
            return Response(res, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="post")
    def post_movement(self, request, pk=None):
        """POST /api/accounting/inventory/{id}/post/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        inv_account_id = request.data.get("inventory_account_id")
        offset_account_id = request.data.get("offset_account_id")
        unit_cost = request.data.get("unit_cost_override")
        transaction_date = request.data.get("transaction_date")
        try:
            posted_je = post_inventory_movement_to_accounting(
                movement_id=pk,
                user=request.user,
                company=company,
                inventory_account_id=inv_account_id,
                offset_account_id=offset_account_id,
                unit_cost_override=unit_cost,
                transaction_date=transaction_date,
            )
            return Response({
                "message": f"Inventory movement #{pk} posted successfully to General Ledger.",
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
    def reverse_movement(self, request, pk=None):
        """POST /api/accounting/inventory/{id}/reverse/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        reason = request.data.get("reason", "")
        try:
            res = reverse_inventory_accounting(
                movement_id=pk,
                user=request.user,
                company=company,
                reason=reason,
            )
            reversal_je = res.get("reversal_journal_entry")
            return Response({
                "message": f"Inventory movement #{pk} accounting reversed successfully.",
                "movement_id": res["movement_id"],
                "status": res["status"],
                "reversal_journal_entry_id": reversal_je.id if reversal_je else None,
                "reversal_journal_entry_number": reversal_je.entry_number if reversal_je else None,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="revalue")
    def revalue(self, request):
        """POST /api/accounting/inventory/revalue/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        item_id = request.data.get("item_id")
        warehouse_id = request.data.get("warehouse_id")
        new_unit_cost = request.data.get("new_unit_cost")
        reason = request.data.get("reason", "")
        transaction_date = request.data.get("transaction_date")
        if not item_id or new_unit_cost is None:
            return Response({"error": "item_id and new_unit_cost are required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            res = post_inventory_valuation_event(
                item_id=item_id,
                warehouse_id=warehouse_id,
                new_unit_cost=new_unit_cost,
                user=request.user,
                company=company,
                reason=reason,
                transaction_date=transaction_date,
            )
            return Response(res, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="settings")
    def policy_settings(self, request):
        """GET /api/accounting/inventory/settings/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            policy = get_inventory_policy(company)
            # Add resolved leaf account metadata
            raw_acc = resolve_inventory_asset_account(Item(category="raw_material"), company)
            fg_acc = resolve_inventory_asset_account(Item(category="finished_good"), company)
            clearing_acc = resolve_inventory_clearing_account(company)
            cogs_acc = resolve_inventory_cogs_account(Item(category="raw_material"), company)
            adj_acc = resolve_inventory_adjustment_account(company)
            write_off_acc = resolve_inventory_write_off_account(company)
            return Response({
                "policy": policy,
                "resolved_accounts": {
                    "raw_material": {"id": raw_acc.id, "code": raw_acc.code, "name": raw_acc.name},
                    "finished_goods": {"id": fg_acc.id, "code": fg_acc.code, "name": fg_acc.name},
                    "clearing": {"id": clearing_acc.id, "code": clearing_acc.code, "name": clearing_acc.name},
                    "cogs": {"id": cogs_acc.id, "code": cogs_acc.code, "name": cogs_acc.name},
                    "adjustment": {"id": adj_acc.id, "code": adj_acc.code, "name": adj_acc.name},
                    "write_off": {"id": write_off_acc.id, "code": write_off_acc.code, "name": write_off_acc.name},
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


from .manufacturing_accounting import (
    get_manufacturing_policy,
    resolve_manufacturing_wip_account,
    resolve_manufacturing_raw_material_account,
    resolve_manufacturing_finished_goods_account,
    resolve_manufacturing_labor_account,
    resolve_manufacturing_overhead_account,
    resolve_manufacturing_scrap_account,
    resolve_manufacturing_variance_account,
    calculate_production_order_costs,
    get_manufacturing_accounting_preview,
    post_manufacturing_accounting,
    reverse_manufacturing_accounting,
    post_manufacturing_variance_adjustment,
    get_manufacturing_accounting_summary,
    get_manufacturing_accounting_orders,
)
from production.models import ProductionOrder


class ManufacturingAccountingViewSet(viewsets.ViewSet):
    """
    Manufacturing-to-Accounting Subledger API (Blueprint Section No. 15).
    Connects production orders, BOM material consumption, direct labor, applied overhead,
    WIP asset accumulation, finished goods completion, scrap loss, and cost variances
    directly to the Double-Entry Engine (#8) and General Ledger (#9).
    """
    permission_classes = [IsAuthenticated]

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/manufacturing/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            data = get_manufacturing_accounting_summary(company)
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="orders")
    def orders(self, request):
        """GET /api/accounting/manufacturing/orders/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        status_filter = request.query_params.get("status")
        accounting_status = request.query_params.get("accounting_status")
        search = request.query_params.get("search")
        try:
            page = int(request.query_params.get("page", 1))
            page_size = int(request.query_params.get("page_size", 20))
        except ValueError:
            page = 1
            page_size = 20
        try:
            data = get_manufacturing_accounting_orders(
                company=company,
                status=status_filter,
                accounting_status=accounting_status,
                search=search,
                page=page,
                page_size=page_size,
            )
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="detail")
    def detail_order(self, request, pk=None):
        """GET /api/accounting/manufacturing/{id}/detail/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            order = ProductionOrder.objects.select_related("recipe__product", "warehouse", "line").get(
                pk=pk,
                recipe__product__company=company,
            )
            costs = calculate_production_order_costs(order, company)
            je = JournalEntry.objects.filter(
                company=company,
                source_module="manufacturing",
                source_id=order.id,
            ).first()
            return Response({
                "id": order.id,
                "product_id": order.recipe.product.id,
                "product_name": order.recipe.product.name,
                "sku": getattr(order.recipe.product, "sku", ""),
                "warehouse_name": order.warehouse.name,
                "line_name": order.line.name if order.line else None,
                "status": order.status,
                "planned_quantity": float(order.quantity),
                "costs": {
                    "total_material_cost": float(costs["total_material_cost"]),
                    "labor_cost": float(costs["labor_cost"]),
                    "overhead_cost": float(costs["overhead_cost"]),
                    "total_production_cost": float(costs["total_production_cost"]),
                    "unit_production_cost": float(costs["unit_production_cost"]),
                    "materials": costs["materials"],
                },
                "journal_entry_id": je.id if je else None,
                "journal_entry_number": je.entry_number if je else None,
                "accounting_status": "POSTED" if (je and je.status == "posted") else ("REVERSED" if (je and je.status == "reversed") else "PENDING"),
            }, status=status.HTTP_200_OK)
        except ProductionOrder.DoesNotExist:
            return Response({"error": f"Production order #{pk} not found."}, status=status.HTTP_404_NOT_FOUND)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="preview")
    def preview(self, request, pk=None):
        """POST /api/accounting/manufacturing/{id}/preview/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        completed_qty = request.data.get("completed_quantity")
        scrap_qty = request.data.get("scrap_quantity")
        labor_cost_override = request.data.get("labor_cost")
        overhead_cost_override = request.data.get("overhead_cost")
        try:
            preview_data = get_manufacturing_accounting_preview(
                order_id=pk,
                company=company,
                completed_qty=completed_qty,
                scrap_qty=scrap_qty,
                labor_cost_override=labor_cost_override,
                overhead_cost_override=overhead_cost_override,
            )
            return Response(preview_data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="post")
    def post_accounting(self, request, pk=None):
        """POST /api/accounting/manufacturing/{id}/post/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        notes = request.data.get("notes")
        completed_qty = request.data.get("completed_quantity")
        scrap_qty = request.data.get("scrap_quantity")
        labor_cost_override = request.data.get("labor_cost")
        overhead_cost_override = request.data.get("overhead_cost")
        accounting_date = request.data.get("accounting_date")
        try:
            posted_je, created = post_manufacturing_accounting(
                order_id=pk,
                company=company,
                user=request.user,
                notes=notes,
                completed_qty=completed_qty,
                scrap_qty=scrap_qty,
                labor_cost_override=labor_cost_override,
                overhead_cost_override=overhead_cost_override,
                accounting_date=accounting_date,
            )
            return Response({
                "message": "Manufacturing accounting posted successfully." if created else "Order already posted to accounting.",
                "created": created,
                "order_id": pk,
                "journal_entry_id": posted_je.id,
                "journal_entry_number": posted_je.entry_number,
                "total_amount": float(posted_je.total_debit),
                "status": posted_je.status,
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_accounting(self, request, pk=None):
        """POST /api/accounting/manufacturing/{id}/reverse/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        reason = request.data.get("reason", "")
        try:
            reversal_je = reverse_manufacturing_accounting(
                order_id=pk,
                company=company,
                user=request.user,
                reason=reason,
            )
            return Response({
                "message": "Manufacturing accounting reversed successfully.",
                "order_id": pk,
                "reversal_journal_entry_id": reversal_je.id,
                "reversal_journal_entry_number": reversal_je.entry_number,
                "status": reversal_je.status,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="variance")
    def variance(self, request, pk=None):
        """POST /api/accounting/manufacturing/{id}/variance/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        variance_amount = request.data.get("variance_amount")
        reason = request.data.get("reason", "")
        if variance_amount is None:
            return Response({"error": "variance_amount is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            var_je = post_manufacturing_variance_adjustment(
                order_id=pk,
                company=company,
                user=request.user,
                variance_amount=variance_amount,
                reason=reason,
            )
            return Response({
                "message": "Manufacturing variance adjustment posted successfully.",
                "order_id": pk,
                "journal_entry_id": var_je.id,
                "journal_entry_number": var_je.entry_number,
                "status": var_je.status,
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else e.messages
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="settings")
    def policy_settings(self, request):
        """GET /api/accounting/manufacturing/settings/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            policy = get_manufacturing_policy(company)
            wip_acc = resolve_manufacturing_wip_account(company)
            raw_acc = resolve_manufacturing_raw_material_account(Item(category="raw_material"), company)
            fg_acc = resolve_manufacturing_finished_goods_account(Item(category="finished_good"), company)
            labor_acc = resolve_manufacturing_labor_account(company)
            overhead_acc = resolve_manufacturing_overhead_account(company)
            scrap_acc = resolve_manufacturing_scrap_account(company)
            var_acc = resolve_manufacturing_variance_account(company)
            return Response({
                "policy": policy,
                "resolved_accounts": {
                    "wip": {"id": wip_acc.id, "code": wip_acc.code, "name": wip_acc.name},
                    "raw_material": {"id": raw_acc.id, "code": raw_acc.code, "name": raw_acc.name},
                    "finished_goods": {"id": fg_acc.id, "code": fg_acc.code, "name": fg_acc.name},
                    "labor": {"id": labor_acc.id, "code": labor_acc.code, "name": labor_acc.name},
                    "overhead": {"id": overhead_acc.id, "code": overhead_acc.code, "name": overhead_acc.name},
                    "scrap": {"id": scrap_acc.id, "code": scrap_acc.code, "name": scrap_acc.name},
                    "variance": {"id": var_acc.id, "code": var_acc.code, "name": var_acc.name},
                }
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ==============================================================================
# BLUEPRINT SECTION #16 — EXPENSES-TO-ACCOUNTING VIEWSETS
# ==============================================================================

from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from .models import Expense, ExpenseCategory, ExpenseAuditLog
from .serializers import (
    ExpenseCategorySerializer,
    ExpenseSerializer,
    ExpenseDetailSerializer,
    ExpenseCreateUpdateSerializer,
)
from .expenses_accounting import (
    seed_default_expense_categories,
    get_expenses_policy,
    resolve_expense_account,
    resolve_tax_account,
    resolve_payment_account,
    submit_expense,
    approve_expense,
    reject_expense,
    cancel_expense,
    attach_receipt_to_expense,
    get_expense_accounting_preview,
    post_expense_accounting,
    reverse_expense_accounting,
    get_expenses_summary,
)


class ExpenseCategoryViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    CRUD operations and seeding for configurable Expense Categories.
    """
    company_field = "company"
    queryset = ExpenseCategory.objects.select_related("expense_account", "expense_account__account_type").all()
    serializer_class = ExpenseCategorySerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["is_active"]
    search_fields = ["name", "code", "description"]
    ordering_fields = ["name", "code", "created_at"]
    ordering = ["name"]

    def perform_create(self, serializer):
        serializer.save(company=self.request.user.company)

    @action(detail=False, methods=["post"], url_path="seed-defaults")
    def seed_defaults(self, request):
        """POST /api/accounting/expense-categories/seed-defaults/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)

        cats = seed_default_expense_categories(company)
        return Response({
            "message": f"Successfully seeded {len(cats)} default expense categories.",
            "categories": ExpenseCategorySerializer(cats, many=True).data,
        }, status=status.HTTP_200_OK)


class ExpenseViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    CRUD and accounting integration operations for operational expenses.
    Supports employee & vendor expense logging, receipt uploads, approval workflows,
    tax handling, payment source selection, and double-entry General Ledger postings.
    """
    company_field = "company"
    queryset = (
        Expense.objects
        .select_related(
            "category",
            "employee",
            "vendor",
            "expense_account",
            "tax_account",
            "payment_account",
            "journal_entry",
            "reversal_journal_entry",
            "created_by",
            "submitted_by",
            "approved_by",
            "posted_by",
        )
        .prefetch_related("audit_logs", "journal_entry__lines")
        .all()
    )
    permission_classes = [IsFinanceOrAdmin]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["expense_type", "approval_status", "accounting_status", "payment_source", "category"]
    search_fields = ["expense_number", "title", "description", "vendor_name_raw", "employee__first_name", "employee__last_name", "vendor__name"]
    ordering_fields = ["expense_date", "created_at", "total_amount", "expense_number"]
    ordering = ["-expense_date", "-created_at"]

    def get_serializer_class(self):
        if self.action in ["create", "update", "partial_update"]:
            return ExpenseCreateUpdateSerializer
        if self.action == "retrieve":
            return ExpenseDetailSerializer
        return ExpenseSerializer

    def perform_create(self, serializer):
        company = self.request.user.company
        user = self.request.user if self.request.user.is_authenticated else None
        expense = serializer.save(company=company, created_by=user)
        ExpenseAuditLog.objects.create(
            expense=expense,
            action="created",
            actor=user,
            details={
                "title": expense.title,
                "amount": float(expense.total_amount),
                "type": expense.expense_type,
                "payment_source": expense.payment_source,
            },
            notes="Expense created in draft status",
        )

    def perform_update(self, serializer):
        instance = self.get_object()
        if instance.accounting_status == "posted":
            raise ValidationError(_("Cannot modify an expense that has already been posted to the General Ledger."))
        expense = serializer.save()
        user = self.request.user if self.request.user.is_authenticated else None
        ExpenseAuditLog.objects.create(
            expense=expense,
            action="updated",
            actor=user,
            details={
                "title": expense.title,
                "amount": float(expense.total_amount),
                "approval_status": expense.approval_status,
            },
            notes="Expense details updated",
        )

    def perform_destroy(self, instance):
        if instance.accounting_status == "posted":
            raise ValidationError(_("Cannot delete an expense that has been posted to the General Ledger. Reverse it instead."))
        instance.delete()

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/expenses/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            data = get_expenses_summary(company)
            return Response(data, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="submit")
    def submit(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/submit/"""
        expense = self.get_object()
        try:
            updated = submit_expense(expense, user=request.user)
            return Response(ExpenseDetailSerializer(updated, context={"request": request}).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/approve/"""
        expense = self.get_object()
        notes = request.data.get("notes", "")
        try:
            updated = approve_expense(expense, user=request.user, notes=notes)
            return Response(ExpenseDetailSerializer(updated, context={"request": request}).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/reject/"""
        expense = self.get_object()
        reason = request.data.get("reason", "")
        try:
            updated = reject_expense(expense, user=request.user, reason=reason)
            return Response(ExpenseDetailSerializer(updated, context={"request": request}).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/cancel/"""
        expense = self.get_object()
        try:
            updated = cancel_expense(expense, user=request.user)
            return Response(ExpenseDetailSerializer(updated, context={"request": request}).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="receipt", parser_classes=[MultiPartParser, FormParser])
    def upload_receipt(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/receipt/"""
        expense = self.get_object()
        file_obj = request.FILES.get("file") or request.FILES.get("receipt")
        if not file_obj:
            return Response({"error": "No file uploaded. Key 'file' or 'receipt' required in form-data."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            updated = attach_receipt_to_expense(expense, file_obj=file_obj, user=request.user)
            return Response(ExpenseDetailSerializer(updated, context={"request": request}).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="preview")
    def preview(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/preview/"""
        expense = self.get_object()
        try:
            preview_data = get_expense_accounting_preview(
                expense,
                expense_account_id=request.data.get("expense_account_id"),
                tax_account_id=request.data.get("tax_account_id"),
                payment_account_id=request.data.get("payment_account_id"),
                payment_source_override=request.data.get("payment_source"),
                amount_before_tax_override=request.data.get("amount_before_tax"),
                tax_amount_override=request.data.get("tax_amount"),
            )
            return Response(preview_data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="post")
    def post_accounting(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/post/"""
        expense = self.get_object()
        try:
            posted_je = post_expense_accounting(
                expense=expense,
                user=request.user,
                transaction_date=request.data.get("transaction_date"),
                expense_account_id=request.data.get("expense_account_id"),
                tax_account_id=request.data.get("tax_account_id"),
                payment_account_id=request.data.get("payment_account_id"),
                notes=request.data.get("notes"),
            )
            return Response({
                "message": f"Expense #{expense.expense_number} successfully posted to General Ledger.",
                "expense_id": expense.id,
                "expense_number": expense.expense_number,
                "journal_entry_id": posted_je.id,
                "journal_entry_number": posted_je.entry_number,
                "total_amount": float(posted_je.total_debit),
                "status": posted_je.status,
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_accounting(self, request, pk=None):
        """POST /api/accounting/expenses/{id}/reverse/"""
        expense = self.get_object()
        reason = request.data.get("reason", "")
        reversal_date = request.data.get("reversal_date")
        try:
            reversal_je = reverse_expense_accounting(
                expense=expense,
                user=request.user,
                reason=reason,
                reversal_date=reversal_date,
            )
            return Response({
                "message": f"Expense #{expense.expense_number} accounting reversed successfully.",
                "expense_id": expense.id,
                "reversal_journal_entry_id": reversal_je.id,
                "reversal_journal_entry_number": reversal_je.entry_number,
                "status": reversal_je.status,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ==============================================================================
# BLUEPRINT SECTION #17 — CASH & BANK VIEWSETS
# ==============================================================================

from .models import BankAccount, BankReconciliation, BankTransaction, BankAuditLog
from .serializers import (
    BankAccountSerializer,
    BankAccountCreateUpdateSerializer,
    BankTransactionSerializer,
    BankReconciliationSerializer,
    BankAuditLogSerializer,
    OpeningBalanceInputSerializer,
    DepositInputSerializer,
    WithdrawalInputSerializer,
    BankTransferInputSerializer,
    ReconciliationInputSerializer,
)
from .cash_bank import (
    record_opening_balance,
    record_deposit,
    record_withdrawal,
    record_transfer,
    import_bank_statement_csv,
    get_unreconciled_transactions_queue,
    reconcile_bank_account,
    reopen_reconciliation,
    get_cash_bank_summary,
)


class BankAccountViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Blueprint Section #17 — Bank Account Master API.
    Manages checking, savings, money market, credit card, and petty cash accounts.
    Supports opening balances, statement CSV import, unreconciled transaction queues,
    and activation toggling.
    """
    company_field = "company"
    queryset = BankAccount.objects.select_related("gl_account", "gl_account__account_type", "opening_balance_journal_entry").all()
    serializer_class = BankAccountSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["account_type", "currency", "is_active"]
    search_fields = ["account_name", "bank_name", "account_number", "routing_number", "swift_bic"]
    ordering_fields = ["bank_name", "account_name", "created_at", "reconciled_balance"]
    ordering = ["bank_name", "account_name"]

    def get_serializer_class(self):
        if self.action in ["create", "update", "partial_update"]:
            return BankAccountCreateUpdateSerializer
        return BankAccountSerializer

    def perform_create(self, serializer):
        company = self.request.user.company
        user = self.request.user if self.request.user.is_authenticated else None
        account = serializer.save(company=company)
        BankAuditLog.objects.create(
            company=company,
            bank_account=account,
            action="account_created",
            actor=user,
            details={
                "bank_name": account.bank_name,
                "account_name": account.account_name,
                "account_type": account.account_type,
                "gl_account": account.gl_account.code,
            },
            notes="Bank account created",
        )

    def perform_update(self, serializer):
        company = self.request.user.company
        user = self.request.user if self.request.user.is_authenticated else None
        account = serializer.save()
        BankAuditLog.objects.create(
            company=company,
            bank_account=account,
            action="account_updated",
            actor=user,
            details={
                "bank_name": account.bank_name,
                "account_name": account.account_name,
                "is_active": account.is_active,
            },
            notes="Bank account updated",
        )

    @action(detail=True, methods=["post"], url_path="opening-balance")
    def opening_balance(self, request, pk=None):
        """POST /api/accounting/bank-accounts/{id}/opening-balance/"""
        bank_account = self.get_object()
        serializer = OpeningBalanceInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            res = record_opening_balance(
                bank_account_id=bank_account.id,
                amount=data["amount"],
                balance_date=data["date"],
                user=request.user,
                equity_account_id=data.get("equity_account"),
                notes=data.get("notes", ""),
            )
            return Response({
                "message": f"Opening balance of {data['amount']} posted successfully.",
                "bank_account": BankAccountSerializer(res["bank_account"]).data,
                "journal_entry_id": res["journal_entry"].id,
                "journal_entry_number": res["journal_entry"].entry_number,
                "transaction_id": res["transaction"].id,
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="import-statement")
    def import_statement(self, request, pk=None):
        """POST /api/accounting/bank-accounts/{id}/import-statement/"""
        bank_account = self.get_object()
        file_obj = request.FILES.get("file")
        csv_data = request.data.get("csv_data")

        if not file_obj and not csv_data:
            return Response({"error": "A CSV file upload ('file') or 'csv_data' text is required."}, status=status.HTTP_400_BAD_REQUEST)

        payload = file_obj if file_obj else csv_data
        try:
            res = import_bank_statement_csv(
                bank_account_id=bank_account.id,
                csv_text_or_file=payload,
                user=request.user,
            )
            return Response({
                "message": f"Statement import completed. {res['imported_count']} rows imported, {res['skipped_count']} duplicate rows skipped.",
                **res
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="transactions")
    def account_transactions(self, request, pk=None):
        """GET /api/accounting/bank-accounts/{id}/transactions/"""
        bank_account = self.get_object()
        qs = BankTransaction.objects.filter(bank_account=bank_account)

        status_filter = request.query_params.get("reconciliation_status")
        if status_filter:
            qs = qs.filter(reconciliation_status=status_filter)

        txn_type = request.query_params.get("transaction_type")
        if txn_type:
            qs = qs.filter(transaction_type=txn_type)

        direction = request.query_params.get("direction")
        if direction:
            qs = qs.filter(direction=direction)

        start_date = request.query_params.get("start_date")
        if start_date:
            qs = qs.filter(transaction_date__gte=start_date)

        end_date = request.query_params.get("end_date")
        if end_date:
            qs = qs.filter(transaction_date__lte=end_date)

        search = request.query_params.get("search")
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(description__icontains=search) |
                Q(reference__icontains=search) |
                Q(counterparty__icontains=search) |
                Q(external_id__icontains=search)
            )

        serializer = BankTransactionSerializer(qs.order_by("-transaction_date", "-id"), many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="unreconciled-queue")
    def unreconciled_queue(self, request, pk=None):
        """GET /api/accounting/bank-accounts/{id}/unreconciled-queue/"""
        bank_account = self.get_object()
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        search = request.query_params.get("search")

        try:
            queue_data = get_unreconciled_transactions_queue(
                bank_account_id=bank_account.id,
                start_date=start_date,
                end_date=end_date,
                search=search,
            )
            bt_serialized = BankTransactionSerializer(queue_data["bank_transactions"], many=True).data
            jl_serialized = [
                {
                    "id": jl.id,
                    "journal_entry_id": jl.journal_entry_id,
                    "journal_entry_number": jl.journal_entry.entry_number,
                    "transaction_date": jl.journal_entry.transaction_date.isoformat(),
                    "debit": float(jl.debit),
                    "credit": float(jl.credit),
                    "amount": float(jl.debit if jl.debit > 0 else jl.credit),
                    "direction": "inflow" if jl.debit > 0 else "outflow",
                    "description": jl.description or jl.journal_entry.narration,
                    "reference": jl.journal_entry.reference,
                    "account_code": jl.account.code,
                    "account_name": jl.account.name,
                }
                for jl in queue_data["journal_lines"]
            ]
            return Response({
                "bank_account_id": bank_account.id,
                "bank_account_name": bank_account.account_name,
                "reconciled_balance": float(bank_account.reconciled_balance),
                "current_gl_balance": float(bank_account.current_gl_balance),
                "unreconciled_difference": float(bank_account.unreconciled_difference),
                "bank_transactions": bt_serialized,
                "journal_lines": jl_serialized,
            }, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        """POST /api/accounting/bank-accounts/{id}/toggle-active/"""
        bank_account = self.get_object()
        bank_account.is_active = not bank_account.is_active
        bank_account.save(update_fields=["is_active", "updated_at"])
        action_name = "account_activated" if bank_account.is_active else "account_deactivated"
        BankAuditLog.objects.create(
            company=bank_account.company,
            bank_account=bank_account,
            action=action_name,
            actor=request.user,
            details={"is_active": bank_account.is_active},
            notes=f"Bank account {'activated' if bank_account.is_active else 'deactivated'}",
        )
        return Response(BankAccountSerializer(bank_account).data, status=status.HTTP_200_OK)


class BankTransactionViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Blueprint Section #17 — Bank Transaction Subledger API.
    Lists, filters, records deposits, withdrawals, and internal bank transfers.
    """
    company_field = "company"
    queryset = BankTransaction.objects.select_related(
        "bank_account",
        "journal_entry",
        "journal_entry_line",
        "reconciliation",
        "created_by"
    ).all()
    serializer_class = BankTransactionSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["bank_account", "direction", "transaction_type", "source", "matching_status", "reconciliation_status"]
    search_fields = ["description", "reference", "counterparty", "external_id", "source_document_ref"]
    ordering_fields = ["transaction_date", "created_at", "amount"]
    ordering = ["-transaction_date", "-created_at"]

    @action(detail=False, methods=["post"], url_path="deposit")
    def deposit(self, request):
        """POST /api/accounting/bank-transactions/deposit/"""
        serializer = DepositInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        bank_account_id = request.data.get("bank_account")
        if not bank_account_id:
            return Response({"error": "bank_account is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bt = record_deposit(
                bank_account_id=bank_account_id,
                amount=data["amount"],
                txn_date=data["date"],
                offset_account_id=data["offset_account"],
                user=request.user,
                description=data.get("description", ""),
                reference=data.get("reference", ""),
                counterparty=data.get("counterparty", ""),
            )
            return Response(BankTransactionSerializer(bt).data, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="withdrawal")
    def withdrawal(self, request):
        """POST /api/accounting/bank-transactions/withdrawal/"""
        serializer = WithdrawalInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        bank_account_id = request.data.get("bank_account")
        if not bank_account_id:
            return Response({"error": "bank_account is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            bt = record_withdrawal(
                bank_account_id=bank_account_id,
                amount=data["amount"],
                txn_date=data["date"],
                offset_account_id=data["offset_account"],
                user=request.user,
                description=data.get("description", ""),
                reference=data.get("reference", ""),
                counterparty=data.get("counterparty", ""),
                transaction_type=data.get("transaction_type", "withdrawal"),
            )
            return Response(BankTransactionSerializer(bt).data, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["post"], url_path="transfer")
    def transfer(self, request):
        """POST /api/accounting/bank-transactions/transfer/"""
        serializer = BankTransferInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            res = record_transfer(
                from_account_id=data["from_account"],
                to_account_id=data["to_account"],
                amount=data["amount"],
                txn_date=data["date"],
                user=request.user,
                description=data.get("description", ""),
                reference=data.get("reference", ""),
            )
            return Response({
                "message": "Internal bank transfer posted successfully.",
                "journal_entry_id": res["journal_entry"].id,
                "journal_entry_number": res["journal_entry"].entry_number,
                "outflow_transaction": BankTransactionSerializer(res["outflow_transaction"]).data,
                "inflow_transaction": BankTransactionSerializer(res["inflow_transaction"]).data,
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class BankReconciliationViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Blueprint Section #17 — Bank Reconciliation API.
    Performs matching, statement reconciliation confirmation, reopening, and audit trail retrieval.
    """
    company_field = "company"
    queryset = BankReconciliation.objects.select_related("bank_account", "reconciled_by").prefetch_related("transactions").all()
    serializer_class = BankReconciliationSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["bank_account", "status"]
    search_fields = ["reconciliation_number", "notes", "bank_account__account_name"]
    ordering_fields = ["statement_date", "created_at"]
    ordering = ["-statement_date", "-created_at"]

    @action(detail=False, methods=["post"], url_path="reconcile")
    def reconcile(self, request):
        """POST /api/accounting/bank-reconciliations/reconcile/"""
        serializer = ReconciliationInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        bank_account_id = request.data.get("bank_account")
        if not bank_account_id:
            return Response({"error": "bank_account is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            rec = reconcile_bank_account(
                bank_account_id=bank_account_id,
                statement_date=data["statement_date"],
                statement_balance=data["statement_balance"],
                transaction_ids=data.get("transaction_ids", []),
                journal_line_ids=data.get("journal_line_ids", []),
                user=request.user,
                notes=data.get("notes", ""),
            )
            return Response({
                "message": f"Reconciliation {rec.reconciliation_number} completed successfully.",
                "reconciliation": BankReconciliationSerializer(rec).data,
            }, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None):
        """POST /api/accounting/bank-reconciliations/{id}/reopen/"""
        reason = request.data.get("reason", "")
        try:
            rec = reopen_reconciliation(
                reconciliation_id=pk,
                user=request.user,
                reason=reason,
            )
            return Response({
                "message": f"Reconciliation {rec.reconciliation_number} reopened successfully.",
                "reconciliation": BankReconciliationSerializer(rec).data,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="cleared-items")
    def cleared_items(self, request, pk=None):
        """GET /api/accounting/bank-reconciliations/{id}/cleared-items/"""
        rec = self.get_object()
        txns = BankTransactionSerializer(rec.transactions.all(), many=True).data
        lines = [
            {
                "id": jl.id,
                "journal_entry_number": jl.journal_entry.entry_number,
                "transaction_date": jl.journal_entry.transaction_date.isoformat(),
                "debit": float(jl.debit),
                "credit": float(jl.credit),
                "amount": float(jl.debit if jl.debit > 0 else jl.credit),
                "direction": "inflow" if jl.debit > 0 else "outflow",
                "description": jl.description,
            }
            for jl in rec.reconciled_journal_lines.select_related("journal_entry").all()
        ]
        return Response({
            "reconciliation_id": rec.id,
            "reconciliation_number": rec.reconciliation_number,
            "transactions": txns,
            "journal_lines": lines,
        }, status=status.HTTP_200_OK)


class BankingSummaryViewSet(viewsets.ViewSet):
    """
    Cash & Bank Summary API (Blueprint Section No. 17).
    Provides tenant-level high-level banking statistics and KPIs.
    """
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/banking/summary/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "User company context required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            res = get_cash_bank_summary(company=company)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ==============================================================================
# BLUEPRINT SECTION #18 — PAYMENTS & ALLOCATIONS VIEWS
# ==============================================================================

from .models import Payment, PaymentAllocation, PaymentAuditLog
from .serializers import (
    PaymentSerializer,
    PaymentAllocationSerializer,
    PaymentAuditLogSerializer,
    CreatePaymentInputSerializer,
    AllocatePaymentInputSerializer,
    ReversePaymentInputSerializer,
)
from .payments_allocations import (
    create_payment,
    post_payment,
    allocate_payment,
    unallocate_allocation,
    reverse_payment,
    cancel_payment,
    get_available_documents_for_allocation,
    get_payments_summary,
)


class PaymentViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Blueprint Section #18 — Payments & Allocations API.
    Manages Customer Receipts and Vendor Payments:
      - Draft creation and atomic GL posting (#8, #9)
      - Cash & Bank integration (#17)
      - Multi-document allocation and partial payments
      - Allocation reversals and payment reversals
      - Full audit trail lineage
    """
    company_field = "company"
    queryset = Payment.objects.select_related(
        "customer", "vendor", "bank_account", "cash_account",
        "journal_entry", "bank_transaction", "reversal_journal_entry",
        "created_by", "approved_by"
    ).prefetch_related(
        "allocations", "allocations__invoice", "allocations__bill"
    ).all()
    serializer_class = PaymentSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = [
        "payment_type", "status", "allocation_status",
        "payment_source_type", "customer", "vendor", "bank_account"
    ]
    search_fields = [
        "payment_number", "reference", "external_reference",
        "notes", "customer__name", "vendor__name"
    ]
    ordering_fields = ["payment_date", "created_at", "amount", "payment_number"]
    ordering = ["-payment_date", "-created_at"]

    def create(self, request, *args, **kwargs):
        """POST /api/accounting/payments/"""
        company = getattr(request.user, "company", None)
        serializer = CreatePaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            payment = create_payment(
                company=company,
                user=request.user,
                payment_type=data["payment_type"],
                amount=data["amount"],
                payment_date=data.get("payment_date"),
                customer_id=data.get("customer_id"),
                vendor_id=data.get("vendor_id"),
                payment_source_type=data.get("payment_source_type", "bank"),
                bank_account_id=data.get("bank_account_id"),
                cash_account_id=data.get("cash_account_id"),
                payment_method=data.get("payment_method", "bank_transfer"),
                reference=data.get("reference", ""),
                external_reference=data.get("external_reference", ""),
                notes=data.get("notes", ""),
                auto_post=data.get("auto_post", False),
                allocations=data.get("allocations", []),
            )
            return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="post")
    def post_action(self, request, pk=None):
        """POST /api/accounting/payments/{id}/post/"""
        payment = self.get_object()
        company = getattr(request.user, "company", None)
        allocations = request.data.get("allocations", [])

        try:
            posted = post_payment(
                payment_id=payment.id,
                user=request.user,
                company=company,
                allocations=allocations,
            )
            return Response(PaymentSerializer(posted).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="allocate")
    def allocate_action(self, request, pk=None):
        """POST /api/accounting/payments/{id}/allocate/"""
        payment = self.get_object()
        company = getattr(request.user, "company", None)
        serializer = AllocatePaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            allocated = allocate_payment(
                payment_id=payment.id,
                allocations=serializer.validated_data["allocations"],
                user=request.user,
                company=company,
            )
            return Response(PaymentSerializer(allocated).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_action(self, request, pk=None):
        """POST /api/accounting/payments/{id}/reverse/"""
        payment = self.get_object()
        company = getattr(request.user, "company", None)
        serializer = ReversePaymentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            reversed_pmt = reverse_payment(
                payment_id=payment.id,
                reason=serializer.validated_data["reason"],
                user=request.user,
                company=company,
            )
            return Response(PaymentSerializer(reversed_pmt).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel_action(self, request, pk=None):
        """POST /api/accounting/payments/{id}/cancel/"""
        payment = self.get_object()
        company = getattr(request.user, "company", None)

        try:
            cancelled = cancel_payment(
                payment_id=payment.id,
                user=request.user,
                company=company,
            )
            return Response(PaymentSerializer(cancelled).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["get"], url_path="available-documents")
    def available_documents(self, request, pk=None):
        """GET /api/accounting/payments/{id}/available-documents/"""
        payment = self.get_object()
        company = getattr(request.user, "company", None)
        docs = get_available_documents_for_allocation(payment_id=payment.id, company=company)
        return Response(docs, status=status.HTTP_200_OK)

    @action(detail=True, methods=["get"], url_path="audit-trail")
    def audit_trail(self, request, pk=None):
        """GET /api/accounting/payments/{id}/audit-trail/"""
        payment = self.get_object()
        logs = payment.audit_logs.select_related("actor").all()
        return Response(PaymentAuditLogSerializer(logs, many=True).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary_action(self, request):
        """GET /api/accounting/payments/summary/"""
        company = getattr(request.user, "company", None)
        try:
            res = get_payments_summary(company=company)
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class PaymentAllocationViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    Blueprint Section #18 — Read-only listing and unallocation of individual payment allocations.
    """
    company_field = "company"
    queryset = PaymentAllocation.objects.select_related(
        "payment", "invoice", "bill", "created_by"
    ).all()
    serializer_class = PaymentAllocationSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_fields = ["payment", "status", "invoice", "bill"]
    ordering = ["-created_at"]

    @action(detail=True, methods=["post"], url_path="unallocate")
    def unallocate(self, request, pk=None):
        """POST /api/accounting/payment-allocations/{id}/unallocate/"""
        allocation = self.get_object()
        company = getattr(request.user, "company", None)

        try:
            updated_pmt = unallocate_allocation(
                allocation_id=allocation.id,
                user=request.user,
                company=company,
            )
            return Response({
                "message": f"Allocation {allocation.allocation_number} unallocated successfully.",
                "payment": PaymentSerializer(updated_pmt).data,
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


# =============================================================================
# BLUEPRINT SECTION #19 — TAX LAYER VIEWSETS
# =============================================================================

from .models import (
    TaxCode,
    TaxTransactionLine,
    TaxAdjustment,
    TaxAuditLog,
)
from .serializers import (
    TaxCodeSerializer,
    TaxTransactionLineSerializer,
    TaxAdjustmentSerializer,
    TaxAuditLogSerializer,
    CalculateTaxInputSerializer,
    CreateTaxAdjustmentInputSerializer,
    ReverseTaxLineInputSerializer,
)
from .tax import (
    calculate_tax,
    calculate_lines_tax,
    record_tax_line,
    create_tax_adjustment,
    post_tax_adjustment,
    reverse_tax_adjustment,
    reverse_tax_transaction_line,
    get_tax_summary,
    get_tax_report,
    seed_default_tax_codes,
)


class TaxCodeViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Blueprint Section #19 — Tax Codes & Rates Master Data API.
    Provides CRUD, activation/deactivation, and standard default seeding.
    """
    company_field = "company"
    queryset = TaxCode.objects.select_related("tax_account", "created_by").all()
    serializer_class = TaxCodeSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["tax_type", "calculation_mode", "is_active", "is_recoverable"]
    search_fields = ["code", "name", "description"]
    ordering_fields = ["code", "rate", "created_at"]
    ordering = ["code"]

    def perform_create(self, serializer):
        company = getattr(self.request.user, "company", None)
        serializer.save(company=company, created_by=self.request.user)
        TaxAuditLog.objects.create(
            company=company,
            tax_code=serializer.instance,
            action="tax_code_created",
            actor=self.request.user,
            details={"code": serializer.instance.code, "rate": float(serializer.instance.rate)}
        )

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)
        TaxAuditLog.objects.create(
            company=serializer.instance.company,
            tax_code=serializer.instance,
            action="tax_code_updated",
            actor=self.request.user,
            details={"code": serializer.instance.code, "rate": float(serializer.instance.rate)}
        )

    @action(detail=True, methods=["post"], url_path="toggle-active")
    def toggle_active(self, request, pk=None):
        """POST /api/accounting/tax-codes/{id}/toggle-active/"""
        tax_code = self.get_object()
        tax_code.is_active = not tax_code.is_active
        tax_code.updated_by = request.user
        tax_code.save(update_fields=["is_active", "updated_by", "updated_at"])

        TaxAuditLog.objects.create(
            company=tax_code.company,
            tax_code=tax_code,
            action="tax_code_activated" if tax_code.is_active else "tax_code_deactivated",
            actor=request.user,
            details={"code": tax_code.code, "is_active": tax_code.is_active}
        )

        return Response(TaxCodeSerializer(tax_code).data, status=status.HTTP_200_OK)

    @action(detail=False, methods=["post"], url_path="seed-defaults")
    def seed_defaults(self, request):
        """POST /api/accounting/tax-codes/seed-defaults/"""
        company = getattr(request.user, "company", None)
        if not company:
            return Response({"error": "No company associated with user"}, status=status.HTTP_400_BAD_REQUEST)

        created_cnt = seed_default_tax_codes(company, user=request.user)
        return Response({
            "status": "success",
            "message": f"Successfully initialized {created_cnt} standard tax code(s).",
            "total_codes": TaxCode.objects.filter(company=company).count()
        }, status=status.HTTP_200_OK)


class TaxTransactionLineViewSet(CompanyScopedMixin, viewsets.ReadOnlyModelViewSet):
    """
    Blueprint Section #19 — Transaction-Level Tax Breakdown API.
    Read-only view of granular tax lines with controlled reversal capability.
    """
    company_field = "company"
    queryset = TaxTransactionLine.objects.select_related(
        "tax_code", "tax_account", "journal_entry", "reversal_journal_entry", "created_by"
    ).all()
    serializer_class = TaxTransactionLineSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["source_module", "is_posted", "is_reversed", "tax_code", "tax_account"]
    search_fields = ["source_reference", "source_id", "reversal_reference"]
    ordering = ["-transaction_date", "-created_at"]

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_line(self, request, pk=None):
        """POST /api/accounting/tax-lines/{id}/reverse/"""
        line = self.get_object()
        company = getattr(request.user, "company", None)
        serializer = ReverseTaxLineInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            rev_line = reverse_tax_transaction_line(
                tax_line_id=line.id,
                reason=serializer.validated_data["reason"],
                user=request.user,
                company=company,
            )
            return Response(TaxTransactionLineSerializer(rev_line).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class TaxAdjustmentViewSet(CompanyScopedMixin, viewsets.ModelViewSet):
    """
    Blueprint Section #19 — Controlled Tax Adjustment API.
    Creates and posts double-entry adjustments for tax liabilities or input credits.
    """
    company_field = "company"
    queryset = TaxAdjustment.objects.select_related(
        "tax_code", "tax_account", "offset_account", "journal_entry", "reversal_journal_entry"
    ).all()
    serializer_class = TaxAdjustmentSerializer
    permission_classes = [IsFinanceOrAdmin]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = ["status", "adjustment_direction", "tax_code"]
    search_fields = ["adjustment_number", "reason"]
    ordering = ["-adjustment_date", "-created_at"]

    def create(self, request, *args, **kwargs):
        company = getattr(request.user, "company", None)
        serializer = CreateTaxAdjustmentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            adj = create_tax_adjustment(
                company=company,
                user=request.user,
                data=serializer.validated_data,
            )
            return Response(TaxAdjustmentSerializer(adj).data, status=status.HTTP_201_CREATED)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="post")
    def post_action(self, request, pk=None):
        """POST /api/accounting/tax-adjustments/{id}/post/"""
        adj = self.get_object()
        company = getattr(request.user, "company", None)

        try:
            posted_adj = post_tax_adjustment(
                adjustment_id=adj.id,
                user=request.user,
                company=company,
            )
            return Response(TaxAdjustmentSerializer(posted_adj).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=["post"], url_path="reverse")
    def reverse_action(self, request, pk=None):
        """POST /api/accounting/tax-adjustments/{id}/reverse/"""
        adj = self.get_object()
        company = getattr(request.user, "company", None)
        serializer = ReverseTaxLineInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            rev_adj = reverse_tax_adjustment(
                adjustment_id=adj.id,
                reason=serializer.validated_data["reason"],
                user=request.user,
                company=company,
            )
            return Response(TaxAdjustmentSerializer(rev_adj).data, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)


class TaxManagementViewSet(viewsets.ViewSet):
    """
    Blueprint Section #19 — Central Tax Operations, Calculation, Summary & Reporting API.
    Mounted at /api/accounting/taxes/
    """
    permission_classes = [IsFinanceOrAdmin]

    @action(detail=False, methods=["post"], url_path="calculate")
    def calculate(self, request):
        """POST /api/accounting/taxes/calculate/"""
        serializer = CalculateTaxInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            res = calculate_tax(
                amount=serializer.validated_data["amount"],
                tax_code=serializer.validated_data.get("tax_code_id"),
                tax_rate=serializer.validated_data.get("tax_rate"),
                calculation_mode=serializer.validated_data.get("calculation_mode"),
                precision=serializer.validated_data.get("precision", 2),
            )
            return Response({
                "taxable_amount": float(res["taxable_amount"]),
                "tax_rate": float(res["tax_rate"]),
                "tax_amount": float(res["tax_amount"]),
                "total_amount": float(res["total_amount"]),
                "calculation_mode": res["calculation_mode"],
                "tax_code_id": res["tax_code_id"],
                "tax_code": res["tax_code"],
            }, status=status.HTTP_200_OK)
        except DjangoValidationError as e:
            msg = e.message_dict if hasattr(e, "message_dict") else getattr(e, "messages", str(e))
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """GET /api/accounting/taxes/summary/"""
        company = getattr(request.user, "company", None)
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        tax_code_id = request.query_params.get("tax_code_id")
        account_id = request.query_params.get("account_id")

        try:
            res = get_tax_summary(
                company=company,
                start_date=start_date,
                end_date=end_date,
                tax_code_id=tax_code_id,
                account_id=account_id,
            )
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="report")
    def report(self, request):
        """GET /api/accounting/taxes/report/"""
        company = getattr(request.user, "company", None)
        start_date = request.query_params.get("start_date")
        end_date = request.query_params.get("end_date")
        tax_code_id = request.query_params.get("tax_code_id")
        account_id = request.query_params.get("account_id")
        source_module = request.query_params.get("source_module")

        try:
            res = get_tax_report(
                company=company,
                start_date=start_date,
                end_date=end_date,
                tax_code_id=tax_code_id,
                account_id=account_id,
                source_module=source_module,
            )
            return Response(res, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=False, methods=["get"], url_path="audit-trail")
    def audit_trail(self, request):
        """GET /api/accounting/taxes/audit-trail/"""
        company = getattr(request.user, "company", None)
        logs = TaxAuditLog.objects.filter(company=company).select_related(
            "actor", "tax_code", "tax_adjustment"
        ).order_by("-created_at")[:100]
        return Response(TaxAuditLogSerializer(logs, many=True).data, status=status.HTTP_200_OK)












