"""
Payments & Allocations Subledger & Engine (Blueprint Section No. 18)
Connects Customer Receipts and Vendor Payments with:
  - Cash & Bank (#17)
  - Accounts Receivable (#10) and Accounts Payable (#11)
  - Double-Entry General Ledger Posting (#8, #9)
  - Traceable, auditable, multi-document allocations and unallocations.
"""

from decimal import Decimal
from datetime import date, datetime
from django.db import transaction
from django.db.models import Sum, Q, F, Count
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from accounts.models import Company
from sales.models import Customer, Invoice, CustomerPayment
from procurement.models import Vendor, Bill, VendorPayment
from accounting.models import (
    Account,
    JournalEntry,
    JournalEntryLine,
    AccountingSettings,
    AccountingPeriod,
    BankAccount,
    BankTransaction,
    Payment,
    PaymentAllocation,
    PaymentAuditLog,
)
from accounting.engine import post_journal_entry, reverse_journal_entry
from accounting.receivables import get_ar_account, get_bank_account, sync_customer_balance
from accounting.payables import get_ap_account, sync_vendor_balance


# ==============================================================================
# 1. PAYMENT CREATION & MANAGEMENT
# ==============================================================================

def create_payment(
    company,
    user,
    payment_type,
    amount,
    payment_date=None,
    customer_id=None,
    vendor_id=None,
    payment_source_type="bank",
    bank_account_id=None,
    cash_account_id=None,
    payment_method="bank_transfer",
    reference="",
    external_reference="",
    notes="",
    auto_post=False,
    allocations=None,
):
    """
    Creates a new Payment record (Customer Receipt or Vendor Payment).
    If auto_post=True, posts the journal entry and executes allocations atomically.
    """
    with transaction.atomic():
        amount_dec = Decimal(str(amount))
        if amount_dec <= Decimal("0.00"):
            raise ValidationError(_("Payment amount must be greater than zero."))

        eff_date = payment_date or timezone.localdate()
        if isinstance(eff_date, str):
            eff_date = date.fromisoformat(eff_date)

        customer = None
        vendor = None

        if payment_type == "customer_receipt":
            if not customer_id:
                raise ValidationError(_("Customer is required for customer receipt."))
            try:
                customer = Customer.objects.get(pk=customer_id, company=company)
            except Customer.DoesNotExist:
                raise ValidationError(_(f"Customer #{customer_id} not found in this company."))
        elif payment_type == "vendor_payment":
            if not vendor_id:
                raise ValidationError(_("Vendor is required for vendor payment."))
            try:
                vendor = Vendor.objects.get(pk=vendor_id, company=company)
            except Vendor.DoesNotExist:
                raise ValidationError(_(f"Vendor #{vendor_id} not found in this company."))
        else:
            raise ValidationError(_(f"Invalid payment type '{payment_type}'."))

        bank_account = None
        cash_account = None

        if payment_source_type == "bank":
            if not bank_account_id:
                raise ValidationError(_("Bank account is required when source type is 'bank'."))
            try:
                bank_account = BankAccount.objects.get(pk=bank_account_id, company=company)
            except BankAccount.DoesNotExist:
                raise ValidationError(_(f"Bank account #{bank_account_id} not found in this company."))
            if not bank_account.is_active:
                raise ValidationError(_(f"Bank account '{bank_account.account_name}' is inactive."))
        elif payment_source_type == "cash":
            if cash_account_id:
                try:
                    cash_account = Account.objects.get(pk=cash_account_id, company=company)
                except Account.DoesNotExist:
                    raise ValidationError(_(f"Cash account #{cash_account_id} not found."))
                if cash_account.is_header:
                    raise ValidationError(_("Cannot use a header account for cash payments."))
            else:
                cash_account = get_bank_account(company)
        else:
            raise ValidationError(_(f"Invalid payment source type '{payment_source_type}'."))

        # Idempotency check on external_reference
        if external_reference:
            if Payment.objects.filter(company=company, external_reference=external_reference).exists():
                raise ValidationError(_(f"A payment with external reference '{external_reference}' already exists."))

        payment = Payment.objects.create(
            company=company,
            payment_type=payment_type,
            payment_date=eff_date,
            amount=amount_dec,
            currency="USD",
            customer=customer,
            vendor=vendor,
            payment_source_type=payment_source_type,
            bank_account=bank_account,
            cash_account=cash_account,
            payment_method=payment_method,
            reference=reference,
            external_reference=external_reference,
            notes=notes,
            status="draft",
            allocation_status="unallocated",
            allocated_amount=Decimal("0.00"),
            created_by=user,
        )

        PaymentAuditLog.objects.create(
            company=company,
            payment=payment,
            action="payment_created",
            actor=user,
            details={
                "payment_number": payment.payment_number,
                "amount": str(amount_dec),
                "type": payment_type,
                "source": payment_source_type,
            },
            notes=notes or "Payment draft created."
        )

        if auto_post:
            payment = post_payment(payment.id, user=user, company=company, allocations=allocations)

        return payment


