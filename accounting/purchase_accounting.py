"""
Purchase-to-Accounting Integration Subledger (Blueprint Section No. 13)
Connects operational procurement vendor bills, bill line items, material/expense allocation,
input tax accounting, and Accounts Payable (#11) directly to the Double-Entry Engine (#8) and General Ledger (#9).
"""

from decimal import Decimal
from datetime import date, datetime
from django.db import transaction
from django.db.models import Sum, Q, F
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import Company
from procurement.models import Vendor, Bill, BillLine, VendorPayment
from accounting.models import Account, AccountType, JournalEntry, JournalEntryLine, AccountingSettings, AccountingPeriod
from accounting.engine import post_journal_entry, reverse_journal_entry
from accounting.payables import (
    get_ap_account,
    get_bill_expense_account,
    get_bank_account,
    sync_vendor_balance,
)


def get_input_tax_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Input Tax Recoverable asset account for the tenant.
    Prefers code '1310', then account containing 'Input Tax' or 'Tax Recoverable' under Current Assets.
    """
    if account_id:
        acc = Account.objects.filter(
            pk=account_id,
            company=company,
            is_active=True,
        ).first()
        if not acc:
            raise ValidationError(_(f"Specified input tax account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post input tax to a header account. Select a leaf account."))
        return acc

    # 1. Direct code match: 1310
    acc = Account.objects.filter(
        company=company,
        code="1310",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    # 2. Account with name containing 'Input Tax' or 'Tax Recoverable' under asset
    acc = Account.objects.filter(
        company=company,
        name__icontains="Input Tax",
        account_type__category="asset",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    # 3. Create standard leaf account '1310 Input Tax Recoverable' under 1000 Current Assets
    parent = Account.objects.filter(company=company, code="1000").first()
    acc_type = AccountType.objects.filter(name="Prepaid Expenses & Other Current Assets").first()
    if not acc_type:
        acc_type = AccountType.objects.filter(category="asset").first()

    acc, _ = Account.objects.get_or_create(
        company=company,
        code="1310",
        defaults={
            "name": "Input Tax Recoverable",
            "account_type": acc_type,
            "parent": parent,
            "description": "Recoverable input GST/VAT paid on supplier materials and commercial expenses",
            "currency": getattr(company, "currency", "USD") or "USD",
            "is_active": True,
        }
    )
    return acc


def get_purchase_accounting_preview(bill_id, company, expense_account_id=None, tax_account_id=None, tax_amount=None):
    """
    Computes and returns a prospective balanced double-entry preview for a purchase bill
    without committing any changes to the database.
    Shows:
      - Debit:  Purchase / Expense / Inventory (Asset/Expense) = bill.total_amount - tax_amount
      - Debit:  Input Tax Recoverable (Asset)                 = tax_amount (if > 0)
      - Credit: Accounts Payable (Liability)                  = bill.total_amount
    """
    try:
        bill = Bill.objects.select_related("vendor", "purchase_order").prefetch_related("lines__item").get(
            pk=bill_id, company=company
        )
    except Bill.DoesNotExist:
        raise ValidationError(_(f"Purchase Bill #{bill_id} not found for this company."))

    total_amount = Decimal(str(bill.total_amount))
    if total_amount <= Decimal("0.00"):
        raise ValidationError(_(f"Bill total amount must be positive. Current total is {total_amount}."))

    tax_val = Decimal(str(tax_amount or "0.00"))
    if tax_val < Decimal("0.00"):
        raise ValidationError(_("Tax amount cannot be negative."))
    if tax_val > total_amount:
        raise ValidationError(_(f"Tax amount ({tax_val}) cannot exceed total bill amount ({total_amount})."))

    net_purchase = total_amount - tax_val

    ap_account = get_ap_account(company)
    expense_account = get_bill_expense_account(company, account_id=expense_account_id)

    lines_preview = [
        {
            "line_number": 1,
            "account_id": expense_account.id,
            "account_code": expense_account.code,
            "account_name": expense_account.name,
            "category": expense_account.account_type.category,
            "type": "debit",
            "debit": float(net_purchase),
            "credit": 0.0,
            "description": f"Purchase/Inventory - Bill {bill.bill_number or bill.id} ({bill.vendor.name})",
        },
    ]

    line_idx = 2
    if tax_val > Decimal("0.00"):
        tax_account = get_input_tax_account(company, account_id=tax_account_id)
        lines_preview.append({
            "line_number": line_idx,
            "account_id": tax_account.id,
            "account_code": tax_account.code,
            "account_name": tax_account.name,
            "category": "asset",
            "type": "debit",
            "debit": float(tax_val),
            "credit": 0.0,
            "description": f"Input Tax Recoverable - Bill {bill.bill_number or bill.id}",
        })
        line_idx += 1

    lines_preview.append({
        "line_number": line_idx,
        "account_id": ap_account.id,
        "account_code": ap_account.code,
        "account_name": ap_account.name,
        "category": "liability",
        "type": "credit",
        "debit": 0.0,
        "credit": float(total_amount),
        "description": f"Accounts Payable - Bill {bill.bill_number or bill.id} ({bill.vendor.name})",
    })

    total_debit = float(net_purchase + tax_val)
    total_credit = float(total_amount)
    is_balanced = (round(total_debit, 2) == round(total_credit, 2))

    return {
        "bill_id": bill.id,
        "bill_number": bill.bill_number or f"BILL-{bill.id}",
        "vendor_name": bill.vendor.name,
        "bill_date": bill.bill_date.isoformat(),
        "total_amount": float(total_amount),
        "net_purchase": float(net_purchase),
        "tax_amount": float(tax_val),
        "total_debit": total_debit,
        "total_credit": total_credit,
        "is_balanced": is_balanced,
        "lines": lines_preview,
    }


def post_purchase_to_accounting(
    bill_id,
    user,
    company,
    expense_account_id=None,
    tax_account_id=None,
    tax_amount=None,
):
    """
    Posts an operational vendor purchase bill to the General Ledger via Double-Entry Engine (#8).
    Creates a balanced Journal Entry:
      - DEBIT:  Purchase / Inventory / Expense (Asset/Expense) = bill.total_amount - tax_amount
      - DEBIT:  Input Tax Recoverable (Asset)                 = tax_amount (if > 0)
      - CREDIT: Accounts Payable (Liability)                  = bill.total_amount
    Enforces tenant isolation, period lock validation, and prevents duplicate posting.
    """
    with transaction.atomic():
        try:
            bill = Bill.objects.select_for_update().get(pk=bill_id, company=company)
        except Bill.DoesNotExist:
            raise ValidationError(_(f"Purchase Bill #{bill_id} not found in this company."))

        if bill.status == "cancelled":
            raise ValidationError(_("Cannot post a cancelled bill to accounting."))

        total_amount = Decimal(str(bill.total_amount))
        if total_amount <= Decimal("0.00"):
            raise ValidationError(_(f"Bill total amount must be positive. Current total is {total_amount}."))

        # Check duplicate posting
        existing_je = JournalEntry.objects.filter(
            company=company,
            source_module="procurement.bill",
            source_id=bill.id,
            status="posted",
        ).first()
        if existing_je:
            raise ValidationError(_(
                f"Bill #{bill.id} has already been posted to the General Ledger "
                f"(Journal Entry: {existing_je.entry_number})."
            ))

        tax_val = Decimal(str(tax_amount or "0.00"))
        if tax_val < Decimal("0.00"):
            raise ValidationError(_("Tax amount cannot be negative."))
        if tax_val > total_amount:
            raise ValidationError(_(f"Tax amount ({tax_val}) cannot exceed total bill amount ({total_amount})."))

        net_purchase = total_amount - tax_val

        ap_account = get_ap_account(company)
        expense_account = get_bill_expense_account(company, account_id=expense_account_id)

        # Create draft journal entry
        ref_text = bill.bill_number or f"BILL-{bill.id}"
        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=bill.bill_date,
            reference=ref_text,
            description=f"Vendor Bill {ref_text} from {bill.vendor.name}",
            source_module="procurement.bill",
            source_id=bill.id,
            created_by=user,
            status="draft",
        )

        # Line 1: Debit Purchase / Expense / Inventory (Net Amount)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=expense_account,
            line_number=1,
            debit=net_purchase,
            credit=Decimal("0.00"),
            description=f"Purchase Expense/Inventory - {ref_text} ({bill.vendor.name})",
        )

        line_idx = 2
        # Line 2 (Optional): Debit Input Tax Recoverable
        if tax_val > Decimal("0.00"):
            tax_account = get_input_tax_account(company, account_id=tax_account_id)
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=tax_account,
                line_number=line_idx,
                debit=tax_val,
                credit=Decimal("0.00"),
                description=f"Input Tax Recoverable - {ref_text}",
            )
            line_idx += 1

        # Line 3: Credit Accounts Payable (Gross Total Amount)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=ap_account,
            line_number=line_idx,
            debit=Decimal("0.00"),
            credit=total_amount,
            description=f"Accounts Payable - {ref_text} ({bill.vendor.name})",
        )

        # Post atomically through #8 engine
        posted_entry = post_journal_entry(entry.id, user, company=company)

        # Synchronize vendor's outstanding_balance
        sync_vendor_balance(bill.vendor)

        return posted_entry


def reverse_purchase_accounting(bill_id, user, company, reason=""):
    """
    Reverses an accounting-posted vendor bill using the #8 Reversal Engine.
    Also transitions the bill status to 'cancelled' and re-synchronizes the vendor balance.
    """
    with transaction.atomic():
        try:
            bill = Bill.objects.select_for_update().get(pk=bill_id, company=company)
        except Bill.DoesNotExist:
            raise ValidationError(_(f"Bill #{bill_id} not found in this company."))

        if bill.amount_paid > Decimal("0.00"):
            raise ValidationError(_(
                f"Cannot reverse bill #{bill.id} because payments totaling {bill.amount_paid} "
                "have already been applied. Reverse applied vendor payments first."
            ))

        existing_je = JournalEntry.objects.filter(
            company=company,
            source_module="procurement.bill",
            source_id=bill.id,
            status="posted",
        ).first()

        reversal_je = None
        if existing_je:
            reversal_reason = reason or f"Cancelled Vendor Bill {bill.bill_number or bill.id}"
            eff_date = existing_je.transaction_date
            reversal_je = reverse_journal_entry(
                entry_id=existing_je.id,
                user=user,
                reason=reversal_reason,
                reversal_date=eff_date,
                company=company,
            )

        # Update bill status to cancelled
        bill.status = "cancelled"
        bill.save(update_fields=["status"])

        # Re-synchronize vendor balance
        sync_vendor_balance(bill.vendor)

        return {
            "bill_id": bill.id,
            "status": bill.status,
            "reversal_journal_entry": reversal_je,
        }


def get_purchase_accounting_summary(company, as_of_date=None):
    """
    Computes aggregate metrics for the Purchase-to-Accounting subledger:
      - Total Bills count & amount
      - Posted Bills count & amount
      - Unposted Bills count & amount
      - Cancelled Bills count & amount
      - GL Purchase/Expense/Inventory net debits
      - GL Input Tax Recoverable net debit balance
      - GL Accounts Payable credit balance
    """
    target_date = as_of_date or timezone.localdate()
    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    bills = Bill.objects.filter(company=company)

    total_count = bills.count()
    total_amount = bills.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    posted_bill_ids = JournalEntry.objects.filter(
        company=company,
        source_module="procurement.bill",
        status="posted",
    ).values_list("source_id", flat=True)

    posted_bills = bills.filter(id__in=posted_bill_ids)
    posted_count = posted_bills.count()
    posted_amount = posted_bills.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    unposted_bills = bills.exclude(id__in=posted_bill_ids).exclude(status="cancelled")
    unposted_count = unposted_bills.count()
    unposted_amount = unposted_bills.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    cancelled_bills = bills.filter(status="cancelled")
    cancelled_count = cancelled_bills.count()
    cancelled_amount = cancelled_bills.aggregate(total=Sum("total_amount"))["total"] or Decimal("0.00")

    # GL balances from posted journal entries
    # 1. Accounts Payable (code 2010 or type AP)
    ap_acc = Account.objects.filter(company=company, code="2010").first()
    gl_accounts_payable = Decimal("0.00")
    if ap_acc:
        lines = JournalEntryLine.objects.filter(
            journal_entry__company=company,
            journal_entry__status="posted",
            account=ap_acc,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        gl_accounts_payable = (lines["cr"] or Decimal("0.00")) - (lines["dr"] or Decimal("0.00"))

    # 2. Input Tax Recoverable (code 1310 or input tax account)
    tax_acc = Account.objects.filter(company=company, code="1310").first()
    gl_input_tax = Decimal("0.00")
    if tax_acc:
        tax_lines = JournalEntryLine.objects.filter(
            journal_entry__company=company,
            journal_entry__status="posted",
            account=tax_acc,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        gl_input_tax = (tax_lines["dr"] or Decimal("0.00")) - (tax_lines["cr"] or Decimal("0.00"))

    # 3. Purchase Expense / Raw Materials (codes 1210, 5010, or material/expense accounts)
    exp_lines = JournalEntryLine.objects.filter(
        journal_entry__company=company,
        journal_entry__status="posted",
        account__code__in=["1210", "5010"],
    ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
    gl_purchase_expense = (exp_lines["dr"] or Decimal("0.00")) - (exp_lines["cr"] or Decimal("0.00"))

    return {
        "as_of_date": target_date.isoformat(),
        "total_bills_count": total_count,
        "total_bills_amount": float(total_amount),
        "posted_bills_count": posted_count,
        "posted_bills_amount": float(posted_amount),
        "unposted_bills_count": unposted_count,
        "unposted_bills_amount": float(unposted_amount),
        "cancelled_bills_count": cancelled_count,
        "cancelled_bills_amount": float(cancelled_amount),
        "gl_purchase_expense": float(gl_purchase_expense),
        "gl_input_tax": float(gl_input_tax),
        "gl_accounts_payable": float(gl_accounts_payable),
    }


def get_purchase_accounting_bills(company, search=None, status_filter=None, posted_filter=None):
    """
    Returns the list of purchase bills decorated with accounting posting status and journal entry details.
    """
    queryset = Bill.objects.filter(company=company).select_related("vendor", "purchase_order").prefetch_related("lines__item")

    if search:
        queryset = queryset.filter(
            Q(vendor__name__icontains=search) |
            Q(bill_number__icontains=search) |
            Q(id__icontains=search.replace("BILL-", "").replace("bill-", "")) |
            Q(purchase_order__id__icontains=search.replace("PO-", "").replace("po-", ""))
        )

    if status_filter and status_filter != "all":
        queryset = queryset.filter(status=status_filter)

    # Pre-fetch posted journal entries for bills in this company
    posted_jes = {
        je.source_id: je
        for je in JournalEntry.objects.filter(
            company=company,
            source_module="procurement.bill",
        ).select_related("created_by")
    }

    bills_data = []
    for bill in queryset:
        je = posted_jes.get(bill.id)

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
        elif bill.status == "cancelled":
            accounting_status = "cancelled"

        # Apply posted filter
        if posted_filter == "posted" and accounting_status != "posted":
            continue
        if posted_filter == "unposted" and accounting_status != "not_posted":
            continue
        if posted_filter == "reversed" and accounting_status != "reversed":
            continue

        bills_data.append({
            "id": bill.id,
            "bill_number": bill.bill_number or f"BILL-{bill.id}",
            "purchase_order_id": bill.purchase_order.id if bill.purchase_order else None,
            "purchase_order_number": f"PO-{bill.purchase_order.id}" if bill.purchase_order else "-",
            "vendor_id": bill.vendor.id,
            "vendor_name": bill.vendor.name,
            "bill_date": bill.bill_date.isoformat(),
            "due_date": bill.due_date.isoformat() if bill.due_date else None,
            "total_amount": float(bill.total_amount),
            "amount_paid": float(bill.amount_paid),
            "balance_due": float(bill.balance_due),
            "status": bill.status,
            "accounting_status": accounting_status,
            "journal_entry": je_info,
            "lines_count": bill.lines.count(),
        })

    return bills_data
