"""
Blueprint Section #16 — Expenses-to-Accounting Subledger Service
Connects operational employee and vendor expenses, receipt management,
multi-stage approval workflows, tax accounting, and payment sources
directly to the Double-Entry Engine (#8) and General Ledger (#9).
"""

from decimal import Decimal
import os
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounting.models import (
    Account,
    AccountType,
    JournalEntry,
    JournalEntryLine,
    AccountingSettings,
    AccountingPeriod,
    Expense,
    ExpenseCategory,
    ExpenseAuditLog,
)
from accounting.engine import post_journal_entry, reverse_journal_entry
from accounting.purchase_accounting import get_input_tax_account


# ==============================================================================
# 1. DEFAULT EXPENSE CATEGORIES SEEDING
# ==============================================================================

DEFAULT_CATEGORIES_SPEC = [
    {
        "code": "TRAVEL",
        "name": "Travel & Transportation",
        "description": "Airline, rail, taxi, rideshare, mileage, and travel logistics",
        "preferred_account_code": "6020",
        "fallback_code": "6000",
    },
    {
        "code": "MEALS",
        "name": "Meals & Entertainment",
        "description": "Client dining, team meals, subsistence, and business entertainment",
        "preferred_account_code": "6020",
        "fallback_code": "6000",
    },
    {
        "code": "OFFICE",
        "name": "Office Supplies & Administrative",
        "description": "Stationery, printing, postage, office snacks, and administrative consumables",
        "preferred_account_code": "6000",
        "fallback_code": "6000",
    },
    {
        "code": "SOFTWARE",
        "name": "IT, Software & Subscriptions",
        "description": "SaaS subscriptions, cloud hosting, software licenses, domain renewals",
        "preferred_account_code": "6040",
        "fallback_code": "6000",
    },
    {
        "code": "UTILITIES",
        "name": "Utilities & Facility Overhead",
        "description": "Electricity, gas, water, internet, and municipal utility services",
        "preferred_account_code": "5200",
        "fallback_code": "6050",
    },
    {
        "code": "REPAIRS",
        "name": "Repairs & Maintenance",
        "description": "Plant equipment maintenance, vehicle repairs, facility upkeep",
        "preferred_account_code": "5210",
        "fallback_code": "6000",
    },
    {
        "code": "RENT",
        "name": "Facility Rent & Leases",
        "description": "Office and warehouse space leases, equipment rentals, storage fees",
        "preferred_account_code": "6050",
        "fallback_code": "6000",
    },
    {
        "code": "PROF_SERVICES",
        "name": "Professional Services & Legal",
        "description": "Legal counsel, external accounting, audit fees, consulting",
        "preferred_account_code": "6000",
        "fallback_code": "6000",
    },
    {
        "code": "TRAINING",
        "name": "Training & Professional Development",
        "description": "Certifications, conferences, seminars, skill training courses",
        "preferred_account_code": "6010",
        "fallback_code": "6000",
    },
    {
        "code": "MISCELLANEOUS",
        "name": "Miscellaneous Operating Expense",
        "description": "General operating expenditures not classified elsewhere",
        "preferred_account_code": "6000",
        "fallback_code": "6000",
    },
]


def seed_default_expense_categories(company):
    """
    Provisions standard expense categories for a tenant company, wiring them
    to appropriate Chart of Accounts leaf expense accounts.
    """
    created_categories = []
    for spec in DEFAULT_CATEGORIES_SPEC:
        # Resolve preferred or fallback account
        acc = Account.objects.filter(
            company=company,
            code=spec["preferred_account_code"],
            is_active=True,
        ).exclude(children__isnull=False).first()

        if not acc:
            acc = Account.objects.filter(
                company=company,
                code=spec["fallback_code"],
                is_active=True,
            ).exclude(children__isnull=False).first()

        if not acc:
            # Any active leaf expense account
            acc = Account.objects.filter(
                company=company,
                account_type__category="expense",
                is_active=True,
            ).exclude(children__isnull=False).first()

        cat, created = ExpenseCategory.objects.get_or_create(
            company=company,
            code=spec["code"],
            defaults={
                "name": spec["name"],
                "description": spec["description"],
                "expense_account": acc,
                "is_active": True,
            },
        )
        if not created and not cat.expense_account and acc:
            cat.expense_account = acc
            cat.save(update_fields=["expense_account"])

        created_categories.append(cat)
    return created_categories


