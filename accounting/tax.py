"""
Blueprint Section #19 — Centralized Tax Layer Service
Handles configurable tax codes, deterministic decimal tax calculations (inclusive/exclusive),
transaction-level breakdown, double-entry journal integration, auditable adjustments,
reversals, and reporting.
"""

from decimal import Decimal, ROUND_HALF_UP
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.db.models import Sum, Count, Q

from accounting.models import (
    TaxCode,
    TaxTransactionLine,
    TaxAdjustment,
    TaxAuditLog,
    Account,
    AccountingPeriod,
    JournalEntry,
    JournalEntryLine,
)
from accounting.engine import post_journal_entry, reverse_journal_entry
from accounting.sales_accounting import get_tax_payable_account
from accounting.purchase_accounting import get_input_tax_account


def quantize_currency(amount, precision=2):
    """
    Rounds a numeric amount to financial Decimal precision using standard ROUND_HALF_UP.
    Prevents floating-point precision leakage.
    """
    if amount is None:
        amount = Decimal("0.00")
    elif not isinstance(amount, Decimal):
        amount = Decimal(str(amount))
    
    fmt = "0." + "0" * precision if precision > 0 else "0"
    return amount.quantize(Decimal(fmt), rounding=ROUND_HALF_UP)


def calculate_tax(amount, tax_rate=None, tax_code=None, calculation_mode=None, precision=2):
    """
    Authoritative backend tax calculation engine.
    Calculates taxable base, tax amount, and total gross deterministic values.
    
    Calculation Modes:
    1. 'exclusive':
       - Taxable base = amount
       - Tax amount = amount * (rate / 100)
       - Total gross = Taxable base + Tax amount
    2. 'inclusive':
       - Total gross = amount
       - Taxable base = amount / (1 + rate / 100)
       - Tax amount = Total gross - Taxable base
    """
    if amount is None:
        raise ValidationError(_("Amount is required for tax calculation."))

    amt = quantize_currency(amount, precision)
    if amt < Decimal("0.00"):
        raise ValidationError(_("Taxable amount cannot be negative."))

    resolved_code_obj = None
    if isinstance(tax_code, TaxCode):
        resolved_code_obj = tax_code
    elif tax_code:
        resolved_code_obj = TaxCode.objects.filter(pk=tax_code).first()

    if resolved_code_obj:
        if tax_rate is None:
            tax_rate = resolved_code_obj.rate
        if calculation_mode is None:
            calculation_mode = resolved_code_obj.calculation_mode

    if calculation_mode is None:
        calculation_mode = "exclusive"

    if tax_rate is None:
        tax_rate = Decimal("0.0000")
    elif not isinstance(tax_rate, Decimal):
        tax_rate = Decimal(str(tax_rate))

    if tax_rate < Decimal("0.0000"):
        raise ValidationError(_("Tax rate cannot be negative."))

    # Zero tax / exempt case
    if tax_rate == Decimal("0.0000"):
        return {
            "taxable_amount": amt,
            "tax_rate": tax_rate,
            "tax_amount": Decimal("0.00"),
            "total_amount": amt,
            "calculation_mode": calculation_mode,
            "tax_code_id": resolved_code_obj.id if resolved_code_obj else None,
            "tax_code": resolved_code_obj.code if resolved_code_obj else None,
        }

    rate_factor = tax_rate / Decimal("100.0000")

    if calculation_mode == "inclusive":
        gross_total = amt
        taxable_base = quantize_currency(gross_total / (Decimal("1.0000") + rate_factor), precision)
        tax_amount = gross_total - taxable_base
    else:  # exclusive
        taxable_base = amt
        tax_amount = quantize_currency(taxable_base * rate_factor, precision)
        gross_total = taxable_base + tax_amount

    return {
        "taxable_amount": taxable_base,
        "tax_rate": tax_rate,
        "tax_amount": tax_amount,
        "total_amount": gross_total,
        "calculation_mode": calculation_mode,
        "tax_code_id": resolved_code_obj.id if resolved_code_obj else None,
        "tax_code": resolved_code_obj.code if resolved_code_obj else None,
    }


