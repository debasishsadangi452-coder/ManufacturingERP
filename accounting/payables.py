"""
Accounts Payable (AP) Subledger & Accounting Integration (Blueprint Section No. 11)
Provides vendor payables ledger, deterministic AP aging calculations,
payment allocation, and atomic double-entry posting through the Double-Entry Engine (#8).
"""

from decimal import Decimal
from datetime import date, datetime
from django.db import transaction
from django.db.models import Sum, Q, F
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import Company
from procurement.models import Vendor, Bill, VendorPayment
from accounting.models import Account, JournalEntry, JournalEntryLine, AccountingSettings, AccountingPeriod
from accounting.engine import post_journal_entry


def get_ap_account(company):
    """
    Resolves the primary active Accounts Payable leaf liability account for the tenant.
    Prefers standard code '2010', then account type 'Accounts Payable'.
    """
    acc = Account.objects.filter(
        company=company,
        code="2010",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    acc = Account.objects.filter(
        company=company,
        account_type__name="Accounts Payable",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    raise ValidationError(_(
        f"No active leaf Accounts Payable account found for company '{company.name}'. "
        "Ensure account code 2010 or an Accounts Payable account exists in your Chart of Accounts."
    ))


def get_bill_expense_account(company, account_id=None):
    """
    Resolves the active leaf account to debit upon recording a vendor bill.
    In manufacturing, raw material purchases typically debit Raw Materials Inventory (1210)
    or Direct Raw Materials Consumed / Purchase Expense (5010).
    """
    if account_id:
        acc = Account.objects.filter(
            pk=account_id,
            company=company,
            is_active=True,
        ).first()
        if not acc:
            raise ValidationError(_(f"Specified expense/inventory account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post bills to a header account. Select a leaf expense/inventory account."))
        return acc

    # 1. Prefer Raw Materials Inventory (1210)
    acc = Account.objects.filter(
        company=company,
        code="1210",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    # 2. Prefer Direct Raw Materials Consumed (5010)
    acc = Account.objects.filter(
        company=company,
        code="5010",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    # 3. Fallback: Any active leaf Inventory or Expense account
    acc = Account.objects.filter(
        company=company,
        account_type__category__in=["asset", "expense"],
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    raise ValidationError(_(
        f"No active leaf Inventory or Expense account found for company '{company.name}'. "
        "Ensure account code 1210, 5010, or an active Inventory/Expense account exists."
    ))


def get_bank_account(company, account_id=None):
    """
    Resolves the active Cash/Bank leaf account for vendor payment disbursement.
    If account_id is provided, validates that it belongs to the tenant and is active.
    Otherwise defaults to code '1010' or 'Cash & Cash Equivalents'.
    """
    if account_id:
        acc = Account.objects.filter(
            pk=account_id,
            company=company,
            is_active=True,
        ).first()
        if not acc:
            raise ValidationError(_(f"Specified bank/cash account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post payments to a header account. Select a leaf bank/cash account."))
        return acc

    acc = Account.objects.filter(
        company=company,
        code="1010",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    acc = Account.objects.filter(
        company=company,
        account_type__name="Cash & Cash Equivalents",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    raise ValidationError(_(
        f"No active leaf Bank/Cash account found for company '{company.name}'. "
        "Ensure account code 1010 or a Cash & Cash Equivalents account exists."
    ))


def post_bill_to_ap(bill_id, user, company, expense_account_id=None):
    """
    Posts an operational vendor bill to the General Ledger via Double-Entry Engine (#8).
    Balanced Journal Entry:
      - DEBIT:  Inventory / Purchase Expense (Asset/Expense)
      - CREDIT: Accounts Payable (Liability)
    Enforces tenant isolation, period lock validation, and prevents duplicate posting.
    """
    with transaction.atomic():
        try:
            bill = Bill.objects.select_for_update().get(pk=bill_id, company=company)
        except Bill.DoesNotExist:
            raise ValidationError(_(f"Bill #{bill_id} not found in this company."))

        if bill.status == "cancelled":
            raise ValidationError(_("Cannot post a cancelled bill to Accounts Payable."))

        if bill.total_amount <= Decimal("0.00"):
            raise ValidationError(_(f"Bill total amount must be positive. Current total is {bill.total_amount}."))

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

        ap_account = get_ap_account(company)
        expense_account = get_bill_expense_account(company, account_id=expense_account_id)

        # Create draft journal entry
        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=bill.bill_date,
            reference=bill.bill_number or f"BILL-{bill.id}",
            description=f"Vendor Bill {bill.bill_number or bill.id} from {bill.vendor.name}",
            source_module="procurement.bill",
            source_id=bill.id,
            created_by=user,
            status="draft",
        )

        # Debit Expense / Inventory
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=expense_account,
            line_number=1,
            debit=bill.total_amount,
            credit=Decimal("0.00"),
            description=f"Purchase - Bill {bill.bill_number or bill.id} ({bill.vendor.name})",
        )

        # Credit Accounts Payable
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=ap_account,
            line_number=2,
            debit=Decimal("0.00"),
            credit=bill.total_amount,
            description=f"AP - Bill {bill.bill_number or bill.id} ({bill.vendor.name})",
        )

        # Post atomically through #8 engine
        posted_entry = post_journal_entry(entry.id, user, company=company)

        # Synchronize vendor's outstanding_balance
        sync_vendor_balance(bill.vendor)

        return posted_entry


def record_and_allocate_ap_payment(
    vendor_id,
    amount,
    user,
    company,
    payment_date=None,
    method="bank_transfer",
    reference="",
    allocations=None,
    bank_account_id=None,
):
    """
    Records a vendor payment, allocates it across outstanding bill(s),
    updates bill balances/statuses, and posts a balanced Journal Entry to GL:
      - DEBIT:  Accounts Payable (Liability Decrease)
      - CREDIT: Operating Bank/Cash (Asset Decrease)
    """
    with transaction.atomic():
        try:
            vendor = Vendor.objects.select_for_update().get(pk=vendor_id, company=company)
        except Vendor.DoesNotExist:
            raise ValidationError(_(f"Vendor #{vendor_id} not found in this company."))

        payment_amount = Decimal(str(amount))
        if payment_amount <= Decimal("0.00"):
            raise ValidationError(_("Payment amount must be greater than zero."))

        effective_date = payment_date or timezone.localdate()
        if isinstance(effective_date, str):
            effective_date = date.fromisoformat(effective_date)

        # Normalize allocations: list of {"bill_id": id, "amount": decimal}
        if not allocations:
            raise ValidationError(_("At least one bill allocation is required for vendor payment processing."))

        total_allocated = Decimal("0.00")
        processed_payments = []

        for alloc in allocations:
            bill_id = alloc.get("bill_id")
            alloc_amt = Decimal(str(alloc.get("amount", "0")))

            if alloc_amt <= Decimal("0.00"):
                continue

            try:
                bill = Bill.objects.select_for_update().get(
                    pk=bill_id,
                    vendor=vendor,
                    company=company,
                )
            except Bill.DoesNotExist:
                raise ValidationError(_(f"Bill #{bill_id} not found for vendor '{vendor.name}'."))

            if bill.status in ("paid", "cancelled"):
                raise ValidationError(_(f"Bill {bill.bill_number or bill.id} is already marked as {bill.status}."))

            if alloc_amt > bill.balance_due:
                raise ValidationError(_(
                    f"Allocated amount {alloc_amt} exceeds balance due ({bill.balance_due}) for bill {bill.bill_number or bill.id}."
                ))

            # Apply operational payment
            bill.apply_payment(alloc_amt)
            total_allocated += alloc_amt

            pmt = VendorPayment.objects.create(
                company=company,
                vendor=vendor,
                bill=bill,
                amount=alloc_amt,
                payment_date=effective_date,
                method=method,
                reference=reference,
            )
            processed_payments.append((pmt, bill, alloc_amt))

        if total_allocated <= Decimal("0.00"):
            raise ValidationError(_("Total allocated payment amount must be greater than zero."))

        if total_allocated != payment_amount:
            raise ValidationError(_(
                f"Sum of allocations ({total_allocated}) does not match the payment amount ({payment_amount})."
            ))

        # Accounting Journal Entry for the Vendor Payment
        bank_account = get_bank_account(company, account_id=bank_account_id)
        ap_account = get_ap_account(company)

        bill_refs = ", ".join([f"BILL-{bill.id}" if not bill.bill_number else bill.bill_number for _, bill, _ in processed_payments])
        ref_text = reference or f"VPMT-{vendor.id}-{effective_date.strftime('%Y%m%d')}"

        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=effective_date,
            reference=ref_text,
            description=f"Vendor Payment to {vendor.name} applied to {bill_refs} ({method})",
            source_module="procurement.payment",
            source_id=processed_payments[0][0].id if processed_payments else None,
            created_by=user,
            status="draft",
        )

        # Debit Accounts Payable (Liability Decrease)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=ap_account,
            line_number=1,
            debit=payment_amount,
            credit=Decimal("0.00"),
            description=f"AP reduction - {vendor.name} ({bill_refs})",
        )

        # Credit Bank Account (Cash Decrease)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=bank_account,
            line_number=2,
            debit=Decimal("0.00"),
            credit=payment_amount,
            description=f"Payment disbursement - {vendor.name} ({bill_refs})",
        )

        posted_entry = post_journal_entry(entry.id, user, company=company)

        # Update vendor outstanding balance
        sync_vendor_balance(vendor)

        return {
            "journal_entry": posted_entry,
            "payments": [p.id for p, _, _ in processed_payments],
            "total_allocated": total_allocated,
        }


def sync_vendor_balance(vendor):
    """
    Recalculates and persists vendor.outstanding_balance from all open/partial bills.
    """
    total_due = Bill.objects.filter(
        vendor=vendor,
        status__in=["open", "partial"]
    ).aggregate(
        total=Sum(F("total_amount") - F("amount_paid"))
    )["total"] or Decimal("0.00")

    vendor.outstanding_balance = max(Decimal("0.00"), total_due)
    vendor.save(update_fields=["outstanding_balance"])
    return vendor.outstanding_balance


def calculate_bill_aging(bill, as_of_date=None):
    """
    Determines days overdue and deterministic bucket for an individual vendor bill.
    Buckets:
      - 'current': due_date >= as_of_date (not overdue)
      - '1_30': 1 to 30 days overdue
      - '31_60': 31 to 60 days overdue
      - '61_90': 61 to 90 days overdue
      - '90_plus': > 90 days overdue
    """
    ref_date = as_of_date or timezone.localdate()
    if isinstance(ref_date, str):
        ref_date = date.fromisoformat(ref_date)

    due = bill.due_date or bill.bill_date
    days_overdue = (ref_date - due).days

    if days_overdue <= 0:
        bucket = "current"
        bucket_label = "Current"
    elif 1 <= days_overdue <= 30:
        bucket = "1_30"
        bucket_label = "1–30 Days"
    elif 31 <= days_overdue <= 60:
        bucket = "31_60"
        bucket_label = "31–60 Days"
    elif 61 <= days_overdue <= 90:
        bucket = "61_90"
        bucket_label = "61–90 Days"
    else:
        bucket = "90_plus"
        bucket_label = "90+ Days"

    return {
        "days_overdue": max(0, days_overdue),
        "is_overdue": days_overdue > 0,
        "bucket": bucket,
        "bucket_label": bucket_label,
    }


def get_ap_aging_report(company, as_of_date=None):
    """
    Calculates deterministic AP aging breakdown across all open/partial vendor bills.
    Returns aggregated bucket totals and vendor-wise breakdowns.
    """
    ref_date = as_of_date or timezone.localdate()
    if isinstance(ref_date, str):
        ref_date = date.fromisoformat(ref_date)

    bills = Bill.objects.filter(
        company=company,
        status__in=["open", "partial"],
    ).select_related("vendor").order_by("due_date", "bill_date")

    totals = {
        "total_payables": Decimal("0.00"),
        "current": Decimal("0.00"),
        "days_1_30": Decimal("0.00"),
        "days_31_60": Decimal("0.00"),
        "days_61_90": Decimal("0.00"),
        "days_90_plus": Decimal("0.00"),
        "overdue_total": Decimal("0.00"),
        "bill_count": 0,
    }

    vendor_map = {}

    for b in bills:
        bal = b.balance_due
        if bal <= Decimal("0.00"):
            continue

        aging = calculate_bill_aging(b, as_of_date=ref_date)
        bucket = aging["bucket"]

        totals["total_payables"] += bal
        totals["bill_count"] += 1

        if bucket == "current":
            totals["current"] += bal
        elif bucket == "1_30":
            totals["days_1_30"] += bal
            totals["overdue_total"] += bal
        elif bucket == "31_60":
            totals["days_31_60"] += bal
            totals["overdue_total"] += bal
        elif bucket == "61_90":
            totals["days_61_90"] += bal
            totals["overdue_total"] += bal
        elif bucket == "90_plus":
            totals["days_90_plus"] += bal
            totals["overdue_total"] += bal

        v = b.vendor
        if v.id not in vendor_map:
            vendor_map[v.id] = {
                "vendor_id": v.id,
                "vendor_name": v.name,
                "category": v.category,
                "email": v.email,
                "phone": v.phone,
                "payment_terms": v.payment_terms or "Net 30",
                "total_balance": Decimal("0.00"),
                "current": Decimal("0.00"),
                "days_1_30": Decimal("0.00"),
                "days_31_60": Decimal("0.00"),
                "days_61_90": Decimal("0.00"),
                "days_90_plus": Decimal("0.00"),
                "overdue_balance": Decimal("0.00"),
                "bill_count": 0,
            }

        v_data = vendor_map[v.id]
        v_data["total_balance"] += bal
        v_data["bill_count"] += 1

        if bucket == "current":
            v_data["current"] += bal
        elif bucket == "1_30":
            v_data["days_1_30"] += bal
            v_data["overdue_balance"] += bal
        elif bucket == "31_60":
            v_data["days_31_60"] += bal
            v_data["overdue_balance"] += bal
        elif bucket == "61_90":
            v_data["days_61_90"] += bal
            v_data["overdue_balance"] += bal
        elif bucket == "90_plus":
            v_data["days_90_plus"] += bal
            v_data["overdue_balance"] += bal

    vendor_list = sorted(vendor_map.values(), key=lambda x: x["total_balance"], reverse=True)

    return {
        "as_of_date": ref_date.isoformat(),
        "totals": totals,
        "vendors": vendor_list,
    }


def get_ap_summary(company, as_of_date=None):
    """
    Consolidates high-level Accounts Payable KPIs:
      - Total Outstanding Balance
      - Current (Not Due) Amount
      - Total Overdue Amount
      - 90+ Days Critical Overdue
      - Open & Partial Bills count
      - Paid Bills count this month
      - Amount paid to vendors this month
    """
    ref_date = as_of_date or timezone.localdate()
    if isinstance(ref_date, str):
        ref_date = date.fromisoformat(ref_date)

    aging_report = get_ap_aging_report(company, as_of_date=ref_date)
    totals = aging_report["totals"]

    first_day_of_month = ref_date.replace(day=1)

    paid_this_month = VendorPayment.objects.filter(
        company=company,
        payment_date__gte=first_day_of_month,
        payment_date__lte=ref_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    bills_paid_this_month = Bill.objects.filter(
        company=company,
        status="paid",
        created_at__gte=first_day_of_month,
    ).count()

    total_vendors_with_balance = len(aging_report["vendors"])

    return {
        "as_of_date": ref_date.isoformat(),
        "total_payables": totals["total_payables"],
        "current_balance": totals["current"],
        "overdue_balance": totals["overdue_total"],
        "critical_90_plus": totals["days_90_plus"],
        "aging_buckets": {
            "current": totals["current"],
            "days_1_30": totals["days_1_30"],
            "days_31_60": totals["days_31_60"],
            "days_61_90": totals["days_61_90"],
            "days_90_plus": totals["days_90_plus"],
        },
        "bill_counts": {
            "total_open": totals["bill_count"],
            "paid_this_month": bills_paid_this_month,
            "vendors_with_balance": total_vendors_with_balance,
        },
        "paid_this_month": paid_this_month,
    }


def get_ap_bills(company, vendor_id=None, status_filter=None, search=None, as_of_date=None):
    """
    Lists vendor bills with dynamic AP calculation, aging buckets,
    and double-entry journal entry posting status.
    """
    ref_date = as_of_date or timezone.localdate()
    if isinstance(ref_date, str):
        ref_date = date.fromisoformat(ref_date)

    qs = Bill.objects.filter(company=company).select_related("vendor", "purchase_order")

    if vendor_id:
        qs = qs.filter(vendor_id=vendor_id)

    if status_filter and status_filter != "all":
        if status_filter == "overdue":
            qs = qs.filter(
                status__in=["open", "partial"],
                due_date__lt=ref_date,
            )
        else:
            qs = qs.filter(status=status_filter)

    if search:
        qs = qs.filter(
            Q(vendor__name__icontains=search) |
            Q(bill_number__icontains=search) |
            Q(id__icontains=search.replace("BILL-", "").replace("bill-", ""))
        )

    qs = qs.order_by("-bill_date", "-id")

    # Map posted journal entries for these bills
    bill_ids = [b.id for b in qs[:200]]
    posted_jes = {
        je.source_id: je
        for je in JournalEntry.objects.filter(
            company=company,
            source_module="procurement.bill",
            source_id__in=bill_ids,
            status="posted",
        )
    }

    result = []
    for b in qs:
        aging = calculate_bill_aging(b, as_of_date=ref_date)
        je = posted_jes.get(b.id)

        result.append({
            "id": b.id,
            "bill_number": b.bill_number or f"BILL-{b.id}",
            "vendor_id": b.vendor.id,
            "vendor_name": b.vendor.name,
            "purchase_order_id": b.purchase_order_id,
            "bill_date": b.bill_date.isoformat(),
            "due_date": b.due_date.isoformat() if b.due_date else b.bill_date.isoformat(),
            "total_amount": b.total_amount,
            "amount_paid": b.amount_paid,
            "balance_due": b.balance_due,
            "status": b.status,
            "days_overdue": aging["days_overdue"],
            "is_overdue": aging["is_overdue"],
            "aging_bucket": aging["bucket"],
            "aging_bucket_label": aging["bucket_label"],
            "is_posted_to_gl": bool(je),
            "journal_entry_id": je.id if je else None,
            "journal_entry_number": je.entry_number if je else None,
        })

    return result


def get_vendor_ap_statement(vendor_id, company, start_date=None, end_date=None):
    """
    Generates a chronological Vendor Payable Statement / Ledger.
    Combines Bills (Credits to AP) and Vendor Payments (Debits to AP) with running balance.
    Note: Normal balance for AP is Credit (Liabilities).
    """
    try:
        vendor = Vendor.objects.get(pk=vendor_id, company=company)
    except Vendor.DoesNotExist:
        raise ValidationError(_(f"Vendor #{vendor_id} not found in this company."))

    bills_qs = Bill.objects.filter(
        vendor=vendor,
        company=company,
    ).exclude(status="cancelled")

    payments_qs = VendorPayment.objects.filter(
        vendor=vendor,
        company=company,
    )

    if start_date:
        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        bills_qs = bills_qs.filter(bill_date__gte=start_date)
        payments_qs = payments_qs.filter(payment_date__gte=start_date)

    if end_date:
        if isinstance(end_date, str):
            end_date = date.fromisoformat(end_date)
        bills_qs = bills_qs.filter(bill_date__lte=end_date)
        payments_qs = payments_qs.filter(payment_date__lte=end_date)

    lines = []

    for b in bills_qs:
        lines.append({
            "date": b.bill_date,
            "type": "bill",
            "document_number": b.bill_number or f"BILL-{b.id}",
            "reference": f"PO #{b.purchase_order_id}" if b.purchase_order_id else "",
            "description": f"Vendor Bill {b.bill_number or b.id} (Due {b.due_date})",
            "debit": Decimal("0.00"),
            "credit": b.total_amount,
            "status": b.status,
            "due_date": b.due_date.isoformat() if b.due_date else None,
        })

    for pmt in payments_qs:
        alloc_ref = f"BILL-{pmt.bill_id}" if pmt.bill_id else "Unallocated"
        lines.append({
            "date": pmt.payment_date,
            "type": "payment",
            "document_number": f"VPMT-{pmt.id}",
            "reference": pmt.reference or pmt.method,
            "description": f"Payment via {pmt.get_method_display()} applied to {alloc_ref}",
            "debit": pmt.amount,
            "credit": Decimal("0.00"),
            "status": "applied",
            "due_date": None,
        })

    # Sort lines chronologically
    lines.sort(key=lambda x: (x["date"], 0 if x["type"] == "bill" else 1))

    # Compute running balance (Normal balance for Accounts Payable is Credit)
    running_balance = Decimal("0.00")
    total_billed = Decimal("0.00")
    total_paid = Decimal("0.00")

    formatted_lines = []
    for line in lines:
        total_billed += line["credit"]
        total_paid += line["debit"]
        running_balance += (line["credit"] - line["debit"])

        formatted_lines.append({
            **line,
            "date": line["date"].isoformat(),
            "running_balance": running_balance,
        })

    aging = calculate_vendor_aging(vendor)

    return {
        "vendor": {
            "id": vendor.id,
            "name": vendor.name,
            "category": vendor.category,
            "email": vendor.email,
            "phone": vendor.phone,
            "address": vendor.address,
            "payment_terms": vendor.payment_terms or "Net 30",
            "outstanding_balance": vendor.outstanding_balance,
        },
        "period": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        "summary": {
            "total_billed": total_billed,
            "total_paid": total_paid,
            "ending_balance": running_balance,
            "aging": aging,
        },
        "transactions": formatted_lines,
    }


def calculate_vendor_aging(vendor, as_of_date=None):
    """Calculates aging buckets for a single vendor."""
    ref_date = as_of_date or timezone.localdate()
    bills = Bill.objects.filter(
        vendor=vendor,
        status__in=["open", "partial"]
    )
    aging = {
        "current": Decimal("0.00"),
        "days_1_30": Decimal("0.00"),
        "days_31_60": Decimal("0.00"),
        "days_61_90": Decimal("0.00"),
        "days_90_plus": Decimal("0.00"),
        "total_overdue": Decimal("0.00"),
    }
    for b in bills:
        bal = b.balance_due
        if bal <= Decimal("0.00"):
            continue
        info = calculate_bill_aging(b, as_of_date=ref_date)
        bucket = info["bucket"]
        if bucket == "current":
            aging["current"] += bal
        elif bucket == "1_30":
            aging["days_1_30"] += bal
            aging["total_overdue"] += bal
        elif bucket == "31_60":
            aging["days_31_60"] += bal
            aging["total_overdue"] += bal
        elif bucket == "61_90":
            aging["days_61_90"] += bal
            aging["total_overdue"] += bal
        elif bucket == "90_plus":
            aging["days_90_plus"] += bal
            aging["total_overdue"] += bal
    return aging