# ==============================================================================
# 2. POLICY & ACCOUNT RESOLUTION
# ==============================================================================

def get_expenses_policy(company):
    """Retrieves current expense accounting policy settings for a company."""
    settings = AccountingSettings.objects.filter(company=company).first()
    return {
        "expenses_accounting_enabled": getattr(settings, "expenses_accounting_enabled", True),
        "expenses_require_approval": getattr(settings, "expenses_require_approval", True),
        "default_cash_account_id": getattr(settings, "expenses_default_cash_account_id", None),
        "default_bank_account_id": getattr(settings, "expenses_default_bank_account_id", None),
        "default_payable_account_id": getattr(settings, "expenses_default_payable_account_id", None),
        "default_employee_payable_account_id": getattr(settings, "expenses_default_employee_payable_account_id", None),
        "default_tax_account_id": getattr(settings, "expenses_default_tax_account_id", None),
    }


def resolve_expense_account(expense, account_id=None):
    """
    Resolves the active leaf expense debit account.
    Priority:
    1. Explicit account_id parameter or expense.expense_account
    2. ExpenseCategory.expense_account
    3. Account code 6000 or any active leaf expense account in company
    """
    company = expense.company

    if account_id:
        acc = Account.objects.filter(company=company, id=account_id).first()
        if not acc or not acc.is_active:
            raise ValidationError(_(f"Specified expense account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot post expense to a header account. Select a leaf account."))
        return acc

    if expense.expense_account_id:
        acc = expense.expense_account
        if acc.is_active and not acc.is_header:
            return acc

    if expense.category and expense.category.expense_account_id:
        acc = expense.category.expense_account
        if acc.is_active and not acc.is_header:
            return acc

    # Fallback search: code '6000' or any active leaf expense account
    acc = Account.objects.filter(
        company=company,
        code="6000",
        is_active=True,
    ).exclude(children__isnull=False).first()

    if not acc:
        acc = Account.objects.filter(
            company=company,
            account_type__category="expense",
            is_active=True,
        ).exclude(children__isnull=False).first()

    if not acc:
        raise ValidationError(_("No active leaf expense account available in Chart of Accounts for this company."))

    return acc


def resolve_tax_account(company, account_id=None):
    """
    Resolves or provisions the active leaf Input Tax Recoverable account (Asset 1310).
    Reuses existing input tax resolution from purchase_accounting.py.
    """
    settings = AccountingSettings.objects.filter(company=company).first()
    default_tax_id = getattr(settings, "expenses_default_tax_account_id", None)
    target_id = account_id or default_tax_id

    return get_input_tax_account(company, account_id=target_id)