def calculate_lines_tax(lines, default_tax_code=None, default_mode="exclusive", precision=2):
    """
    Computes deterministic line-level tax breakdowns and document totals.
    `lines`: list of dicts with keys: amount, tax_code_id, tax_rate, calculation_mode.
    """
    results = []
    total_taxable = Decimal("0.00")
    total_tax = Decimal("0.00")
    grand_total = Decimal("0.00")

    for idx, line in enumerate(lines):
        line_amt = line.get("amount", Decimal("0.00"))
        line_code_id = line.get("tax_code_id") or (default_tax_code.id if default_tax_code else None)
        line_rate = line.get("tax_rate")
        line_mode = line.get("calculation_mode") or default_mode

        calc = calculate_tax(
            amount=line_amt,
            tax_rate=line_rate,
            tax_code=line_code_id,
            calculation_mode=line_mode,
            precision=precision,
        )
        calc["line_number"] = idx + 1
        calc["description"] = line.get("description", "")
        results.append(calc)

        total_taxable += calc["taxable_amount"]
        total_tax += calc["tax_amount"]
        grand_total += calc["total_amount"]

    return {
        "lines": results,
        "total_taxable_amount": total_taxable,
        "total_tax_amount": total_tax,
        "grand_total": grand_total,
    }


def record_tax_line(
    company,
    source_module,
    source_id,
    source_reference,
    taxable_amount,
    tax_rate,
    tax_amount,
    transaction_date,
    tax_code=None,
    tax_account=None,
    calculation_mode="exclusive",
    source_line_id="",
    journal_entry=None,
    actor=None,
    metadata=None,
):
    """
    Records an immutable transaction-level tax breakdown line and links to GL journal entry.
    """
    if tax_account is None:
        if tax_code and tax_code.tax_account:
            tax_account = tax_code.tax_account
        elif source_module == "sales":
            tax_account = get_tax_payable_account(company)
        else:
            tax_account = get_input_tax_account(company)

    if tax_account.company_id != company.id:
        raise ValidationError(_("Tax account belongs to a different company."))
    if tax_account.is_header:
        raise ValidationError(_("Tax account cannot be a header account."))
    if not tax_account.is_active:
        raise ValidationError(_("Tax account is inactive."))

    t_amt = quantize_currency(tax_amount)
    base_amt = quantize_currency(taxable_amount)
    gross_total = base_amt + t_amt

    is_posted = bool(journal_entry and journal_entry.status == "posted")

    line = TaxTransactionLine.objects.create(
        company=company,
        tax_code=tax_code,
        tax_account=tax_account,
        source_module=source_module,
        source_id=str(source_id or ""),
        source_reference=str(source_reference or ""),
        source_line_id=str(source_line_id or ""),
        taxable_amount=base_amt,
        tax_rate=Decimal(str(tax_rate or "0.0000")),
        tax_amount=t_amt,
        total_amount=gross_total,
        calculation_mode=calculation_mode or "exclusive",
        transaction_date=transaction_date,
        journal_entry=journal_entry,
        is_posted=is_posted,
        created_by=actor,
        metadata=metadata or {},
    )

    TaxAuditLog.objects.create(
        company=company,
        tax_code=tax_code,
        tax_line=line,
        action="tax_line_recorded",
        actor=actor,
        details={
            "source_module": source_module,
            "source_id": str(source_id),
            "source_reference": str(source_reference),
            "taxable_amount": float(base_amt),
            "tax_amount": float(t_amt),
            "tax_rate": float(tax_rate or 0),
            "journal_entry_id": journal_entry.id if journal_entry else None,
        }
    )

    return line


