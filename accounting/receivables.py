"""
Accounts Receivable (AR) Subledger & Accounting Integration (Blueprint Section No. 10)
Provides customer receivable ledger, deterministic AR aging calculations,
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
from sales.models import Customer, Invoice, CustomerPayment
from accounting.models import Account, JournalEntry, JournalEntryLine, AccountingSettings, AccountingPeriod
from accounting.engine import post_journal_entry


def get_ar_account(company):
    """
    Resolves the primary active Accounts Receivable leaf account for the tenant.
    Prefers standard code '1100', then account type 'Accounts Receivable'.
    """
    acc = Account.objects.filter(
        company=company,
        code="1100",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    acc = Account.objects.filter(
        company=company,
        account_type__name="Accounts Receivable",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    raise ValidationError(_(
        f"No active leaf Accounts Receivable account found for company '{company.name}'. "
        "Ensure account code 1100 or an Accounts Receivable account exists in your Chart of Accounts."
    ))


def get_sales_revenue_account(company):
    """
    Resolves the default active Operating Sales Revenue leaf account for the tenant.
    Prefers standard code '4010', then account type 'Operating Sales Revenue'.
    """
    acc = Account.objects.filter(
        company=company,
        code="4010",
        is_active=True,
    ).first()
    if acc and not acc.is_header:
        return acc

    acc = Account.objects.filter(
        company=company,
        account_type__name="Operating Sales Revenue",
        is_active=True,
    ).exclude(children__isnull=False).first()
    if acc:
        return acc

    raise ValidationError(_(
        f"No active leaf Operating Sales Revenue account found for company '{company.name}'. "
        "Ensure account code 4010 or a Sales Revenue account exists in your Chart of Accounts."
    ))


def get_bank_account(company, account_id=None):
    """
    Resolves the active Cash/Bank leaf account for customer payment deposit.
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


def post_invoice_to_ar(invoice_id, user, company):
    """
    Posts an operational sales invoice to the General Ledger via Double-Entry Engine (#8).
    Balanced Journal Entry:
      - DEBIT:  Accounts Receivable (Asset)
      - CREDIT: Operating Sales Revenue (Revenue)
    Enforces tenant isolation, period lock validation, and prevents duplicate posting.
    """
    with transaction.atomic():
        try:
            invoice = Invoice.objects.select_for_update().get(pk=invoice_id, company=company)
        except Invoice.DoesNotExist:
            raise ValidationError(_(f"Invoice #{invoice_id} not found in this company."))

        if invoice.status == "cancelled":
            raise ValidationError(_("Cannot post a cancelled invoice to Accounts Receivable."))

        if invoice.total_amount <= Decimal("0.00"):
            raise ValidationError(_(f"Invoice total amount must be positive. Current total is {invoice.total_amount}."))

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

        ar_account = get_ar_account(company)
        rev_account = get_sales_revenue_account(company)

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

        # Debit Accounts Receivable
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=ar_account,
            line_number=1,
            debit=invoice.total_amount,
            credit=Decimal("0.00"),
            description=f"AR - Invoice INV-{invoice.id} ({invoice.customer.name})",
        )

        # Credit Sales Revenue
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=rev_account,
            line_number=2,
            debit=Decimal("0.00"),
            credit=invoice.total_amount,
            description=f"Sales Revenue - Invoice INV-{invoice.id}",
        )

        # Post atomically through #8 engine
        posted_entry = post_journal_entry(entry.id, user, company=company)

        # Synchronize customer's balance_due
        sync_customer_balance(invoice.customer)

        return posted_entry