def resolve_payment_account(expense, account_id=None):
    """
    Resolves the active leaf credit account based on payment_source:
    - CASH -> Petty Cash (1030) or cash leaf asset account
    - BANK -> Operating Bank (1010) or bank leaf asset account
    - PAYABLE:
        - Employee Expense -> Accrued Payroll / Reimbursements (2100)
        - Vendor Expense   -> Accounts Payable (2010)
    """
    company = expense.company
    settings = AccountingSettings.objects.filter(company=company).first()

    # 1. Explicit override
    if account_id:
        acc = Account.objects.filter(company=company, id=account_id).first()
        if not acc or not acc.is_active:
            raise ValidationError(_(f"Specified payment account ID {account_id} not found or inactive."))
        if acc.is_header:
            raise ValidationError(_("Cannot credit a header account. Select a leaf account."))
        return acc

    # 2. Preset on expense model
    if expense.payment_account_id:
        acc = expense.payment_account
        if acc.is_active and not acc.is_header:
            return acc

    # 3. Source-based resolution
    source = expense.payment_source

    if source == "cash":
        if settings and settings.expenses_default_cash_account_id:
            acc = settings.expenses_default_cash_account
            if acc.is_active and not acc.is_header:
                return acc

        # Search code 1030 or name containing Cash under asset
        acc = Account.objects.filter(
            company=company,
            code="1030",
            is_active=True,
        ).exclude(children__isnull=False).first()
        if not acc:
            acc = Account.objects.filter(
                company=company,
                account_type__category="asset",
                name__icontains="Cash",
                is_active=True,
            ).exclude(children__isnull=False).first()
        if not acc:
            raise ValidationError(_("No active leaf Cash account found (e.g. 1030 Petty Cash)."))
        return acc

    elif source == "bank":
        if settings and settings.expenses_default_bank_account_id:
            acc = settings.expenses_default_bank_account
            if acc.is_active and not acc.is_header:
                return acc

        # Search code 1010 or name containing Bank under asset
        acc = Account.objects.filter(
            company=company,
            code="1010",
            is_active=True,
        ).exclude(children__isnull=False).first()
        if not acc:
            acc = Account.objects.filter(
                company=company,
                account_type__category="asset",
                name__icontains="Bank",
                is_active=True,
            ).exclude(children__isnull=False).first()
        if not acc:
            raise ValidationError(_("No active leaf Bank account found (e.g. 1010 Operating Bank Account)."))
        return acc

    elif source == "payable":
        if expense.expense_type == "employee":
            if settings and settings.expenses_default_employee_payable_account_id:
                acc = settings.expenses_default_employee_payable_account
                if acc.is_active and not acc.is_header:
                    return acc

            # Search code 2100 Accrued Payroll & Wages or Reimbursement under liability
            acc = Account.objects.filter(
                company=company,
                code="2100",
                is_active=True,
            ).exclude(children__isnull=False).first()
            if not acc:
                acc = Account.objects.filter(
                    company=company,
                    account_type__category="liability",
                    name__icontains="Payroll",
                    is_active=True,
                ).exclude(children__isnull=False).first()
            if not acc:
                acc = Account.objects.filter(
                    company=company,
                    account_type__category="liability",
                    is_active=True,
                ).exclude(children__isnull=False).first()
            if not acc:
                raise ValidationError(_("No active leaf Employee Payable / Payroll account found (e.g. 2100)."))
            return acc
        else:
            # Vendor payable
            if settings and settings.expenses_default_payable_account_id:
                acc = settings.expenses_default_payable_account
                if acc.is_active and not acc.is_header:
                    return acc

            acc = Account.objects.filter(
                company=company,
                code="2010",
                is_active=True,
            ).exclude(children__isnull=False).first()
            if not acc:
                acc = Account.objects.filter(
                    company=company,
                    account_type__category="liability",
                    name__icontains="Payable",
                    is_active=True,
                ).exclude(children__isnull=False).first()
            if not acc:
                raise ValidationError(_("No active leaf Accounts Payable account found (e.g. 2010)."))
            return acc

    raise ValidationError(_(f"Unsupported payment source '{source}'."))


# ==============================================================================
# 3. APPROVAL WORKFLOW
# ==============================================================================

def submit_expense(expense, user=None):
    """Transitions expense from draft to submitted state."""
    if expense.approval_status not in ["draft", "rejected"]:
        raise ValidationError(_(f"Cannot submit expense in status '{expense.approval_status}'. Must be draft or rejected."))

    expense.approval_status = "submitted"
    expense.submitted_by = user
    expense.submitted_at = timezone.now()
    expense.save()

    ExpenseAuditLog.objects.create(
        expense=expense,
        action="submitted",
        actor=user,
        details={
            "previous_status": "draft",
            "new_status": "submitted",
            "amount": float(expense.total_amount),
        },
        notes="Expense submitted for management review",
    )
    return expense