# ==============================================================================
# 2. PAYMENT POSTING & JOURNAL CREATION
# ==============================================================================

def post_payment(payment_id, user, company, allocations=None):
    """
    Atomically posts a draft Payment to the General Ledger via Double-Entry Engine (#8):
      - Customer Receipt:
          DR Bank / Cash (Asset Increase)
          CR Accounts Receivable (Asset Decrease)
      - Vendor Payment:
          DR Accounts Payable (Liability Decrease)
          CR Bank / Cash (Asset Decrease)
    Also creates a linked BankTransaction in Cash & Bank (#17) if using a BankAccount.
    """
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=payment_id, company=company)
        except Payment.DoesNotExist:
            raise ValidationError(_("Payment not found."))

        if payment.status == "posted":
            raise ValidationError(_(f"Payment {payment.payment_number} is already posted."))
        if payment.status in ("cancelled", "reversed"):
            raise ValidationError(_(f"Cannot post a {payment.status} payment."))

        # 1. Period & Lock Date Validations
        settings = AccountingSettings.objects.filter(company=company).first()
        if settings and settings.lock_date and payment.payment_date <= settings.lock_date:
            raise ValidationError(_(
                f"Cannot post payment on {payment.payment_date}: Accounting is locked up to {settings.lock_date}."
            ))

        period = AccountingPeriod.objects.filter(
            fiscal_year__company=company,
            start_date__lte=payment.payment_date,
            end_date__gte=payment.payment_date,
        ).first()
        if not period or period.status != "open":
            status_desc = period.status if period else "no period"
            raise ValidationError(_(
                f"Cannot post payment on {payment.payment_date}: Period is {status_desc}."
            ))

        # 2. Resolve Accounts
        if payment.payment_source_type == "bank":
            if not payment.bank_account:
                raise ValidationError(_("Bank account is required for posting."))
            bank_gl = payment.bank_account.gl_account
            if not bank_gl.is_active or bank_gl.is_header:
                raise ValidationError(_(f"Bank GL account {bank_gl.code} must be an active leaf account."))
        else:
            bank_gl = payment.cash_account or get_bank_account(company)
            if not bank_gl.is_active or bank_gl.is_header:
                raise ValidationError(_(f"Cash GL account {bank_gl.code} must be an active leaf account."))

        ref_str = payment.reference or payment.payment_number

        if payment.payment_type == "customer_receipt":
            ar_account = get_ar_account(company)
            description = f"Customer Receipt {payment.payment_number} from {payment.customer.name} ({payment.payment_method})"

            entry = JournalEntry.objects.create(
                company=company,
                transaction_date=payment.payment_date,
                reference=ref_str,
                description=description,
                source_module="accounting.payment",
                source_id=payment.id,
                created_by=user,
                status="draft",
            )

            # DR Bank/Cash
            bank_line = JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=bank_gl,
                line_number=1,
                debit=payment.amount,
                credit=Decimal("0.00"),
                description=f"Payment received - {payment.customer.name}",
            )

            # CR Accounts Receivable
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=ar_account,
                line_number=2,
                debit=Decimal("0.00"),
                credit=payment.amount,
                description=f"AR reduction - {payment.customer.name}",
            )

        elif payment.payment_type == "vendor_payment":
            ap_account = get_ap_account(company)
            description = f"Vendor Payment {payment.payment_number} to {payment.vendor.name} ({payment.payment_method})"

            entry = JournalEntry.objects.create(
                company=company,
                transaction_date=payment.payment_date,
                reference=ref_str,
                description=description,
                source_module="accounting.payment",
                source_id=payment.id,
                created_by=user,
                status="draft",
            )

            # DR Accounts Payable
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=ap_account,
                line_number=1,
                debit=payment.amount,
                credit=Decimal("0.00"),
                description=f"AP reduction - {payment.vendor.name}",
            )

            # CR Bank/Cash
            bank_line = JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=bank_gl,
                line_number=2,
                debit=Decimal("0.00"),
                credit=payment.amount,
                description=f"Disbursement - {payment.vendor.name}",
            )

        # 3. Post through #8 engine
        posted_je = post_journal_entry(entry.id, user=user, company=company)

        # 4. Integrate with Cash & Bank (#17)
        bank_tx = None
        if payment.payment_source_type == "bank" and payment.bank_account:
            direction = "inflow" if payment.payment_type == "customer_receipt" else "outflow"
            tx_type = "customer_receipt" if payment.payment_type == "customer_receipt" else "vendor_payment"
            counterparty = payment.customer.name if payment.customer else payment.vendor.name

            bank_tx = BankTransaction.objects.create(
                company=company,
                bank_account=payment.bank_account,
                transaction_date=payment.payment_date,
                amount=payment.amount,
                direction=direction,
                transaction_type=tx_type,
                source="erp",
                description=f"{payment.get_payment_type_display()}: {counterparty}",
                reference=payment.reference or payment.payment_number,
                counterparty=counterparty,
                matching_status="matched",
                reconciliation_status="unreconciled",
                journal_entry=posted_je,
                journal_entry_line=bank_line,
                source_document_ref=payment.payment_number,
                created_by=user,
            )

        # 5. Update Payment
        payment.status = "posted"
        payment.journal_entry = posted_je
        payment.bank_transaction = bank_tx
        payment.posted_at = timezone.now()
        payment.save(update_fields=["status", "journal_entry", "bank_transaction", "posted_at", "updated_at"])

        PaymentAuditLog.objects.create(
            company=company,
            payment=payment,
            action="payment_posted",
            actor=user,
            details={
                "journal_entry": posted_je.entry_number,
                "bank_transaction": bank_tx.id if bank_tx else None,
            },
            notes=f"Payment posted to GL. Entry: {posted_je.entry_number}"
        )

        # 6. Process initial allocations if provided
        if allocations:
            payment = allocate_payment(payment.id, allocations=allocations, user=user, company=company)

        return payment