def create_tax_adjustment(company, user, data):
    """
    Creates a controlled, auditable tax adjustment in draft or posted state.
    """
    tax_code_id = data.get("tax_code_id")
    original_line_id = data.get("original_tax_line_id")
    tax_account_id = data.get("tax_account_id")
    offset_account_id = data.get("offset_account_id")
    adjustment_direction = data.get("adjustment_direction", "increase_liability")
    taxable_amount = quantize_currency(data.get("taxable_amount", Decimal("0.00")))
    tax_amount = quantize_currency(data.get("tax_amount"))
    adjustment_date = data.get("adjustment_date") or timezone.now().date()
    reason = (data.get("reason") or "").strip()
    auto_post = data.get("auto_post", False)

    if not reason:
        raise ValidationError({"reason": _("A detailed business reason is mandatory for tax adjustments.")})
    if tax_amount <= Decimal("0.00"):
        raise ValidationError({"tax_amount": _("Adjustment tax amount must be greater than zero.")})

    tax_code = None
    if tax_code_id:
        tax_code = TaxCode.objects.filter(pk=tax_code_id, company=company).first()
        if not tax_code:
            raise ValidationError({"tax_code_id": _("Selected tax code not found.")})

    original_line = None
    if original_line_id:
        original_line = TaxTransactionLine.objects.filter(pk=original_line_id, company=company).first()

    # Resolve tax account
    if not tax_account_id:
        if tax_code:
            tax_account = tax_code.tax_account
        elif original_line:
            tax_account = original_line.tax_account
        else:
            tax_account = get_tax_payable_account(company)
    else:
        tax_account = Account.objects.filter(pk=tax_account_id, company=company).first()
        if not tax_account:
            raise ValidationError({"tax_account_id": _("Selected tax account not found.")})

    # Resolve offset account
    if not offset_account_id:
        raise ValidationError({"offset_account_id": _("Balancing offset account is required.")})
    offset_account = Account.objects.filter(pk=offset_account_id, company=company).first()
    if not offset_account:
        raise ValidationError({"offset_account_id": _("Selected offset account not found.")})

    with transaction.atomic():
        adjustment = TaxAdjustment.objects.create(
            company=company,
            tax_code=tax_code,
            original_tax_line=original_line,
            tax_account=tax_account,
            offset_account=offset_account,
            adjustment_direction=adjustment_direction,
            taxable_amount=taxable_amount,
            tax_amount=tax_amount,
            adjustment_date=adjustment_date,
            reason=reason,
            status="draft",
            created_by=user,
        )

        TaxAuditLog.objects.create(
            company=company,
            tax_code=tax_code,
            tax_adjustment=adjustment,
            action="tax_adjustment_created",
            actor=user,
            details={
                "adjustment_number": adjustment.adjustment_number,
                "direction": adjustment_direction,
                "tax_amount": float(tax_amount),
                "reason": reason,
            }
        )

        if auto_post:
            adjustment = post_tax_adjustment(adjustment.id, user, company=company)

        return adjustment