def approve_expense(expense, user=None, notes=""):
    """Approves a submitted or draft expense."""
    if expense.approval_status in ["approved", "posted"]:
        raise ValidationError(_(f"Expense is already in '{expense.approval_status}' state."))
    if expense.approval_status == "cancelled":
        raise ValidationError(_("Cannot approve a cancelled expense."))

    prev_status = expense.approval_status
    expense.approval_status = "approved"
    expense.approved_by = user
    expense.approved_at = timezone.now()
    if expense.accounting_status == "not_ready":
        expense.accounting_status = "ready"
    expense.save()

    ExpenseAuditLog.objects.create(
        expense=expense,
        action="approved",
        actor=user,
        details={
            "previous_status": prev_status,
            "new_status": "approved",
            "amount": float(expense.total_amount),
        },
        notes=notes or "Expense approved for accounting integration",
    )
    return expense


def reject_expense(expense, user=None, reason=""):
    """Rejects an expense."""
    if expense.accounting_status == "posted":
        raise ValidationError(_("Cannot reject an expense that has already been posted to the General Ledger."))
    if expense.approval_status == "rejected":
        raise ValidationError(_("Expense is already rejected."))

    prev_status = expense.approval_status
    expense.approval_status = "rejected"
    expense.rejection_reason = reason
    expense.accounting_status = "not_ready"
    expense.save()

    ExpenseAuditLog.objects.create(
        expense=expense,
        action="rejected",
        actor=user,
        details={
            "previous_status": prev_status,
            "new_status": "rejected",
            "reason": reason,
        },
        notes=reason or "Expense rejected by reviewer",
    )
    return expense


def cancel_expense(expense, user=None):
    """Cancels an expense."""
    if expense.accounting_status == "posted":
        raise ValidationError(_("Cannot cancel an expense that has already been posted. Reverse it instead."))

    prev_status = expense.approval_status
    expense.approval_status = "cancelled"
    expense.accounting_status = "not_ready"
    expense.save()

    ExpenseAuditLog.objects.create(
        expense=expense,
        action="cancelled",
        actor=user,
        details={"previous_status": prev_status, "new_status": "cancelled"},
        notes="Expense cancelled",
    )
    return expense


# ==============================================================================
# 4. RECEIPT ATTACHMENT
# ==============================================================================

ALLOWED_RECEIPT_EXTENSIONS = [".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"]
MAX_RECEIPT_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB


def attach_receipt_to_expense(expense, file_obj, user=None):
    """Attaches a receipt document/image to the expense record with validation."""
    if not file_obj:
        raise ValidationError(_("No receipt file provided."))

    # Validate file size
    if file_obj.size > MAX_RECEIPT_SIZE_BYTES:
        raise ValidationError(_(f"Receipt file size ({file_obj.size / (1024*1024):.1f} MB) exceeds maximum allowed size (15 MB)."))

    # Validate file extension
    ext = os.path.splitext(file_obj.name)[1].lower()
    if ext not in ALLOWED_RECEIPT_EXTENSIONS:
        raise ValidationError(_(f"Unsupported file format '{ext}'. Allowed formats: {', '.join(ALLOWED_RECEIPT_EXTENSIONS)}."))

    expense.receipt = file_obj
    expense.receipt_name = file_obj.name
    expense.receipt_size = file_obj.size
    expense.receipt_content_type = getattr(file_obj, "content_type", "") or "application/octet-stream"
    expense.save()

    ExpenseAuditLog.objects.create(
        expense=expense,
        action="receipt_attached",
        actor=user,
        details={
            "filename": expense.receipt_name,
            "size_bytes": expense.receipt_size,
            "content_type": expense.receipt_content_type,
        },
        notes=f"Receipt '{expense.receipt_name}' attached",
    )
    return expense


# ==============================================================================
# 5. ACCOUNTING PREVIEW & POSTING
# ==============================================================================