# ==============================================================================
# 3. PAYMENT ALLOCATION & UNALLOCATION
# ==============================================================================

def allocate_payment(payment_id, allocations, user, company):
    """
    Allocates an unallocated or partially allocated posted payment across
    one or more outstanding invoices (customer receipts) or bills (vendor payments).
    Does NOT create duplicate GL entries — updates settlement state atomically.
    """
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=payment_id, company=company)
        except Payment.DoesNotExist:
            raise ValidationError(_("Payment not found."))

        if payment.status != "posted":
            raise ValidationError(_(f"Cannot allocate payment in '{payment.status}' status. Only posted payments can be allocated."))

        if payment.unallocated_amount <= Decimal("0.00"):
            raise ValidationError(_("This payment has no unallocated funds remaining."))

        if not allocations or not isinstance(allocations, list):
            raise ValidationError(_("At least one allocation item is required."))

        total_requested = Decimal("0.00")
        validated_items = []

        # Validate each item
        for item in allocations:
            amt = Decimal(str(item.get("amount", "0")))
            if amt <= Decimal("0.00"):
                continue

            if payment.payment_type == "customer_receipt":
                inv_id = item.get("invoice_id")
                if not inv_id:
                    raise ValidationError(_("invoice_id is required for customer receipt allocation."))
                try:
                    invoice = Invoice.objects.select_for_update().get(
                        pk=inv_id,
                        customer=payment.customer,
                        company=company,
                    )
                except Invoice.DoesNotExist:
                    raise ValidationError(_(f"Invoice #{inv_id} not found for customer '{payment.customer.name}'."))

                if invoice.status == "cancelled":
                    raise ValidationError(_(f"Invoice INV-{inv_id} is cancelled."))

                if amt > invoice.balance_due:
                    raise ValidationError(_(
                        f"Allocated amount {amt} exceeds balance due ({invoice.balance_due}) for invoice INV-{inv_id}."
                    ))

                validated_items.append({"invoice": invoice, "amount": amt, "notes": item.get("notes", "")})

            elif payment.payment_type == "vendor_payment":
                bill_id = item.get("bill_id")
                if not bill_id:
                    raise ValidationError(_("bill_id is required for vendor payment allocation."))
                try:
                    bill = Bill.objects.select_for_update().get(
                        pk=bill_id,
                        vendor=payment.vendor,
                        company=company,
                    )
                except Bill.DoesNotExist:
                    raise ValidationError(_(f"Bill #{bill_id} not found for vendor '{payment.vendor.name}'."))

                if bill.status == "cancelled":
                    raise ValidationError(_(f"Bill {bill.bill_number or bill.id} is cancelled."))

                if amt > bill.balance_due:
                    raise ValidationError(_(
                        f"Allocated amount {amt} exceeds balance due ({bill.balance_due}) for bill {bill.bill_number or bill.id}."
                    ))

                validated_items.append({"bill": bill, "amount": amt, "notes": item.get("notes", "")})

            total_requested += amt

        if total_requested <= Decimal("0.00"):
            raise ValidationError(_("Total allocated amount must be greater than zero."))

        if total_requested > payment.unallocated_amount:
            raise ValidationError(_(
                f"Requested allocation ({total_requested}) exceeds available unallocated amount ({payment.unallocated_amount})."
            ))

        # Execute allocations
        for vi in validated_items:
            amt = vi["amount"]
            notes = vi["notes"]

            if payment.payment_type == "customer_receipt":
                invoice = vi["invoice"]
                invoice.apply_payment(amt)

                # Legacy CustomerPayment for statements
                legacy_pmt = CustomerPayment.objects.create(
                    company=company,
                    customer=payment.customer,
                    invoice=invoice,
                    amount=amt,
                    payment_date=payment.payment_date,
                    method=payment.payment_method,
                    reference=payment.payment_number,
                )

                PaymentAllocation.objects.create(
                    company=company,
                    payment=payment,
                    allocation_date=timezone.localdate(),
                    allocated_amount=amt,
                    invoice=invoice,
                    legacy_customer_payment=legacy_pmt,
                    notes=notes,
                    created_by=user,
                )

            elif payment.payment_type == "vendor_payment":
                bill = vi["bill"]
                bill.apply_payment(amt)

                # Legacy VendorPayment for statements
                legacy_pmt = VendorPayment.objects.create(
                    company=company,
                    vendor=payment.vendor,
                    bill=bill,
                    amount=amt,
                    payment_date=payment.payment_date,
                    method=payment.payment_method,
                    reference=payment.payment_number,
                )

                PaymentAllocation.objects.create(
                    company=company,
                    payment=payment,
                    allocation_date=timezone.localdate(),
                    allocated_amount=amt,
                    bill=bill,
                    legacy_vendor_payment=legacy_pmt,
                    notes=notes,
                    created_by=user,
                )

        # Update customer or vendor outstanding balance
        if payment.customer:
            sync_customer_balance(payment.customer)
        if payment.vendor:
            sync_vendor_balance(payment.vendor)

        # Update payment allocation status
        payment.allocated_amount += total_requested
        payment.allocation_status = (
            "fully_allocated" if payment.unallocated_amount <= Decimal("0.00") else "partially_allocated"
        )
        payment.save(update_fields=["allocated_amount", "allocation_status", "updated_at"])

        PaymentAuditLog.objects.create(
            company=company,
            payment=payment,
            action="allocation_created",
            actor=user,
            details={
                "allocated_amount": str(total_requested),
                "total_allocated": str(payment.allocated_amount),
                "unallocated_amount": str(payment.unallocated_amount),
                "items_count": len(validated_items),
            },
            notes=f"Allocated ${total_requested:.2f} across {len(validated_items)} document(s)."
        )

        return payment


