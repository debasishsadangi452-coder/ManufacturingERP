"""
General Ledger Service Layer for Blueprint #9.

Computes account-level transactional ledgers, running balances, opening and closing
balances, and company-wide General Ledger summaries derived strictly from posted
double-entry JournalEntry and JournalEntryLine records.
"""

from decimal import Decimal
from django.db import models
from django.db.models import Sum, Count, Q
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from .models import Account, JournalEntry, JournalEntryLine, AccountingPeriod, FiscalYear


def get_account_ledger(
    company,
    account_id,
    start_date=None,
    end_date=None,
    fiscal_year_id=None,
    accounting_period_id=None,
    status=None,
    search=None,
):
    """
    Computes the complete, chronological General Ledger statement for a specific account.
    Derives opening balance from transactions prior to start_date, and maintains
    a continuous running balance across all period transactions.
    """
    try:
        account = Account.objects.select_related("account_type").get(
            pk=account_id,
            company=company
        )
    except Account.DoesNotExist:
        raise ValidationError(_("Account not found or does not belong to this company."))

    normal_balance = account.account_type.normal_balance  # "debit" or "credit"
    is_debit_normal = (normal_balance == "debit")

    # Base query for all posted/reversed entries (drafts strictly excluded)
    valid_statuses = ["posted", "reversed"]
    if status and status in valid_statuses:
        allowed_statuses = [status]
    else:
        allowed_statuses = valid_statuses

    # Determine date window if fiscal year or period is specified
    if accounting_period_id:
        try:
            period = AccountingPeriod.objects.get(pk=accounting_period_id, company=company)
            if not start_date:
                start_date = period.start_date
            if not end_date:
                end_date = period.end_date
        except AccountingPeriod.DoesNotExist:
            pass
    elif fiscal_year_id:
        try:
            fy = FiscalYear.objects.get(pk=fiscal_year_id, company=company)
            if not start_date:
                start_date = fy.start_date
            if not end_date:
                end_date = fy.end_date
        except FiscalYear.DoesNotExist:
            pass

    # 1. Compute Opening Balance (prior to start_date)
    opening_balance = Decimal("0.00")
    prior_debit = Decimal("0.00")
    prior_credit = Decimal("0.00")

    if start_date:
        prior_agg = JournalEntryLine.objects.filter(
            company=company,
            account=account,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__lt=start_date
        ).aggregate(
            tot_debit=Sum("debit"),
            tot_credit=Sum("credit")
        )
        prior_debit = prior_agg["tot_debit"] or Decimal("0.00")
        prior_credit = prior_agg["tot_credit"] or Decimal("0.00")

        if is_debit_normal:
            opening_balance = prior_debit - prior_credit
        else:
            opening_balance = prior_credit - prior_debit

    # 2. Query Period Transactions
    period_qs = JournalEntryLine.objects.filter(
        company=company,
        account=account,
        journal_entry__status__in=allowed_statuses,
    ).select_related(
        "journal_entry",
        "journal_entry__accounting_period",
        "account",
        "account__account_type",
    )

    if start_date:
        period_qs = period_qs.filter(journal_entry__transaction_date__gte=start_date)
    if end_date:
        period_qs = period_qs.filter(journal_entry__transaction_date__lte=end_date)
    if search:
        period_qs = period_qs.filter(
            Q(journal_entry__entry_number__icontains=search)
            | Q(journal_entry__reference__icontains=search)
            | Q(journal_entry__description__icontains=search)
            | Q(description__icontains=search)
        )

    # Order chronologically for running balance calculation
    period_qs = period_qs.order_by(
        "journal_entry__transaction_date",
        "journal_entry__entry_number",
        "line_number",
        "id"
    )

    # 3. Calculate running balance per row
    running_balance = opening_balance
    period_total_debit = Decimal("0.00")
    period_total_credit = Decimal("0.00")

    transactions = []
    for line in period_qs:
        debit = line.debit
        credit = line.credit
        period_total_debit += debit
        period_total_credit += credit

        if is_debit_normal:
            running_balance += (debit - credit)
        else:
            running_balance += (credit - debit)

        # Determine balance side for display
        if is_debit_normal:
            balance_side = "DR" if running_balance >= Decimal("0.00") else "CR"
        else:
            balance_side = "CR" if running_balance >= Decimal("0.00") else "DR"

        transactions.append({
            "id": line.id,
            "entry_id": line.journal_entry_id,
            "entry_number": line.journal_entry.entry_number,
            "transaction_date": str(line.journal_entry.transaction_date),
            "reference": line.journal_entry.reference,
            "entry_description": line.journal_entry.description,
            "line_description": line.description,
            "period_id": line.journal_entry.accounting_period_id,
            "period_name": line.journal_entry.accounting_period.name if line.journal_entry.accounting_period else "",
            "status": line.journal_entry.status,
            "source_module": line.journal_entry.source_module,
            "reversal_of_id": line.journal_entry.reversal_of_id,
            "line_number": line.line_number,
            "debit": str(debit),
            "credit": str(credit),
            "running_balance": str(running_balance),
            "balance_side": balance_side,
        })

    closing_balance = running_balance
    net_change = closing_balance - opening_balance

    return {
        "account": {
            "id": account.id,
            "code": account.code,
            "name": account.name,
            "account_type_id": account.account_type_id,
            "account_type_name": account.account_type.name,
            "category": account.account_type.category,
            "normal_balance": account.account_type.normal_balance,
            "currency": account.currency,
            "is_header": account.is_header,
        },
        "filters": {
            "start_date": str(start_date) if start_date else None,
            "end_date": str(end_date) if end_date else None,
            "fiscal_year_id": fiscal_year_id,
            "accounting_period_id": accounting_period_id,
            "status": status,
            "search": search,
        },
        "summary": {
            "opening_balance": str(opening_balance),
            "opening_balance_side": ("DR" if opening_balance >= 0 else "CR") if is_debit_normal else ("CR" if opening_balance >= 0 else "DR"),
            "period_total_debit": str(period_total_debit),
            "period_total_credit": str(period_total_credit),
            "net_change": str(net_change),
            "closing_balance": str(closing_balance),
            "closing_balance_side": ("DR" if closing_balance >= 0 else "CR") if is_debit_normal else ("CR" if closing_balance >= 0 else "DR"),
            "transaction_count": len(transactions),
        },
        "transactions": transactions,
    }