def get_expense_accounting_preview(
    expense,
    expense_account_id=None,
    tax_account_id=None,
    payment_account_id=None,
    payment_source_override=None,
    amount_before_tax_override=None,
    tax_amount_override=None,
):
    """
    Generates a prospective, balanced double-entry journal preview without saving to the database.
    """
    company = expense.company

    # Handle parameter overrides
    amount_before_tax = Decimal(str(amount_before_tax_override)) if amount_before_tax_override is not None else expense.amount_before_tax
    tax_amount = Decimal(str(tax_amount_override)) if tax_amount_override is not None else expense.tax_amount
    total_amount = amount_before_tax + tax_amount

    if amount_before_tax < Decimal("0.00"):
        raise ValidationError(_("Expense amount cannot be negative."))
    if tax_amount < Decimal("0.00"):
        raise ValidationError(_("Tax amount cannot be negative."))
    if total_amount <= Decimal("0.00"):
        raise ValidationError(_("Total expense amount must be greater than zero."))

    temp_expense = expense
    if payment_source_override:
        temp_expense.payment_source = payment_source_override

    # Resolve accounts
    exp_acc = resolve_expense_account(temp_expense, account_id=expense_account_id)
    tax_acc = resolve_tax_account(company, account_id=tax_account_id) if tax_amount > Decimal("0.00") else None
    pay_acc = resolve_payment_account(temp_expense, account_id=payment_account_id)

    lines = []
    # Line 1: DR Expense
    lines.append({
        "line_number": 1,
        "account_id": exp_acc.id,
        "account_code": exp_acc.code,
        "account_name": exp_acc.name,
        "category": exp_acc.account_type.category,
        "debit": float(amount_before_tax),
        "credit": 0.0,
        "description": f"Expense [{temp_expense.category.name}] - {temp_expense.title}",
    })

    # Line 2 (Optional): DR Input Tax Recoverable
    if tax_amount > Decimal("0.00") and tax_acc:
        lines.append({
            "line_number": 2,
            "account_id": tax_acc.id,
            "account_code": tax_acc.code,
            "account_name": tax_acc.name,
            "category": tax_acc.account_type.category,
            "debit": float(tax_amount),
            "credit": 0.0,
            "description": f"Input Tax Recoverable - {temp_expense.expense_number}",
        })

    # Line 3: CR Cash / Bank / Payable
    credit_desc = f"Payment Source ({temp_expense.get_payment_source_display()}) - {temp_expense.expense_number}"
    if temp_expense.payment_source == "payable":
        party_name = f"{temp_expense.employee.first_name} {temp_expense.employee.last_name}".strip() if temp_expense.employee else (temp_expense.vendor.name if temp_expense.vendor else temp_expense.vendor_name_raw)
        if party_name:
            credit_desc += f" [{party_name}]"

    lines.append({
        "line_number": len(lines) + 1,
        "account_id": pay_acc.id,
        "account_code": pay_acc.code,
        "account_name": pay_acc.name,
        "category": pay_acc.account_type.category,
        "debit": 0.0,
        "credit": float(total_amount),
        "description": credit_desc,
    })

    total_debit = float(amount_before_tax + tax_amount)
    total_credit = float(total_amount)
    balanced = abs(total_debit - total_credit) < 0.001

    return {
        "expense_id": expense.id,
        "expense_number": expense.expense_number,
        "expense_type": expense.expense_type,
        "title": expense.title,
        "payment_source": temp_expense.payment_source,
        "approval_status": expense.approval_status,
        "amount_before_tax": float(amount_before_tax),
        "tax_amount": float(tax_amount),
        "total_amount": float(total_amount),
        "currency": expense.currency,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "balanced": balanced,
        "prospective_lines": lines,
        "resolved_accounts": {
            "expense": {"id": exp_acc.id, "code": exp_acc.code, "name": exp_acc.name},
            "tax": {"id": tax_acc.id, "code": tax_acc.code, "name": tax_acc.name} if tax_acc else None,
            "payment": {"id": pay_acc.id, "code": pay_acc.code, "name": pay_acc.name},
        },
    }