def unallocate_allocation(allocation_id, user, company):
    """
    Reverses an active allocation:
      - Restores invoice or bill balance due and status
      - Deletes or reverses operational legacy payment link
      - Returns allocated amount back to Payment's unallocated balance
      - Preserves full audit history; does not modify posted GL entry.
    """
    with transaction.atomic():
        try:
            allocation = PaymentAllocation.objects.select_for_update().get(
                pk=allocation_id,
                company=company,
            )
        except PaymentAllocation.DoesNotExist:
            raise ValidationError(_("Allocation not found."))

        if allocation.status == "reversed":
            raise ValidationError(_("This allocation has already been unallocated/reversed."))

        payment = Payment.objects.select_for_update().get(pk=allocation.payment_id, company=company)
        amount = allocation.allocated_amount

        # 1. Restore Document
        if allocation.invoice:
            invoice = Invoice.objects.select_for_update().get(pk=allocation.invoice_id)
            invoice.amount_paid = max(Decimal("0.00"), invoice.amount_paid - amount)
            invoice.status = "open" if invoice.amount_paid == Decimal("0.00") else "partial"
            invoice.save(update_fields=["amount_paid", "status"])

            if allocation.legacy_customer_payment:
                allocation.legacy_customer_payment.delete()
                allocation.legacy_customer_payment = None

            if payment.customer:
                sync_customer_balance(payment.customer)

        elif allocation.bill:
            bill = Bill.objects.select_for_update().get(pk=allocation.bill_id)
            bill.amount_paid = max(Decimal("0.00"), bill.amount_paid - amount)
            bill.status = "open" if bill.amount_paid == Decimal("0.00") else "partial"
            bill.save(update_fields=["amount_paid", "status"])

            if allocation.legacy_vendor_payment:
                allocation.legacy_vendor_payment.delete()
                allocation.legacy_vendor_payment = None

            if payment.vendor:
                sync_vendor_balance(payment.vendor)

        # 2. Mark Allocation Reversed
        allocation.status = "reversed"
        allocation.reversed_at = timezone.now()
        allocation.save(update_fields=["status", "reversed_at", "legacy_customer_payment", "legacy_vendor_payment"])

        # 3. Update Payment Allocation Balances
        payment.allocated_amount = max(Decimal("0.00"), payment.allocated_amount - amount)
        payment.allocation_status = (
            "unallocated" if payment.allocated_amount == Decimal("0.00") else "partially_allocated"
        )
        payment.save(update_fields=["allocated_amount", "allocation_status", "updated_at"])

        # 4. Audit Log
        PaymentAuditLog.objects.create(
            company=company,
            payment=payment,
            action="allocation_unallocated",
            actor=user,
            details={
                "allocation_number": allocation.allocation_number,
                "unallocated_amount": str(amount),
                "current_allocated_total": str(payment.allocated_amount),
            },
            notes=f"Reversed allocation {allocation.allocation_number} of ${amount:.2f}."
        )

        return payment