def post_tax_adjustment(adjustment_id, user, company=None):
    """
    Posts a draft tax adjustment to the General Ledger via Blueprint #8 Double-Entry Engine.
    Creates balanced journal entry and updates adjustment status and tax transaction line.
    """
    with transaction.atomic():
        qs = TaxAdjustment.objects.select_for_update()
        if company:
            qs = qs.filter(company=company)

        try:
            adj = qs.get(pk=adjustment_id)
        except TaxAdjustment.DoesNotExist:
            raise ValidationError(_(f"Tax adjustment #{adjustment_id} not found."))

        if adj.status == "posted":
            raise ValidationError(_(f"Adjustment {adj.adjustment_number} has already been posted."))
        if adj.status != "draft":
            raise ValidationError(_(f"Cannot post tax adjustment in status '{adj.status}'."))

        # 1. Period check: Must be an open period covering adjustment_date
        period = AccountingPeriod.objects.filter(
            fiscal_year__company=adj.company,
            start_date__lte=adj.adjustment_date,
            end_date__gte=adj.adjustment_date,
        ).first()

        if not period or period.status != "open":
            raise ValidationError(_(
                f"Accounting period for date {adj.adjustment_date} is not open (status: {getattr(period, 'status', 'None')}). "
                "Postings to closed or locked periods are strictly rejected."
            ))

        # 2. Account validations
        if not adj.tax_account.is_active or adj.tax_account.is_header:
            raise ValidationError(_("Tax account must be an active leaf posting account."))
        if not adj.offset_account.is_active or adj.offset_account.is_header:
            raise ValidationError(_("Offset account must be an active leaf posting account."))

        # 3. Create Draft Journal Entry
        entry = JournalEntry.objects.create(
            company=adj.company,
            transaction_date=adj.adjustment_date,
            reference=adj.adjustment_number,
            description=f"Tax Adjustment {adj.adjustment_number}: {adj.reason}",
            source_module="tax_adjustment",
            source_id=adj.id,
            created_by=user,
            status="draft",
        )

        # 4. Construct balanced debit/credit lines according to direction
        amount = adj.tax_amount

        if adj.adjustment_direction == "increase_liability":
            # Tax payable increases: DR Expense/Offset, CR Tax Payable
            dr_acc = adj.offset_account
            cr_acc = adj.tax_account
        elif adj.adjustment_direction == "decrease_liability":
            # Tax payable decreases: DR Tax Payable, CR Expense/Offset
            dr_acc = adj.tax_account
            cr_acc = adj.offset_account
        elif adj.adjustment_direction == "increase_credit":
            # Input tax asset increases: DR Input Tax Recoverable, CR Offset
            dr_acc = adj.tax_account
            cr_acc = adj.offset_account
        else:  # decrease_credit
            # Input tax asset decreases: DR Offset, CR Input Tax Recoverable
            dr_acc = adj.offset_account
            cr_acc = adj.tax_account

        JournalEntryLine.objects.create(
            company=adj.company,
            journal_entry=entry,
            account=dr_acc,
            line_number=1,
            debit=amount,
            credit=Decimal("0.00"),
            description=f"{adj.adjustment_number} Debit {dr_acc.name}",
        )
        JournalEntryLine.objects.create(
            company=adj.company,
            journal_entry=entry,
            account=cr_acc,
            line_number=2,
            debit=Decimal("0.00"),
            credit=amount,
            description=f"{adj.adjustment_number} Credit {cr_acc.name}",
        )

        # 5. Post through #8 Double-Entry Accounting Engine
        posted_entry = post_journal_entry(entry.id, user, company=adj.company)

        # 6. Commit adjustment state
        adj.status = "posted"
        adj.journal_entry = posted_entry
        adj.posted_by = user
        adj.posted_at = timezone.now()
        adj.save()

        # 7. Record transaction tax line
        record_tax_line(
            company=adj.company,
            source_module="tax_adjustment",
            source_id=str(adj.id),
            source_reference=adj.adjustment_number,
            taxable_amount=adj.taxable_amount,
            tax_rate=adj.tax_code.rate if adj.tax_code else Decimal("0.0000"),
            tax_amount=adj.tax_amount,
            transaction_date=adj.adjustment_date,
            tax_code=adj.tax_code,
            tax_account=adj.tax_account,
            calculation_mode="exclusive",
            journal_entry=posted_entry,
            actor=user,
            metadata={"direction": adj.adjustment_direction, "reason": adj.reason}
        )

        TaxAuditLog.objects.create(
            company=adj.company,
            tax_code=adj.tax_code,
            tax_adjustment=adj,
            action="tax_adjustment_posted",
            actor=user,
            details={
                "adjustment_number": adj.adjustment_number,
                "journal_entry_id": posted_entry.id,
                "journal_number": posted_entry.entry_number,
                "tax_amount": float(adj.tax_amount),
            }
        )

        return adj


def reverse_tax_adjustment(adjustment_id, reason, user, company=None):
    """
    Safely reverses an already-posted tax adjustment using the Blueprint #8 journal reversal pipeline.
    Preserves audit history and posted journal immutability.
    """
    with transaction.atomic():
        qs = TaxAdjustment.objects.select_for_update()
        if company:
            qs = qs.filter(company=company)

        try:
            adj = qs.get(pk=adjustment_id)
        except TaxAdjustment.DoesNotExist:
            raise ValidationError(_("Tax adjustment not found."))

        if adj.status != "posted":
            raise ValidationError(_(f"Only posted tax adjustments can be reversed (status: {adj.status})."))
        if not adj.journal_entry_id:
            raise ValidationError(_("Adjustment has no associated journal entry to reverse."))

        reversal_reason = reason or f"Reversal of tax adjustment {adj.adjustment_number}"
        rev_entry = reverse_journal_entry(
            entry_id=adj.journal_entry.id,
            user=user,
            reason=reversal_reason,
            company=adj.company,
        )

        adj.status = "reversed"
        adj.reversal_journal_entry = rev_entry
        adj.save()

        # Mark any corresponding tax lines reversed
        TaxTransactionLine.objects.filter(
            company=adj.company,
            source_module="tax_adjustment",
            source_id=str(adj.id),
        ).update(
            is_reversed=True,
            reversed_at=timezone.now(),
            reversal_reference=f"Reversed via {rev_entry.entry_number}",
            reversal_journal_entry=rev_entry,
        )

        TaxAuditLog.objects.create(
            company=adj.company,
            tax_code=adj.tax_code,
            tax_adjustment=adj,
            action="tax_adjustment_reversed",
            actor=user,
            details={
                "adjustment_number": adj.adjustment_number,
                "reversal_journal_id": rev_entry.id,
                "reversal_journal_number": rev_entry.entry_number,
                "reason": reason,
            }
        )

        return adj