def post_expense_accounting(
    expense,
    user=None,
    transaction_date=None,
    expense_account_id=None,
    tax_account_id=None,
    payment_account_id=None,
    notes=None,
):
    """
    Atomically creates and posts a double-entry journal entry for the approved expense,
    advancing its accounting status to POSTED. Enforces idempotency, leaf accounts,
    lock dates, and fiscal period status.
    """
    company = expense.company
    policy = get_expenses_policy(company)

    if not policy["expenses_accounting_enabled"]:
        raise ValidationError(_("Expenses accounting is disabled in company accounting settings."))

    # 1. Approval enforcement
    if policy["expenses_require_approval"] and expense.approval_status != "approved":
        raise ValidationError(_(
            f"Expense {expense.expense_number} cannot be posted in '{expense.approval_status}' status. "
            "It must be approved by an authorized manager first."
        ))

    if expense.approval_status in ["rejected", "cancelled"]:
        raise ValidationError(_(f"Cannot post a {expense.approval_status} expense to the General Ledger."))

    # 2. Idempotency Check
    if expense.accounting_status == "posted" and expense.journal_entry_id:
        raise ValidationError(_(
            f"Expense {expense.expense_number} has already been posted to the General Ledger "
            f"(Journal Entry #{expense.journal_entry.entry_number}). Duplicate posting prevented."
        ))

    existing_je = JournalEntry.objects.filter(
        company=company,
        source_module="expenses",
        source_id=str(expense.id),
        status="posted",
    ).first()
    if existing_je:
        expense.journal_entry = existing_je
        expense.accounting_status = "posted"
        expense.save(update_fields=["journal_entry", "accounting_status"])
        raise ValidationError(_(
            f"Expense {expense.expense_number} has already been posted to the General Ledger "
            f"(Journal Entry #{existing_je.entry_number}). Duplicate posting prevented."
        ))

    # 3. Transaction Date & Fiscal Period Controls
    txn_date = transaction_date or expense.accounting_date or expense.expense_date
    if isinstance(txn_date, str):
        from datetime import date
        txn_date = date.fromisoformat(txn_date)

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.lock_date and txn_date <= settings.lock_date:
        raise ValidationError(_(f"Cannot post transaction on {txn_date}: period is locked as of {settings.lock_date}."))

    period = AccountingPeriod.objects.filter(
        fiscal_year__company=company,
        start_date__lte=txn_date,
        end_date__gte=txn_date,
    ).first()
    if period and getattr(period, "status", "") == "closed":
        raise ValidationError(_(f"Accounting period '{period.name}' is closed. Cannot post expense entry."))

    # 4. Resolve Accounts & Validate Balance
    exp_acc = resolve_expense_account(expense, account_id=expense_account_id)
    tax_acc = resolve_tax_account(company, account_id=tax_account_id) if expense.tax_amount > Decimal("0.00") else None
    pay_acc = resolve_payment_account(expense, account_id=payment_account_id)

    amount_before_tax = expense.amount_before_tax
    tax_amount = expense.tax_amount
    total_amount = expense.total_amount

    if total_amount <= Decimal("0.00"):
        raise ValidationError(_("Expense total amount must be greater than zero."))

    with transaction.atomic():
        ref = expense.expense_number
        narration = notes or f"Expense #{ref} [{expense.category.name}] - {expense.title}"

        # 5. Create draft journal entry
        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=txn_date,
            accounting_period=period,
            reference=ref,
            description=narration,
            source_module="expenses",
            source_id=str(expense.id),
            created_by=user,
            status="draft",
        )

        # Line 1: DR Expense
        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=exp_acc,
            debit=amount_before_tax,
            credit=Decimal("0.00"),
            description=f"Expense - {expense.category.name}: {expense.title}",
            line_number=1,
        )

        # Line 2 (Optional): DR Input Tax Recoverable
        if tax_amount > Decimal("0.00") and tax_acc:
            JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=tax_acc,
                debit=tax_amount,
                credit=Decimal("0.00"),
                description=f"Input Tax Recoverable - {ref}",
                line_number=2,
            )

        # Line 3: CR Cash / Bank / Payable
        credit_memo = f"Payment Source ({expense.get_payment_source_display()}) - {ref}"
        if expense.payment_source == "payable":
            party = f"{expense.employee.first_name} {expense.employee.last_name}".strip() if expense.employee else (expense.vendor.name if expense.vendor else expense.vendor_name_raw)
            if party:
                credit_memo += f" [{party}]"

        JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=pay_acc,
            debit=Decimal("0.00"),
            credit=total_amount,
            description=credit_memo,
            line_number=3 if tax_amount > Decimal("0.00") and tax_acc else 2,
        )

        # 6. Post atomically via Section #8 Engine
        posted_je = post_journal_entry(entry.id, user=user, company=company)

        # 7. Update Expense record
        expense.accounting_date = txn_date
        expense.accounting_status = "posted"
        expense.journal_entry = posted_je
        expense.posted_by = user
        expense.posted_at = timezone.now()
        expense.expense_account = exp_acc
        if tax_acc:
            expense.tax_account = tax_acc
        expense.payment_account = pay_acc
        expense.save()

        # 8. Record audit log
        ExpenseAuditLog.objects.create(
            expense=expense,
            action="posted",
            actor=user,
            details={
                "journal_entry_id": posted_je.id,
                "journal_entry_number": posted_je.entry_number,
                "total_amount": float(total_amount),
                "debit_account": exp_acc.code,
                "credit_account": pay_acc.code,
                "tax_account": tax_acc.code if tax_acc else None,
            },
            notes=f"Posted to General Ledger via JE #{posted_je.entry_number}",
        )

        # Blueprint #19 Tax Layer Integration
        if tax_amount > Decimal("0.00") and tax_acc:
            try:
                from .tax import record_tax_line
                tax_rate_val = Decimal("0.0000")
                if amount_before_tax > Decimal("0.00"):
                    tax_rate_val = (tax_amount / amount_before_tax * Decimal("100.0000")).quantize(Decimal("0.0001"))
                record_tax_line(
                    company=company,
                    source_module="expenses",
                    source_id=str(expense.id),
                    source_reference=expense.expense_number,
                    taxable_amount=amount_before_tax,
                    tax_rate=tax_rate_val,
                    tax_amount=tax_amount,
                    transaction_date=txn_date,
                    tax_account=tax_acc,
                    journal_entry=posted_je,
                    actor=user,
                )
            except Exception:
                pass

        return posted_je