def record_and_allocate_ar_payment(
    customer_id,
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
    Records a customer payment, allocates it across outstanding invoice(s),
    updates invoice balances/statuses, and posts a balanced Journal Entry to GL:
      - DEBIT:  Operating Bank/Cash (Asset)
      - CREDIT: Accounts Receivable (Asset)
    """
    with transaction.atomic():
        try:
            customer = Customer.objects.select_for_update().get(pk=customer_id, company=company)
        except Customer.DoesNotExist:
            raise ValidationError(_(f"Customer #{customer_id} not found in this company."))

        payment_amount = Decimal(str(amount))
        if payment_amount <= Decimal("0.00"):
            raise ValidationError(_("Payment amount must be greater than zero."))

        effective_date = payment_date or timezone.localdate()
        if isinstance(effective_date, str):
            effective_date = date.fromisoformat(effective_date)

        # Normalize allocations: list of {"invoice_id": id, "amount": decimal}
        if not allocations:
            raise ValidationError(_("At least one invoice allocation is required for payment processing."))

        total_allocated = Decimal("0.00")
        processed_payments = []

        for alloc in allocations:
            inv_id = alloc.get("invoice_id")
            alloc_amt = Decimal(str(alloc.get("amount", "0")))

            if alloc_amt <= Decimal("0.00"):
                continue

            try:
                invoice = Invoice.objects.select_for_update().get(
                    pk=inv_id,
                    customer=customer,
                    company=company,
                )
            except Invoice.DoesNotExist:
                raise ValidationError(_(f"Invoice #{inv_id} not found for customer '{customer.name}'."))

            if invoice.status in ("paid", "cancelled"):
                raise ValidationError(_(f"Invoice INV-{inv_id} is already marked as {invoice.status}."))

            if alloc_amt > invoice.balance_due:
                raise ValidationError(_(
                    f"Allocated amount {alloc_amt} exceeds balance due ({invoice.balance_due}) for invoice INV-{inv_id}."
                ))

            # Apply operational payment
            invoice.apply_payment(alloc_amt)
            total_allocated += alloc_amt

            pmt = CustomerPayment.objects.create(
                company=company,
                customer=customer,
                invoice=invoice,
                amount=alloc_amt,
                payment_date=effective_date,
                method=method,
                reference=reference,
            )
            processed_payments.append((pmt, invoice, alloc_amt))

        if total_allocated <= Decimal("0.00"):
            raise ValidationError(_("Total allocated payment amount must be greater than zero."))

        if total_allocated != payment_amount:
            raise ValidationError(_(
                f"Sum of allocations ({total_allocated}) does not match the payment amount ({payment_amount})."
            ))

        # Accounting Journal Entry for the Payment
        bank_account = get_bank_account(company, account_id=bank_account_id)
        ar_account = get_ar_account(company)

        inv_refs = ", ".join([f"INV-{inv.id}" for _, inv, _ in processed_payments])
        ref_text = reference or f"PMT-{customer.id}-{effective_date.strftime('%Y%m%d')}"

        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=effective_date,
            reference=ref_text,
            description=f"Customer Payment from {customer.name} applied to {inv_refs} ({method})",
            source_module="sales.payment",
            source_id=processed_payments[0][0].id if processed_payments else None,
            created_by=user,
            status="draft",
        )

        # Debit Bank Account (Cash Increase)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=bank_account,
            line_number=1,
            debit=payment_amount,
            credit=Decimal("0.00"),
            description=f"Payment received - {customer.name} ({inv_refs})",
        )

        # Credit Accounts Receivable (Asset Decrease)
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=ar_account,
            line_number=2,
            debit=Decimal("0.00"),
            credit=payment_amount,
            description=f"AR reduction - {customer.name} ({inv_refs})",
        )

        posted_entry = post_journal_entry(entry.id, user, company=company)

        # Update customer balance due
        sync_customer_balance(customer)

        return {
            "journal_entry": posted_entry,
            "payments": [p.id for p, _, _ in processed_payments],
            "total_allocated": total_allocated,
        }


def sync_customer_balance(customer):
    """
    Recalculates and persists customer.balance_due from all open/partial invoices.
    """
    total_due = Invoice.objects.filter(
        customer=customer,
        status__in=["open", "partial"]
    ).aggregate(
        total=Sum(F("total_amount") - F("amount_paid"))
    )["total"] or Decimal("0.00")

    customer.balance_due = max(Decimal("0.00"), total_due)
    customer.save(update_fields=["balance_due"])
    return customer.balance_due


def calculate_invoice_aging(invoice, as_of_date=None):
    """
    Determines days overdue and deterministic bucket for an individual invoice.
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

    due = invoice.due_date or invoice.invoice_date
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


def get_ar_aging_report(company, as_of_date=None):
    """
    Calculates deterministic AR aging breakdown across all open/partial invoices.
    Returns aggregated bucket totals and customer-wise breakdowns.
    """
    ref_date = as_of_date or timezone.localdate()
    if isinstance(ref_date, str):
        ref_date = date.fromisoformat(ref_date)

    invoices = Invoice.objects.filter(
        company=company,
        status__in=["open", "partial"],
    ).select_related("customer").order_by("due_date", "invoice_date")

    totals = {
        "total_receivables": Decimal("0.00"),
        "current": Decimal("0.00"),
        "days_1_30": Decimal("0.00"),
        "days_31_60": Decimal("0.00"),
        "days_61_90": Decimal("0.00"),
        "days_90_plus": Decimal("0.00"),
        "overdue_total": Decimal("0.00"),
        "invoice_count": 0,
    }

    customer_map = {}

    for inv in invoices:
        bal = inv.balance_due
        if bal <= Decimal("0.00"):
            continue

        aging = calculate_invoice_aging(inv, as_of_date=ref_date)
        bucket = aging["bucket"]

        # Aggregate company-level
        totals["total_receivables"] += bal
        totals["invoice_count"] += 1

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

        # Customer-level grouping
        cust = inv.customer
        if cust.id not in customer_map:
            customer_map[cust.id] = {
                "customer_id": cust.id,
                "customer_name": cust.name,
                "email": cust.email,
                "phone": cust.phone,
                "payment_terms": cust.payment_terms or "Net 30",
                "total_balance": Decimal("0.00"),
                "current": Decimal("0.00"),
                "days_1_30": Decimal("0.00"),
                "days_31_60": Decimal("0.00"),
                "days_61_90": Decimal("0.00"),
                "days_90_plus": Decimal("0.00"),
                "overdue_balance": Decimal("0.00"),
                "invoice_count": 0,
            }

        c_data = customer_map[cust.id]
        c_data["total_balance"] += bal
        c_data["invoice_count"] += 1

        if bucket == "current":
            c_data["current"] += bal
        elif bucket == "1_30":
            c_data["days_1_30"] += bal
            c_data["overdue_balance"] += bal
        elif bucket == "31_60":
            c_data["days_31_60"] += bal
            c_data["overdue_balance"] += bal
        elif bucket == "61_90":
            c_data["days_61_90"] += bal
            c_data["overdue_balance"] += bal
        elif bucket == "90_plus":
            c_data["days_90_plus"] += bal
            c_data["overdue_balance"] += bal

    customer_list = sorted(customer_map.values(), key=lambda x: x["total_balance"], reverse=True)

    return {
        "as_of_date": ref_date.isoformat(),
        "totals": totals,
        "customers": customer_list,
    }


def get_ar_summary(company, as_of_date=None):
    """
    Consolidates high-level Accounts Receivable KPIs:
      - Total Outstanding Balance
      - Current (Not Due) Amount
      - Total Overdue Amount
      - 90+ Days Critical Overdue
      - Open & Partial Invoices count
      - Paid Invoices count this month
      - Amount collected this month
    """
    ref_date = as_of_date or timezone.localdate()
    if isinstance(ref_date, str):
        ref_date = date.fromisoformat(ref_date)

    aging_report = get_ar_aging_report(company, as_of_date=ref_date)
    totals = aging_report["totals"]

    first_day_of_month = ref_date.replace(day=1)
    
    collected_this_month = CustomerPayment.objects.filter(
        company=company,
        payment_date__gte=first_day_of_month,
        payment_date__lte=ref_date,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    invoices_paid_this_month = Invoice.objects.filter(
        company=company,
        status="paid",
        created_at__gte=first_day_of_month,
    ).count()

    total_customers_with_balance = len(aging_report["customers"])

    return {
        "as_of_date": ref_date.isoformat(),
        "total_receivables": totals["total_receivables"],
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
        "invoice_counts": {
            "total_open": totals["invoice_count"],
            "paid_this_month": invoices_paid_this_month,
            "customers_with_balance": total_customers_with_balance,
        },
        "collected_this_month": collected_this_month,
    }


def get_ar_invoices(company, customer_id=None, status_filter=None, search=None, as_of_date=None):
    """
    Lists customer invoices with dynamic AR calculation, aging buckets,
    and double-entry journal entry posting status.
    """
    ref_date = as_of_date or timezone.localdate()
    if isinstance(ref_date, str):
        ref_date = date.fromisoformat(ref_date)

    qs = Invoice.objects.filter(company=company).select_related("customer", "sales_order")

    if customer_id:
        qs = qs.filter(customer_id=customer_id)

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
            Q(customer__name__icontains=search) |
            Q(id__icontains=search.replace("INV-", "").replace("inv-", ""))
        )

    qs = qs.order_by("-invoice_date", "-id")

    # Map posted journal entries for these invoices
    invoice_ids = [inv.id for inv in qs[:200]]  # limit scan
    posted_jes = {
        je.source_id: je
        for je in JournalEntry.objects.filter(
            company=company,
            source_module="sales.invoice",
            source_id__in=invoice_ids,
            status="posted",
        )
    }

    result = []
    for inv in qs:
        aging = calculate_invoice_aging(inv, as_of_date=ref_date)
        je = posted_jes.get(inv.id)

        result.append({
            "id": inv.id,
            "invoice_number": f"INV-{inv.id}",
            "customer_id": inv.customer.id,
            "customer_name": inv.customer.name,
            "invoice_date": inv.invoice_date.isoformat(),
            "due_date": inv.due_date.isoformat() if inv.due_date else inv.invoice_date.isoformat(),
            "total_amount": inv.total_amount,
            "amount_paid": inv.amount_paid,
            "balance_due": inv.balance_due,
            "status": inv.status,
            "days_overdue": aging["days_overdue"],
            "is_overdue": aging["is_overdue"],
            "aging_bucket": aging["bucket"],
            "aging_bucket_label": aging["bucket_label"],
            "is_posted_to_gl": bool(je),
            "journal_entry_id": je.id if je else None,
            "journal_entry_number": je.entry_number if je else None,
        })

    return result


def get_customer_ar_statement(customer_id, company, start_date=None, end_date=None):
    """
    Generates a chronological Customer Receivable Statement / Ledger.
    Combines Invoices (Debits) and Customer Payments (Credits) with running balance.
    """
    try:
        customer = Customer.objects.get(pk=customer_id, company=company)
    except Customer.DoesNotExist:
        raise ValidationError(_(f"Customer #{customer_id} not found in this company."))

    invoices_qs = Invoice.objects.filter(
        customer=customer,
        company=company,
    ).exclude(status="cancelled")

    payments_qs = CustomerPayment.objects.filter(
        customer=customer,
        company=company,
    )

    if start_date:
        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        invoices_qs = invoices_qs.filter(invoice_date__gte=start_date)
        payments_qs = payments_qs.filter(payment_date__gte=start_date)

    if end_date:
        if isinstance(end_date, str):
            end_date = date.fromisoformat(end_date)
        invoices_qs = invoices_qs.filter(invoice_date__lte=end_date)
        payments_qs = payments_qs.filter(payment_date__lte=end_date)

    # Combine into unified chronological ledger lines
    lines = []

    for inv in invoices_qs:
        lines.append({
            "date": inv.invoice_date,
            "type": "invoice",
            "document_number": f"INV-{inv.id}",
            "reference": f"Sales Order #{inv.sales_order_id}" if inv.sales_order_id else "",
            "description": f"Invoice INV-{inv.id} (Due {inv.due_date})",
            "debit": inv.total_amount,
            "credit": Decimal("0.00"),
            "status": inv.status,
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
        })

    for pmt in payments_qs:
        alloc_ref = f"INV-{pmt.invoice_id}" if pmt.invoice_id else "Unallocated"
        lines.append({
            "date": pmt.payment_date,
            "type": "payment",
            "document_number": f"PMT-{pmt.id}",
            "reference": pmt.reference or pmt.method,
            "description": f"Payment via {pmt.get_method_display()} applied to {alloc_ref}",
            "debit": Decimal("0.00"),
            "credit": pmt.amount,
            "status": "applied",
            "due_date": None,
        })

    # Sort lines chronologically
    lines.sort(key=lambda x: (x["date"], 0 if x["type"] == "invoice" else 1))

    # Compute running balance (Normal balance for AR is Debit)
    running_balance = Decimal("0.00")
    total_invoiced = Decimal("0.00")
    total_paid = Decimal("0.00")

    formatted_lines = []
    for line in lines:
        total_invoiced += line["debit"]
        total_paid += line["credit"]
        running_balance += (line["debit"] - line["credit"])

        formatted_lines.append({
            **line,
            "date": line["date"].isoformat(),
            "running_balance": running_balance,
        })

    # Current aging for this customer
    aging = calculate_customer_aging(customer)

    return {
        "customer": {
            "id": customer.id,
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "address": customer.address,
            "payment_terms": customer.payment_terms or "Net 30",
            "current_balance_due": customer.balance_due,
        },
        "period": {
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
        },
        "summary": {
            "total_invoiced": total_invoiced,
            "total_paid": total_paid,
            "ending_balance": running_balance,
            "aging": aging,
        },
        "transactions": formatted_lines,
    }


def calculate_customer_aging(customer, as_of_date=None):
    """Calculates aging buckets for a single customer."""
    ref_date = as_of_date or timezone.localdate()
    invoices = Invoice.objects.filter(
        customer=customer,
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
    for inv in invoices:
        bal = inv.balance_due
        if bal <= Decimal("0.00"):
            continue
        info = calculate_invoice_aging(inv, as_of_date=ref_date)
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