def reverse_tax_transaction_line(tax_line_id, reason, user, company=None):
    """
    Reverses an individual transaction tax line. If linked to a journal entry,
    executes an auditable reversal through the #8 Double-Entry engine.
    """
    with transaction.atomic():
        qs = TaxTransactionLine.objects.select_for_update()
        if company:
            qs = qs.filter(company=company)

        try:
            line = qs.get(pk=tax_line_id)
        except TaxTransactionLine.DoesNotExist:
            raise ValidationError(_("Tax transaction line not found."))

        if line.is_reversed:
            raise ValidationError(_(f"Tax line #{tax_line_id} is already marked as reversed."))

        reversal_je = None
        if line.journal_entry_id and line.journal_entry.status == "posted":
            if not line.journal_entry.reversals.exists():
                reversal_je = reverse_journal_entry(
                    entry_id=line.journal_entry.id,
                    user=user,
                    reason=reason or f"Reversal of tax line #{line.id}",
                    company=line.company,
                )

        line.is_reversed = True
        line.reversed_at = timezone.now()
        line.reversal_reference = reason or "Transaction reversed"
        if reversal_je:
            line.reversal_journal_entry = reversal_je
        line.save()

        TaxAuditLog.objects.create(
            company=line.company,
            tax_code=line.tax_code,
            tax_line=line,
            action="tax_line_reversed",
            actor=user,
            details={
                "tax_line_id": line.id,
                "source_reference": line.source_reference,
                "tax_amount": float(line.tax_amount),
                "reason": reason,
                "reversal_journal_number": reversal_je.entry_number if reversal_je else None,
            }
        )

        return line