def get_general_ledger_summary(
    company,
    fiscal_year_id=None,
    accounting_period_id=None,
    start_date=None,
    end_date=None,
    category=None,
    search=None,
):
    """
    Computes summary General Ledger metrics (Opening, Debits, Credits, Closing)
    for all active accounts in the company.
    Optimized with batch aggregation queries.
    """
    valid_statuses = ["posted", "reversed"]

    # Resolve date boundaries from period/year if provided
    if accounting_period_id:
        try:
            period = AccountingPeriod.objects.get(pk=accounting_period_id, company=company)
            if not start_date:
                start_date = period.start_date
            if not end_date:
                end_date = period.end_date
        except AccountingPeriod.DoesNotExist:
            pass
    elif fiscal_year_id:
        try:
            fy = FiscalYear.objects.get(pk=fiscal_year_id, company=company)
            if not start_date:
                start_date = fy.start_date
            if not end_date:
                end_date = fy.end_date
        except FiscalYear.DoesNotExist:
            pass

    # Period filter for lines
    period_line_filter = Q(company=company, journal_entry__status__in=valid_statuses)
    if start_date:
        period_line_filter &= Q(journal_entry__transaction_date__gte=start_date)
    if end_date:
        period_line_filter &= Q(journal_entry__transaction_date__lte=end_date)

    # 1. Batch Period Activity
    period_activity = (
        JournalEntryLine.objects.filter(period_line_filter)
        .values("account_id")
        .annotate(
            total_debit=Sum("debit"),
            total_credit=Sum("credit"),
            tx_count=Count("id")
        )
    )
    period_map = {
        row["account_id"]: {
            "total_debit": row["total_debit"] or Decimal("0.00"),
            "total_credit": row["total_credit"] or Decimal("0.00"),
            "tx_count": row["tx_count"],
        }
        for row in period_activity
    }

    # 2. Batch Prior Activity (Opening Balance)
    prior_map = {}
    if start_date:
        prior_activity = (
            JournalEntryLine.objects.filter(
                company=company,
                journal_entry__status__in=valid_statuses,
                journal_entry__transaction_date__lt=start_date
            )
            .values("account_id")
            .annotate(
                prior_debit=Sum("debit"),
                prior_credit=Sum("credit")
            )
        )
        prior_map = {
            row["account_id"]: {
                "prior_debit": row["prior_debit"] or Decimal("0.00"),
                "prior_credit": row["prior_credit"] or Decimal("0.00"),
            }
            for row in prior_activity
        }

    # 3. Assemble Accounts
    accounts_qs = Account.objects.filter(
        company=company,
        is_active=True
    ).select_related("account_type").order_by("code")

    if category:
        accounts_qs = accounts_qs.filter(account_type__category=category)
    if search:
        accounts_qs = accounts_qs.filter(
            Q(code__icontains=search) | Q(name__icontains=search)
        )

    accounts_summary = []
    grand_total_debit = Decimal("0.00")
    grand_total_credit = Decimal("0.00")

    for acc in accounts_qs:
        is_debit_normal = (acc.account_type.normal_balance == "debit")

        # Opening balance
        prior = prior_map.get(acc.id, {"prior_debit": Decimal("0.00"), "prior_credit": Decimal("0.00")})
        if is_debit_normal:
            opening_bal = prior["prior_debit"] - prior["prior_credit"]
        else:
            opening_bal = prior["prior_credit"] - prior["prior_debit"]

        # Period activity
        activity = period_map.get(acc.id, {"total_debit": Decimal("0.00"), "total_credit": Decimal("0.00"), "tx_count": 0})
        deb = activity["total_debit"]
        cred = activity["total_credit"]
        tx_count = activity["tx_count"]

        grand_total_debit += deb
        grand_total_credit += cred

        if is_debit_normal:
            closing_bal = opening_bal + (deb - cred)
            net_change = deb - cred
        else:
            closing_bal = opening_bal + (cred - deb)
            net_change = cred - deb

        accounts_summary.append({
            "account_id": acc.id,
            "code": acc.code,
            "name": acc.name,
            "category": acc.account_type.category,
            "normal_balance": acc.account_type.normal_balance,
            "currency": acc.currency,
            "is_header": acc.is_header,
            "opening_balance": str(opening_bal),
            "period_debit": str(deb),
            "period_credit": str(cred),
            "net_change": str(net_change),
            "closing_balance": str(closing_bal),
            "transaction_count": tx_count,
        })

    return {
        "filters": {
            "start_date": str(start_date) if start_date else None,
            "end_date": str(end_date) if end_date else None,
            "fiscal_year_id": fiscal_year_id,
            "accounting_period_id": accounting_period_id,
            "category": category,
            "search": search,
        },
        "totals": {
            "total_accounts": len(accounts_summary),
            "grand_total_debit": str(grand_total_debit),
            "grand_total_credit": str(grand_total_credit),
            "is_balanced": abs(grand_total_debit - grand_total_credit) < Decimal("0.005"),
        },
        "accounts": accounts_summary,
    }
