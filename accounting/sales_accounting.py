"""
Sales-to-Accounting Integration Subledger (Blueprint Section No. 12)
Connects operational sales invoices, line items, revenue recognition, tax accounting,
and Accounts Receivable (#10) directly to the Double-Entry Engine (#8) and General Ledger (#9).
"""

from decimal import Decimal
from datetime import date, datetime
from django.db import transaction
from django.db.models import Sum, Q, F
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import Company
from sales.models import Customer, Invoice, InvoiceLine, CustomerPayment
from accounting.models import Account, AccountType, JournalEntry, JournalEntryLine, AccountingSettings, AccountingPeriod
from accounting.engine import post_journal_entry, reverse_journal_entry
from accounting.receivables import (
    get_ar_account,
    get_sales_revenue_account,
    get_bank_account,
    sync_customer_balance,
)


def get_tax_payable_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Sales Tax Payable liability account for the tenant.
    Prefers code '2200', then account containing 'Tax' under Current Liabilities.
    """
    if account_id:
        acc = Account.objects.filter(
            pk=account_id,
            company=company,
            is_active=True,
        ).first()
        if not acc:
            raise ValidationError(_(f"Specified tax account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post tax liability to a header account. Select a leaf account."))
        return acc

    # 1. Direct code match: 2200
    acc = Account.objects.filter(
        company=company,
        code="2200",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    # 2. Account with name containing Tax under liability
    acc = Account.objects.filter(
        company=company,
        name__icontains="Tax",
        account_type__category="liability",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 3. Create standard leaf account '2200 Sales Tax Payable' under 2000 Current Liabilities
    parent = Account.objects.filter(company=company, code="2000").first()
    acc_type = AccountType.objects.filter(name="Accrued Expenses & Other Current Liabilities").first()
    if not acc_type:
        acc_type = AccountType.objects.filter(category="liability").first()

    acc, _ = Account.objects.get_or_create(
        company=company,
        code="2200",
        defaults={
            "name": "Sales Tax Payable",
            "account_type": acc_type,
            "parent": parent,
            "description": "Sales tax, VAT, and output taxes collected from customers payable to tax authorities",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def get_sales_accounting_preview(invoice_id, company, revenue_account_id=None, tax_account_id=None, tax_amount=None):
    """
    Computes and returns a prospective balanced double-entry preview for a sales invoice
    without committing any changes to the database.
    Shows:
      - Debit:  Accounts Receivable (Asset) = invoice.total_amount
      - Credit: Sales Revenue (Revenue)     = invoice.total_amount - tax_amount
      - Credit: Tax Payable (Liability)     = tax_amount (if > 0)
    """
    try:
        invoice = Invoice.objects.select_related("customer", "sales_order").prefetch_related("lines__item").get(
            pk=invoice_id, company=company
        )
    except Invoice.DoesNotExist:
        raise ValidationError(_(f"Invoice #{invoice_id} not found for this company."))

    total_amount = Decimal(str(invoice.total_amount))
    if total_amount <= Decimal("0.00"):
        raise ValidationError(_(f"Invoice total amount must be positive. Current total is {total_amount}."))

    tax_val = Decimal(str(tax_amount or "0.00"))
    if tax_val < Decimal("0.00"):
        raise ValidationError(_("Tax amount cannot be negative."))
    if tax_val > total_amount:
        raise ValidationError(_(f"Tax amount ({tax_val}) cannot exceed total invoice amount ({total_amount})."))

    net_sales = total_amount - tax_val

    ar_account = get_ar_account(company)
    
    if revenue_account_id:
        rev_account = Account.objects.filter(pk=revenue_account_id, company=company, is_active=True).first()
        if not rev_account or rev_account.is_header:
            raise ValidationError(_(f"Invalid revenue account selected."))
    else:
        rev_account = get_sales_revenue_account(company)

    tax_account = None
    if tax_val > Decimal("0.00"):
        tax_account = get_tax_payable_account(company, account_id=tax_account_id)

    lines_preview = [
        {
            "line_number": 1,
            "account_id": ar_account.id,
            "account_code": ar_account.code,
            "account_name": ar_account.name,
            "category": "asset",
            "type": "debit",
            "debit": float(total_amount),
            "credit": 0.0,
            "description": f"Accounts Receivable - INV-{invoice.id} ({invoice.customer.name})",
        },
        {
            "line_number": 2,
            "account_id": rev_account.id,
            "account_code": rev_account.code,
            "account_name": rev_account.name,
            "category": "revenue",
            "type": "credit",
            "debit": 0.0,
            "credit": float(net_sales),
            "description": f"Sales Revenue - INV-{invoice.id}",
        },
    ]

    if tax_val > Decimal("0.00") and tax_account:
        lines_preview.append({
            "line_number": 3,
            "account_id": tax_account.id,
            "account_code": tax_account.code,
            "account_name": tax_account.name,
            "category": "liability",
            "type": "credit",
            "debit": 0.0,
            "credit": float(tax_val),
            "description": f"Sales Tax Payable - INV-{invoice.id}",
        })

    total_debit = float(total_amount)
    total_credit = float(net_sales + tax_val)
    is_balanced = (round(total_debit, 2) == round(total_credit, 2))

    return {
        "invoice_id": invoice.id,
        "invoice_number": f"INV-{invoice.id}",
        "customer_name": invoice.customer.name,
        "invoice_date": invoice.invoice_date.isoformat(),
        "total_amount": float(total_amount),
        "net_sales": float(net_sales),
        "tax_amount": float(tax_val),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "is_balanced": is_balanced,
        "lines": lines_preview,
    }


def post_sales_invoice_to_accounting(
    invoice_id,
    user,
    company,
    revenue_account_id=None,
    tax_account_id=None,
    tax_amount=None,
):
    """
    Posts an operational sales invoice to the General Ledger via Double-Entry Engine (#8).
    Creates a balanced Journal Entry:
      - DEBIT:  Accounts Receivable (Asset) = invoice.total_amount
      - CREDIT: Sales Revenue (Revenue)     = invoice.total_amount - tax_amount
      - CREDIT: Tax Payable (Liability)     = tax_amount (if > 0)
    Enforces tenant isolation, period lock validation, and prevents duplicate posting.
    """
    with transaction.atomic():
        try:
            invoice = Invoice.objects.select_for_update().get(pk=invoice_id, company=company)
        except Invoice.DoesNotExist:
            raise ValidationError(_(f"Invoice #{invoice_id} not found in this company."))

        if invoice.status == "cancelled":
            raise ValidationError(_("Cannot post a cancelled invoice to accounting."))

        total_amount = Decimal(str(invoice.total_amount))
        if total_amount <= Decimal("0.00"):
            raise ValidationError(_(f"Invoice total amount must be positive. Current total is {total_amount}."))

        # Check duplicate posting
        existing_je = JournalEntry.objects.filter(
            company=company,
            source_module="sales.invoice",
            source_id=invoice.id,
            status="posted",
        ).first()
        if existing_je:
            raise ValidationError(_(
                f"Invoice #{invoice.id} has already been posted to the General Ledger "
                f"(Journal Entry: {existing_je.entry_number})."
            ))

        tax_val = Decimal(str(tax_amount or "0.00"))
        if tax_val < Decimal("0.00"):
            raise ValidationError(_("Tax amount cannot be negative."))
        if tax_val > total_amount:
            raise ValidationError(_(f"Tax amount ({tax_val}) cannot exceed total invoice amount ({total_amount})."))

        net_sales = total_amount - tax_val

        ar_account = get_ar_account(company)
        
        if revenue_account_id:
            rev_account = Account.objects.filter(pk=revenue_account_id, company=company, is_active=True).first()
            if not rev_account or rev_account.is_header:
                raise ValidationError(_(f"Invalid revenue account selected."))
        else:
            rev_account = get_sales_revenue_account(company)

        tax_account = None
        if tax_val > Decimal("0.00"):
            tax_account = get_tax_payable_account(company, account_id=tax_account_id)

        # Create draft journal entry
        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=invoice.invoice_date,
            reference=f"INV-{invoice.id}",
            description=f"Sales Invoice INV-{invoice.id} for {invoice.customer.name}",
            source_module="sales.invoice",
            source_id=invoice.id,
            created_by=user,
            status="draft",
        )

        # Line 1: Debit Accounts Receivable (Total Gross Receivable)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=ar_account,
            line_number=1,
            debit=total_amount,
            credit=Decimal("0.00"),
            description=f"Accounts Receivable - INV-{invoice.id} ({invoice.customer.name})",
        )

        # Line 2: Credit Sales Revenue (Net Sales)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=rev_account,
            line_number=2,
            debit=Decimal("0.00"),
            credit=net_sales,
            description=f"Sales Revenue - INV-{invoice.id}",
        )

        # Line 3: Credit Sales Tax Payable (if applicable)
        if tax_val > Decimal("0.00") and tax_account:
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=tax_account,
                line_number=3,
                debit=Decimal("0.00"),
                credit=tax_val,
                description=f"Sales Tax Payable - INV-{invoice.id}",
            )

        # Post atomically through #8 engine
        posted_entry = post_journal_entry(entry.id, user, company=company)

        # Synchronize customer's balance_due
        sync_customer_balance(invoice.customer)

        # Blueprint #19 Tax Layer Integration
        if tax_val > Decimal("0.00") and tax_account:
            try:
                from .tax import record_tax_line
                tax_rate_val = Decimal("0.0000")
                if net_sales > Decimal("0.00"):
                    tax_rate_val = (tax_val / net_sales * Decimal("100.0000")).quantize(Decimal("0.0001"))
                record_tax_line(
                    company=company,
                    source_module="sales",
                    source_id=str(invoice.id),
                    source_reference=f"INV-{invoice.id}",
                    taxable_amount=net_sales,
                    tax_rate=tax_rate_val,
                    tax_amount=tax_val,
                    transaction_date=invoice.invoice_date,
                    tax_account=tax_account,
                    journal_entry=posted_entry,
                    actor=user,
                )
            except Exception:
                pass

        return posted_entry


def reverse_sales_invoice_accounting(invoice_id, user, company, reason=""):
    """
    Reverses an accounting-posted sales invoice using the #8 Reversal Engine.
    Also transitions the invoice status to 'cancelled' and re-synchronizes the customer balance.
    """
    with transaction.atomic():
        try:
            invoice = Invoice.objects.select_for_update().get(pk=invoice_id, company=company)
        except Invoice.DoesNotExist:
            raise ValidationError(_(f"Invoice #{invoice_id} not found in this company."))

        if invoice.amount_paid > Decimal("0.00"):
            raise ValidationError(_(
                f"Cannot reverse invoice INV-{invoice.id} because payments totaling {invoice.amount_paid} "
                "have already been applied. Reverse applied payments first."
            ))

        existing_je = JournalEntry.objects.filter(
            company=company,
            source_module="sales.invoice",
            source_id=invoice.id,
            status="posted",
        ).first()

        reversal_je = None
        if existing_je:
            reversal_reason = reason or f"Cancelled Sales Invoice INV-{invoice.id}"
            eff_date = existing_je.transaction_date
            reversal_je = reverse_journal_entry(
                entry_id=existing_je.id,
                user=user,
                reason=reversal_reason,
                reversal_date=eff_date,
                company=company,
            )

        # Update invoice status to cancelled
        invoice.status = "cancelled"
        invoice.save(update_fields=["status"])

        # Re-synchronize customer balance
        sync_customer_balance(invoice.customer)

        # Blueprint #19 Tax Layer Integration: mark tax lines as reversed
        try:
            from .models import TaxTransactionLine
            TaxTransactionLine.objects.filter(
                company=company,
                source_module="sales",
                source_id=str(invoice.id),
                is_reversed=False,
            ).update(
                is_reversed=True,
                reversed_at=timezone.now(),
                reversal_reference="Reversed via invoice cancellation",
                reversal_journal_entry=reversal_je,
            )
        except Exception:
            pass

        return {
            "invoice_id": invoice.id,
            "status": invoice.status,
            "reversal_journal_entry": reversal_je,
        }


def get_sales_accounting_summary(company, as_of_date=None):
    """
    Computes aggregate metrics for the Sales-to-Accounting subledger:
      - Total Invoices count & amount
      - Posted Sales Invoices count & amount
      - Unposted Invoices count & amount
      - Cancelled Invoices count & amount
      - GL Sales Revenue balance
      - GL Tax Payable balance
      - GL Accounts Receivable balance
    """
    target_date = as_of_date or timezone.localdate()
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    invoices = Invoice.objects.filter(company=company)

    total_count = invoices.count()
    total_amount = invoices.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    # Posted invoices are those with a posted journal entry
    posted_invoice_ids = JournalEntry.objects.filter(
        company=company,
        source_module="sales.invoice",
        status="posted",
    ).values_list("source_id", flat=True)

    posted_invoices = invoices.filter(id__in=posted_invoice_ids)
    posted_count = posted_invoices.count()
    posted_amount = posted_invoices.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    unposted_invoices = invoices.exclude(id__in=posted_invoice_ids).exclude(status="cancelled")
    unposted_count = unposted_invoices.count()
    unposted_amount = unposted_invoices.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    cancelled_invoices = invoices.filter(status="cancelled")
    cancelled_count = cancelled_invoices.count()
    cancelled_amount = cancelled_invoices.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    # GL balances from posted journal entries
    # 1. Accounts Receivable (code 1100 or type AR)
    ar_acc = Account.objects.filter(company=company, code="1100").first()
    ar_balance = Decimal("0.00")
    if ar_acc:
        lines = JournalEntryLine.objects.filter(
            journal_entry__company=company,
            journal_entry__status="posted",
            account=ar_acc,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        ar_balance = (lines["dr"] or Decimal("0.00")) - (lines["cr"] or Decimal("0.00"))

    # 2. Sales Revenue (code 4010 or type Operating Sales Revenue)
    rev_lines = JournalEntryLine.objects.filter(
        journal_entry__company=company,
        journal_entry__status="posted",
        account__account_type__category="revenue",
    ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
    gl_sales_revenue = (rev_lines["cr"] or Decimal("0.00")) - (rev_lines["dr"] or Decimal("0.00"))

    # 3. Tax Payable (code 2200 or tax account)
    tax_acc = Account.objects.filter(company=company, code="2200").first()
    gl_tax_payable = Decimal("0.00")
    if tax_acc:
        tax_lines = JournalEntryLine.objects.filter(
            journal_entry__company=company,
            journal_entry__status="posted",
            account=tax_acc,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        gl_tax_payable = (tax_lines["cr"] or Decimal("0.00")) - (tax_lines["dr"] or Decimal("0.00"))

    return {
        "as_of_date": target_date.isoformat(),
        "total_invoices_count": total_count,
        "total_invoices_amount": float(total_amount),
        "posted_invoices_count": posted_count,
        "posted_invoices_amount": float(posted_amount),
        "unposted_invoices_count": unposted_count,
        "unposted_invoices_amount": float(unposted_amount),
        "cancelled_invoices_count": cancelled_count,
        "cancelled_invoices_amount": float(cancelled_amount),
        "gl_sales_revenue": float(gl_sales_revenue),
        "gl_tax_payable": float(gl_tax_payable),
        "gl_accounts_receivable": float(ar_balance),
    }


def get_sales_accounting_invoices(company, search=None, status_filter=None, posted_filter=None):
    """
    Returns the list of sales invoices decorated with accounting posting status and journal entry details.
    """
    queryset = Invoice.objects.filter(company=company).select_related("customer", "sales_order").prefetch_related("lines__item")

    if search:
        queryset = queryset.filter(
            Q(customer__name__icontains=search) |
            Q(id__icontains=search.replace("INV-", "").replace("inv-", "")) |
            Q(sales_order__id__icontains=search.replace("SO-", "").replace("so-", ""))
        )

    if status_filter and status_filter != "all":
        queryset = queryset.filter(status=status_filter)

    # Pre-fetch posted journal entries for sales invoices in this company
    posted_jes = {
        je.source_id: je
        for je in JournalEntry.objects.filter(
            company=company,
            source_module="sales.invoice",
        ).select_related("created_by")
    }

    invoices_data = []
    for inv in queryset:
        je = posted_jes.get(inv.id)
        
        accounting_status = "not_posted"
        je_info = None

        if je:
            if je.status == "posted":
                accounting_status = "posted"
            elif je.status == "reversed":
                accounting_status = "reversed"
            else:
                accounting_status = je.status

            je_info = {
                "journal_entry_id": je.id,
                "entry_number": je.entry_number,
                "transaction_date": je.transaction_date.isoformat(),
                "status": je.status,
            }
        elif inv.status == "cancelled":
            accounting_status = "cancelled"

        # Apply posted filter
        if posted_filter == "posted" and accounting_status != "posted":
            continue
        if posted_filter == "unposted" and accounting_status != "not_posted":
            continue
        if posted_filter == "reversed" and accounting_status != "reversed":
            continue

        invoices_data.append({
            "id": inv.id,
            "invoice_number": f"INV-{inv.id}",
            "sales_order_id": inv.sales_order.id if inv.sales_order else None,
            "sales_order_number": f"SO-{inv.sales_order.id}" if inv.sales_order else "-",
            "customer_id": inv.customer.id,
            "customer_name": inv.customer.name,
            "invoice_date": inv.invoice_date.isoformat(),
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
            "total_amount": float(inv.total_amount),
            "amount_paid": float(inv.amount_paid),
            "balance_due": float(inv.balance_due),
            "status": inv.status,
            "accounting_status": accounting_status,
            "journal_entry": je_info,
            "lines_count": inv.lines.count(),
        })

    return invoices_data