def get_tax_summary(company, start_date=None, end_date=None, tax_code_id=None, account_id=None):
    """
    Returns high-level KPI totals and breakdown matrices for the company tax layer.
    """
    qs = TaxTransactionLine.objects.filter(company=company, is_reversed=False)

    if start_date:
        qs = qs.filter(transaction_date__gte=start_date)
    if end_date:
        qs = qs.filter(transaction_date__lte=end_date)
    if tax_code_id:
        qs = qs.filter(tax_code_id=tax_code_id)
    if account_id:
        qs = qs.filter(tax_account_id=account_id)

    agg = qs.aggregate(
        total_taxable=Sum("taxable_amount"),
        total_tax=Sum("tax_amount"),
        total_gross=Sum("total_amount"),
        tx_count=Count("id")
    )

    total_taxable = agg["total_taxable"] or Decimal("0.00")
    total_tax = agg["total_tax"] or Decimal("0.00")
    total_gross = agg["total_gross"] or Decimal("0.00")
    total_tx_count = agg["tx_count"] or 0

    active_codes_count = TaxCode.objects.filter(company=company, is_active=True).count()

    # Tax breakdown by tax code
    by_code = []
    codes = TaxCode.objects.filter(company=company)
    for c in codes:
        code_qs = qs.filter(tax_code=c)
        code_agg = code_qs.aggregate(
            taxable=Sum("taxable_amount"),
            tax=Sum("tax_amount"),
            cnt=Count("id")
        )
        by_code.append({
            "tax_code_id": c.id,
            "code": c.code,
            "name": c.name,
            "rate": float(c.rate),
            "calculation_mode": c.calculation_mode,
            "tax_account_id": c.tax_account_id,
            "tax_account_name": c.tax_account.name if c.tax_account else "",
            "is_active": c.is_active,
            "taxable_amount": float(code_agg["taxable"] or 0),
            "tax_amount": float(code_agg["tax"] or 0),
            "transaction_count": code_agg["cnt"] or 0,
        })

    # Tax breakdown by tax account
    by_account = []
    acc_ids = qs.values_list("tax_account_id", flat=True).distinct()
    for acc_id in acc_ids:
        acc = Account.objects.filter(pk=acc_id).first()
        if not acc:
            continue
        acc_qs = qs.filter(tax_account=acc)
        acc_agg = acc_qs.aggregate(
            taxable=Sum("taxable_amount"),
            tax=Sum("tax_amount"),
            cnt=Count("id")
        )
        by_account.append({
            "account_id": acc.id,
            "account_code": acc.code,
            "account_name": acc.name,
            "category": acc.account_type.category if acc.account_type else "other",
            "taxable_amount": float(acc_agg["taxable"] or 0),
            "tax_amount": float(acc_agg["tax"] or 0),
            "transaction_count": acc_agg["cnt"] or 0,
        })

    # Breakdown by source module (sales vs purchases vs expenses vs tax_adjustment)
    by_module = []
    for mod in ["sales", "purchases", "expenses", "tax_adjustment", "manual"]:
        mod_qs = qs.filter(source_module=mod)
        mod_agg = mod_qs.aggregate(
            taxable=Sum("taxable_amount"),
            tax=Sum("tax_amount"),
            cnt=Count("id")
        )
        if mod_agg["cnt"]:
            by_module.append({
                "module": mod,
                "taxable_amount": float(mod_agg["taxable"] or 0),
                "tax_amount": float(mod_agg["tax"] or 0),
                "transaction_count": mod_agg["cnt"] or 0,
            })

    return {
        "total_taxable_amount": float(total_taxable),
        "total_tax_amount": float(total_tax),
        "total_gross_amount": float(total_gross),
        "total_transactions_count": total_tx_count,
        "active_tax_codes_count": active_codes_count,
        "by_code": by_code,
        "by_account": by_account,
        "by_module": by_module,
    }


def get_tax_report(company, start_date=None, end_date=None, tax_code_id=None, account_id=None, source_module=None):
    """
    Generates a transaction-level tax report and performs GL reconciliation.
    """
    qs = TaxTransactionLine.objects.filter(company=company).select_related(
        "tax_code", "tax_account", "journal_entry"
    )

    if start_date:
        qs = qs.filter(transaction_date__gte=start_date)
    if end_date:
        qs = qs.filter(transaction_date__lte=end_date)
    if tax_code_id:
        qs = qs.filter(tax_code_id=tax_code_id)
    if account_id:
        qs = qs.filter(tax_account_id=account_id)
    if source_module:
        qs = qs.filter(source_module=source_module)

    lines_data = []
    total_taxable = Decimal("0.00")
    total_tax = Decimal("0.00")
    total_reversed_tax = Decimal("0.00")

    for line in qs.order_by("-transaction_date", "-created_at"):
        is_rev = line.is_reversed
        lines_data.append({
            "id": line.id,
            "transaction_date": str(line.transaction_date),
            "source_module": line.source_module,
            "source_id": line.source_id,
            "source_reference": line.source_reference,
            "tax_code_id": line.tax_code_id,
            "tax_code": line.tax_code.code if line.tax_code else "CUSTOM",
            "tax_code_name": line.tax_code.name if line.tax_code else "",
            "tax_rate": float(line.tax_rate),
            "taxable_amount": float(line.taxable_amount),
            "tax_amount": float(line.tax_amount),
            "total_amount": float(line.total_amount),
            "calculation_mode": line.calculation_mode,
            "tax_account_id": line.tax_account_id,
            "tax_account_code": line.tax_account.code,
            "tax_account_name": line.tax_account.name,
            "journal_entry_id": line.journal_entry_id,
            "journal_number": line.journal_entry.entry_number if line.journal_entry else None,
            "is_posted": line.is_posted,
            "is_reversed": line.is_reversed,
            "reversed_at": str(line.reversed_at) if line.reversed_at else None,
            "reversal_reference": line.reversal_reference,
        })
        if not is_rev:
            total_taxable += line.taxable_amount
            total_tax += line.tax_amount
        else:
            total_reversed_tax += line.tax_amount

    # GL Reconciliation: compare sum of non-reversed tax lines against GL journal lines on tax accounts
    account_reconciliations = []
    tax_account_ids = qs.values_list("tax_account_id", flat=True).distinct()
    for acc_id in tax_account_ids:
        acc = Account.objects.filter(pk=acc_id).first()
        if not acc:
            continue
        tax_lines_sum = qs.filter(tax_account=acc, is_reversed=False).aggregate(s=Sum("tax_amount"))["s"] or Decimal("0.00")
        
        # Query posted GL journal entry lines for this account within date range
        gl_lines = JournalEntryLine.objects.filter(
            company=company,
            account=acc,
            journal_entry__status="posted",
        )
        if start_date:
            gl_lines = gl_lines.filter(journal_entry__transaction_date__gte=start_date)
        if end_date:
            gl_lines = gl_lines.filter(journal_entry__transaction_date__lte=end_date)

        gl_debit = gl_lines.aggregate(s=Sum("debit"))["s"] or Decimal("0.00")
        gl_credit = gl_lines.aggregate(s=Sum("credit"))["s"] or Decimal("0.00")
        
        # Normal balance
        gl_net = gl_credit - gl_debit if acc.account_type and acc.account_type.normal_balance == "credit" else gl_debit - gl_credit

        account_reconciliations.append({
            "account_id": acc.id,
            "account_code": acc.code,
            "account_name": acc.name,
            "category": acc.account_type.category if acc.account_type else "other",
            "tax_lines_total": float(tax_lines_sum),
            "gl_net_posted": float(gl_net),
            "variance": float(abs(tax_lines_sum - gl_net)),
            "is_reconciled": abs(tax_lines_sum - gl_net) < Decimal("0.05"),
        })

    return {
        "records": lines_data,
        "summary": {
            "total_taxable_amount": float(total_taxable),
            "total_tax_amount": float(total_tax),
            "total_reversed_tax_amount": float(total_reversed_tax),
            "records_count": len(lines_data),
        },
        "account_reconciliations": account_reconciliations,
    }