def reverse_expense_accounting(expense, user=None, reason="", reversal_date=None):
    """
    Reverses a posted expense journal entry, creating a mirror-image reversal entry
    in the General Ledger while preserving historical audit lineage.
    """
    company = expense.company

    if expense.accounting_status != "posted" or not expense.journal_entry_id:
        raise ValidationError(_(f"Cannot reverse expense {expense.expense_number}: it has not been posted to the General Ledger."))

    rev_date = reversal_date or timezone.now().date()
    if isinstance(rev_date, str):
        from datetime import date
        rev_date = date.fromisoformat(rev_date)

    settings = AccountingSettings.objects.filter(company=company).first()
    if settings and settings.lock_date and rev_date <= settings.lock_date:
        raise ValidationError(_(f"Cannot reverse transaction on {rev_date}: period is locked as of {settings.lock_date}."))

    period = AccountingPeriod.objects.filter(
        fiscal_year__company=company,
        start_date__lte=rev_date,
        end_date__gte=rev_date,
    ).first()
    if period and getattr(period, "status", "") == "closed":
        raise ValidationError(_(f"Accounting period '{period.name}' is closed. Cannot reverse expense entry."))

    with transaction.atomic():
        reversal_je = reverse_journal_entry(
            entry_id=expense.journal_entry.id,
            user=user,
            reason=reason or f"Reversal of Expense #{expense.expense_number}",
            reversal_date=rev_date,
            company=company,
        )

        expense.accounting_status = "reversed"
        expense.reversal_journal_entry = reversal_je
        expense.save(update_fields=["accounting_status", "reversal_journal_entry"])

        ExpenseAuditLog.objects.create(
            expense=expense,
            action="reversed",
            actor=user,
            details={
                "original_journal_entry_id": expense.journal_entry.id,
                "reversal_journal_entry_id": reversal_je.id,
                "reversal_journal_entry_number": reversal_je.entry_number,
                "reason": reason,
            },
            notes=reason or f"Reversed via JE #{reversal_je.entry_number}",
        )

        # Blueprint #19 Tax Layer Integration: mark tax lines as reversed
        try:
            from .models import TaxTransactionLine
            TaxTransactionLine.objects.filter(
                company=company,
                source_module="expenses",
                source_id=str(expense.id),
                is_reversed=False,
            ).update(
                is_reversed=True,
                reversed_at=timezone.now(),
                reversal_reference="Reversed via expense reversal",
                reversal_journal_entry=reversal_je,
            )
        except Exception:
            pass

        return reversal_je