# ==============================================================================
# 4. PAYMENT REVERSAL & CANCELLATION
# ==============================================================================

def reverse_payment(payment_id, reason, user, company):
    """
    Reverses a posted payment:
      1. Unallocates all active allocations atomically (restoring invoices/bills)
      2. Reverses the posted journal entry using Blueprint #8 double-entry engine
      3. Disassociates or cancels linked BankTransaction
      4. Marks payment status='reversed'
    """
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=payment_id, company=company)
        except Payment.DoesNotExist:
            raise ValidationError(_("Payment not found."))

        if payment.status != "posted":
            raise ValidationError(_(f"Cannot reverse a payment in '{payment.status}' status. Only posted payments can be reversed."))

        if not reason or not reason.strip():
            raise ValidationError(_("A valid reason is required for payment reversal."))

        # 1. Reverse all active allocations first
        active_allocations = payment.allocations.filter(status="active")
        for alloc in active_allocations:
            unallocate_allocation(alloc.id, user=user, company=company)

        # Refresh payment after unallocations
        payment.refresh_from_db()

        # 2. Reverse Journal Entry via #8 engine
        rev_je = None
        if payment.journal_entry:
            rev_je = reverse_journal_entry(
                payment.journal_entry.id,
                user=user,
                reason=reason,
                company=company,
            )

        # 3. Handle Bank Transaction
        if payment.bank_transaction:
            bt = payment.bank_transaction
            bt.matching_status = "unmatched"
            bt.description = f"Reversed: {bt.description}"
            bt.save(update_fields=["matching_status", "description", "updated_at"])

        # 4. Mark Payment Reversed
        payment.status = "reversed"
        payment.reversal_journal_entry = rev_je
        payment.save(update_fields=["status", "reversal_journal_entry", "updated_at"])

        PaymentAuditLog.objects.create(
            company=company,
            payment=payment,
            action="payment_reversed",
            actor=user,
            details={
                "reason": reason,
                "reversal_journal_entry": rev_je.entry_number if rev_je else None,
            },
            notes=f"Payment reversed. Reason: {reason}"
        )

        return payment