def seed_default_tax_codes(company, user=None):
    """
    Seeds standard baseline tax codes for a tenant company if none currently exist.
    Configures:
    - ZERO-0 (0% Zero-Rated / Exempt)
    - STD-10 (10% Standard Output Tax) -> 2200 Sales Tax Payable
    - INP-10 (10% Standard Input Tax) -> 1310 Input Tax Recoverable
    """
    sales_tax_acc = get_tax_payable_account(company)
    input_tax_acc = get_input_tax_account(company)

    created_count = 0

    defaults = [
        {
            "code": "ZERO-0",
            "name": "Zero-Rated / Tax Exempt (0%)",
            "description": "Exempt sales, export commodities, and zero-rated items",
            "rate": Decimal("0.0000"),
            "tax_type": "both",
            "calculation_mode": "exclusive",
            "tax_account": sales_tax_acc,
            "is_recoverable": False,
        },
        {
            "code": "STD-10",
            "name": "Standard Sales Tax (10%)",
            "description": "Standard output tax collected on commercial goods sales",
            "rate": Decimal("10.0000"),
            "tax_type": "sales",
            "calculation_mode": "exclusive",
            "tax_account": sales_tax_acc,
            "is_recoverable": True,
        },
        {
            "code": "INP-10",
            "name": "Standard Input Tax (10%)",
            "description": "Recoverable input tax paid on vendor purchases and commercial expenses",
            "rate": Decimal("10.0000"),
            "tax_type": "purchase",
            "calculation_mode": "exclusive",
            "tax_account": input_tax_acc,
            "is_recoverable": True,
        },
    ]

    for d in defaults:
        _, created = TaxCode.objects.get_or_create(
            company=company,
            code=d["code"],
            defaults={
                "name": d["name"],
                "description": d["description"],
                "rate": d["rate"],
                "tax_type": d["tax_type"],
                "calculation_mode": d["calculation_mode"],
                "tax_account": d["tax_account"],
                "is_recoverable": d["is_recoverable"],
                "is_active": True,
                "created_by": user,
            }
        )
        if created:
            created_count += 1

    return created_count