# ==============================================================================
# 6. SUMMARY & KPI AGGREGATIONS
# ==============================================================================

def get_expenses_summary(company):
    """
    Aggregates subledger metrics and GL balances for the Expenses-to-Accounting subledger.
    """
    from django.db.models import Sum, Count, Q

    expenses_qs = Expense.objects.filter(company=company)

    # Subledger Aggregations
    total_expenses_count = expenses_qs.count()
    total_expenses_amount = expenses_qs.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    pending_approval_qs = expenses_qs.filter(approval_status="submitted")
    pending_approval_count = pending_approval_qs.count()
    pending_approval_amount = pending_approval_qs.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    approved_unposted_qs = expenses_qs.filter(approval_status="approved", accounting_status__in=["not_ready", "ready"])
    approved_unposted_count = approved_unposted_qs.count()
    approved_unposted_amount = approved_unposted_qs.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    posted_qs = expenses_qs.filter(accounting_status="posted")
    posted_count = posted_qs.count()
    posted_amount = posted_qs.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")
    posted_tax_amount = posted_qs.aggregate(s=Sum("tax_amount"))["s"] or Decimal("0.00")

    reversed_qs = expenses_qs.filter(accounting_status="reversed")
    reversed_count = reversed_qs.count()
    reversed_amount = reversed_qs.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    # Outstanding Employee Reimbursements (payable & posted)
    emp_reimbursements_qs = posted_qs.filter(expense_type="employee", payment_source="payable")
    emp_reimbursements_amount = emp_reimbursements_qs.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    # Vendor Payable Expenses (payable & posted)
    vendor_payables_qs = posted_qs.filter(expense_type="vendor", payment_source="payable")
    vendor_payables_amount = vendor_payables_qs.aggregate(s=Sum("total_amount"))["s"] or Decimal("0.00")

    # GL Operating Expenses balance from Journal lines
    gl_lines = JournalEntryLine.objects.filter(
        company=company,
        journal_entry__status="posted",
        account__account_type__category="expense",
    )
    gl_debits = gl_lines.aggregate(s=Sum("debit"))["s"] or Decimal("0.00")
    gl_credits = gl_lines.aggregate(s=Sum("credit"))["s"] or Decimal("0.00")
    gl_operating_expense_net = gl_debits - gl_credits

    return {
        "total_expenses_count": total_expenses_count,
        "total_expenses_amount": float(total_expenses_amount),
        "pending_approval_count": pending_approval_count,
        "pending_approval_amount": float(pending_approval_amount),
        "approved_unposted_count": approved_unposted_count,
        "approved_unposted_amount": float(approved_unposted_amount),
        "posted_count": posted_count,
        "posted_amount": float(posted_amount),
        "posted_tax_amount": float(posted_tax_amount),
        "reversed_count": reversed_count,
        "reversed_amount": float(reversed_amount),
        "employee_reimbursements_amount": float(emp_reimbursements_amount),
        "vendor_payables_amount": float(vendor_payables_amount),
        "gl_operating_expense_net": float(gl_operating_expense_net),
    }