def cancel_payment(payment_id, user, company):
    """
    Cancels a draft payment that has never been posted.
    """
    with transaction.atomic():
        try:
            payment = Payment.objects.select_for_update().get(pk=payment_id, company=company)
        except Payment.DoesNotExist:
            raise ValidationError(_("Payment not found."))

        if payment.status != "draft":
            raise ValidationError(_(f"Cannot cancel payment in '{payment.status}' status. Only draft payments can be cancelled."))

        payment.status = "cancelled"
        payment.save(update_fields=["status", "updated_at"])

        PaymentAuditLog.objects.create(
            company=company,
            payment=payment,
            action="payment_cancelled",
            actor=user,
            details={},
            notes="Draft payment cancelled."
        )

        return payment


# ==============================================================================
# 5. ALLOCATION HELPER QUERIES & KPI SUMMARY
# ==============================================================================

def get_available_documents_for_allocation(payment_id, company):
    """
    Returns outstanding invoices (if customer receipt) or bills (if vendor payment)
    eligible for allocation against this specific payment.
    """
    payment = Payment.objects.get(pk=payment_id, company=company)

    if payment.payment_type == "customer_receipt":
        invoices = Invoice.objects.filter(
            company=company,
            customer=payment.customer,
            status__in=["open", "partial"]
        ).order_by("due_date", "invoice_date")

        results = []
        for inv in invoices:
            if inv.balance_due > Decimal("0.00"):
                results.append({
                    "id": inv.id,
                    "document_type": "invoice",
                    "document_number": f"INV-{inv.id}",
                    "date": inv.invoice_date.isoformat(),
                    "due_date": inv.due_date.isoformat() if inv.due_date else None,
                    "total_amount": str(inv.total_amount),
                    "amount_paid": str(inv.amount_paid),
                    "balance_due": str(inv.balance_due),
                    "status": inv.status,
                })
        return results

    elif payment.payment_type == "vendor_payment":
        bills = Bill.objects.filter(
            company=company,
            vendor=payment.vendor,
            status__in=["open", "partial"]
        ).order_by("due_date", "bill_date")

        results = []
        for bill in bills:
            if bill.balance_due > Decimal("0.00"):
                results.append({
                    "id": bill.id,
                    "document_type": "bill",
                    "document_number": bill.bill_number or f"BILL-{bill.id}",
                    "date": bill.bill_date.isoformat(),
                    "due_date": bill.due_date.isoformat() if bill.due_date else None,
                    "total_amount": str(bill.total_amount),
                    "amount_paid": str(bill.amount_paid),
                    "balance_due": str(bill.balance_due),
                    "status": bill.status,
                })
        return results

    return []


def get_payments_summary(company):
    """
    Calculates Payments & Allocations KPI dashboard metrics for tenant company.
    """
    qs = Payment.objects.filter(company=company)

    posted_qs = qs.filter(status="posted")
    customer_receipts = posted_qs.filter(payment_type="customer_receipt")
    vendor_payments = posted_qs.filter(payment_type="vendor_payment")

    total_receipts_amount = customer_receipts.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    total_disbursements_amount = vendor_payments.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

    unallocated_receipts = sum((p.unallocated_amount for p in customer_receipts), Decimal("0.00"))
    unallocated_disbursements = sum((p.unallocated_amount for p in vendor_payments), Decimal("0.00"))
    total_unallocated = unallocated_receipts + unallocated_disbursements

    total_allocated_amount = posted_qs.aggregate(s=Sum("allocated_amount"))["s"] or Decimal("0.00")

    counts = qs.aggregate(
        total=Count("id"),
        draft=Count("id", filter=Q(status="draft")),
        posted=Count("id", filter=Q(status="posted")),
        reversed=Count("id", filter=Q(status="reversed")),
        unallocated_count=Count("id", filter=Q(status="posted", allocation_status="unallocated")),
        partially_allocated_count=Count("id", filter=Q(status="posted", allocation_status="partially_allocated")),
        fully_allocated_count=Count("id", filter=Q(status="posted", allocation_status="fully_allocated")),
    )

    return {
        "total_payments_count": counts["total"],
        "draft_count": counts["draft"],
        "posted_count": counts["posted"],
        "reversed_count": counts["reversed"],
        "unallocated_count": counts["unallocated_count"],
        "partially_allocated_count": counts["partially_allocated_count"],
        "fully_allocated_count": counts["fully_allocated_count"],
        "total_receipts_amount": str(total_receipts_amount),
        "total_disbursements_amount": str(total_disbursements_amount),
        "total_allocated_amount": str(total_allocated_amount),
        "total_unallocated_amount": str(total_unallocated),
        "unallocated_receipts": str(unallocated_receipts),
        "unallocated_disbursements": str(unallocated_disbursements),
    }
