"""
Accounting Dashboard Service Layer for Blueprint #23.

Provides authoritative executive KPIs and analytics derived directly from
double-entry General Ledger journals, subledgers, banking, and period controls:

1. Cash and bank balance summary (total liquidity + per-account breakdown)
2. Receivables due / overdue (total AR, current due, overdue, aging brackets, top debtors)
3. Payables due / overdue (total AP, current due, overdue, aging brackets, top creditors)
4. Revenue and expense trend (historical monthly breakdown: Revenue, COGS, OpEx, Net Profit)
5. Gross margin / profitability (Gross Margin %, Operating Margin %, Net Margin %)
6. Inventory value (total inventory + Raw Materials, WIP, Finished Goods)
7. Production cost indicators (Direct Materials, Labor, Applied Overhead, Prime Cost, Conversion Cost, COGM, Variances)
8. Unreconciled bank items (unreconciled transactions count & amount, last reconciliation status)
9. Pending approvals (submitted journals count & value, draft entries)
10. Period closing status (active FY, active period, open/locked/closed counts, closing readiness)
11. Recent accounting transactions (audited journal ledger rows with drill-down metadata)

Strictly multi-tenant (company scoped) and relies on posted journals as single source of truth.
"""

from decimal import Decimal
from datetime import date, timedelta
import calendar
from django.db import models
from django.db.models import Sum, Count, Q
from django.utils import timezone

from .models import (
    Account,
    AccountType,
    AccountingPeriod,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    AccountingSettings,
    BankAccount,
    BankTransaction,
    BankReconciliation,
)
from .receivables import get_ar_aging_report
from .payables import get_ap_aging_report


def resolve_dashboard_date_range(
    company,
    date_filter="this_month",
    start_date=None,
    end_date=None,
    fiscal_year_id=None,
    period_id=None,
):
    """
    Resolves standard date range for the executive dashboard.
    Supports: today, this_week, this_month, this_quarter, this_year, custom,
    or explicit fiscal_year_id / period_id.
    """
    today = timezone.now().date()

    if period_id:
        try:
            p = AccountingPeriod.objects.get(pk=period_id, company=company)
            return p.start_date, p.end_date, p.fiscal_year, p
        except AccountingPeriod.DoesNotExist:
            pass
    elif fiscal_year_id:
        try:
            fy = FiscalYear.objects.get(pk=fiscal_year_id, company=company)
            return fy.start_date, fy.end_date, fy, None
        except FiscalYear.DoesNotExist:
            pass

    if start_date and end_date:
        if isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = date.fromisoformat(end_date)
        return start_date, end_date, None, None

    if date_filter == "today":
        return today, today, None, None
    elif date_filter == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today, None, None
    elif date_filter == "this_quarter":
        current_quarter = (today.month - 1) // 3
        q_start_month = current_quarter * 3 + 1
        start = date(today.year, q_start_month, 1)
        return start, today, None, None
    elif date_filter == "this_year":
        # Check active fiscal year first
        fy = FiscalYear.objects.filter(
            company=company,
            start_date__lte=today,
            end_date__gte=today,
            is_closed=False,
        ).first()
        if fy:
            return fy.start_date, today, fy, None
        return date(today.year, 1, 1), today, None, None
    else:  # "this_month" default
        start = date(today.year, today.month, 1)
        return start, today, None, None


def get_cash_bank_summary(company, end_date=None):
    """
    Cash and bank balance summary:
    Total liquidity in GL plus individual bank account details.
    """
    if not end_date:
        end_date = timezone.now().date()

    valid_statuses = ["posted", "reversed"]

    # 1. GL Cash and Bank accounts (code 10xx, normal debit)
    cash_lines = JournalEntryLine.objects.filter(
        company=company,
        journal_entry__status__in=valid_statuses,
        journal_entry__transaction_date__lte=end_date,
        account__code__startswith="10",
    ).aggregate(tot_dr=Sum("debit"), tot_cr=Sum("credit"))

    tot_dr = cash_lines["tot_dr"] or Decimal("0.00")
    tot_cr = cash_lines["tot_cr"] or Decimal("0.00")
    total_cash_and_bank = tot_dr - tot_cr

    # 2. Bank accounts list
    bank_accounts = BankAccount.objects.filter(company=company, is_active=True).select_related("gl_account")
    accounts_list = []
    for ba in bank_accounts:
        accounts_list.append({
            "id": ba.id,
            "bank_name": ba.bank_name,
            "account_number_masked": ba.masked_account_number,
            "currency": ba.currency,
            "current_balance": ba.current_gl_balance,
            "ledger_account_code": ba.gl_account.code if ba.gl_account else "1020",
            "ledger_account_name": ba.gl_account.name if ba.gl_account else "",
        })

    return {
        "total_cash_and_bank": total_cash_and_bank,
        "accounts_count": len(accounts_list),
        "accounts": accounts_list,
    }


def get_receivables_kpis(company, end_date=None):
    """
    Accounts Receivable Due / Overdue KPIs and Aging:
    Total AR, current due, overdue, aging distribution, and top customer debtors.
    """
    if not end_date:
        end_date = timezone.now().date()

    try:
        ar_data = get_ar_aging_report(company=company, as_of_date=end_date)
        totals = ar_data.get("totals", {})
        total_ar = totals.get("total_receivables") or totals.get("total_ar") or Decimal("0.00")
        current_due = totals.get("current", Decimal("0.00"))
        # Overdue is all aging brackets > 0 days
        overdue = (
            totals.get("days_1_30", Decimal("0.00"))
            + totals.get("days_31_60", Decimal("0.00"))
            + totals.get("days_61_90", Decimal("0.00"))
            + totals.get("days_90_plus", Decimal("0.00"))
            + totals.get("days_91_120", Decimal("0.00"))
            + totals.get("days_over_120", Decimal("0.00"))
        )

        aging_brackets = [
            {"bracket": "Current (0-30 days)", "amount": current_due},
            {"bracket": "1-30 days overdue", "amount": totals.get("days_1_30", Decimal("0.00"))},
            {"bracket": "31-60 days overdue", "amount": totals.get("days_31_60", Decimal("0.00"))},
            {"bracket": "61-90 days overdue", "amount": totals.get("days_61_90", Decimal("0.00"))},
            {"bracket": "90+ days overdue", "amount": totals.get("days_90_plus", Decimal("0.00")) + totals.get("days_91_120", Decimal("0.00")) + totals.get("days_over_120", Decimal("0.00"))},
        ]

        # Top 5 customers with balance
        customers = ar_data.get("customers", [])
        sorted_customers = sorted(customers, key=lambda c: c.get("total_balance", Decimal("0.00")), reverse=True)[:5]
        top_customers = [
            {
                "customer_id": c.get("customer_id"),
                "customer_name": c.get("customer_name"),
                "total_balance": c.get("total_balance"),
                "overdue_balance": c.get("overdue_balance", Decimal("0.00")),
            }
            for c in sorted_customers
        ]

        return {
            "total_receivables": total_ar,
            "current_due": current_due,
            "overdue_receivables": overdue,
            "overdue_percentage": round((overdue / total_ar * 100), 2) if total_ar > 0 else Decimal("0.00"),
            "aging_brackets": aging_brackets,
            "top_debtors": top_customers,
        }
    except Exception:
        # Fallback to GL control account
        gl_ar = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=["posted", "reversed"],
            journal_entry__transaction_date__lte=end_date,
            account__code="1100",
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        tot = (gl_ar["dr"] or Decimal("0.00")) - (gl_ar["cr"] or Decimal("0.00"))
        return {
            "total_receivables": tot,
            "current_due": tot,
            "overdue_receivables": Decimal("0.00"),
            "overdue_percentage": Decimal("0.00"),
            "aging_brackets": [],
            "top_debtors": [],
        }


def get_payables_kpis(company, end_date=None):
    """
    Accounts Payable Due / Overdue KPIs and Aging:
    Total AP, current due, overdue, aging distribution, and top vendor creditors.
    """
    if not end_date:
        end_date = timezone.now().date()

    try:
        ap_data = get_ap_aging_report(company=company, as_of_date=end_date)
        totals = ap_data.get("totals", {})
        total_ap = totals.get("total_payables") or totals.get("total_ap") or Decimal("0.00")
        current_due = totals.get("current", Decimal("0.00"))
        overdue = (
            totals.get("days_1_30", Decimal("0.00"))
            + totals.get("days_31_60", Decimal("0.00"))
            + totals.get("days_61_90", Decimal("0.00"))
            + totals.get("days_90_plus", Decimal("0.00"))
            + totals.get("days_91_120", Decimal("0.00"))
            + totals.get("days_over_120", Decimal("0.00"))
        )

        aging_brackets = [
            {"bracket": "Current (0-30 days)", "amount": current_due},
            {"bracket": "1-30 days overdue", "amount": totals.get("days_1_30", Decimal("0.00"))},
            {"bracket": "31-60 days overdue", "amount": totals.get("days_31_60", Decimal("0.00"))},
            {"bracket": "61-90 days overdue", "amount": totals.get("days_61_90", Decimal("0.00"))},
            {"bracket": "90+ days overdue", "amount": totals.get("days_90_plus", Decimal("0.00")) + totals.get("days_91_120", Decimal("0.00")) + totals.get("days_over_120", Decimal("0.00"))},
        ]

        vendors = ap_data.get("vendors", [])
        sorted_vendors = sorted(vendors, key=lambda v: v.get("total_balance", Decimal("0.00")), reverse=True)[:5]
        top_vendors = [
            {
                "vendor_id": v.get("vendor_id"),
                "vendor_name": v.get("vendor_name"),
                "total_balance": v.get("total_balance"),
                "overdue_balance": v.get("overdue_balance", Decimal("0.00")),
            }
            for v in sorted_vendors
        ]

        return {
            "total_payables": total_ap,
            "current_due": current_due,
            "overdue_payables": overdue,
            "overdue_percentage": round((overdue / total_ap * 100), 2) if total_ap > 0 else Decimal("0.00"),
            "aging_brackets": aging_brackets,
            "top_creditors": top_vendors,
        }
    except Exception:
        gl_ap = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=["posted", "reversed"],
            journal_entry__transaction_date__lte=end_date,
            account__code="2010",
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        tot = (gl_ap["cr"] or Decimal("0.00")) - (gl_ap["dr"] or Decimal("0.00"))
        return {
            "total_payables": tot,
            "current_due": tot,
            "overdue_payables": Decimal("0.00"),
            "overdue_percentage": Decimal("0.00"),
            "aging_brackets": [],
            "top_creditors": [],
        }


def get_profitability_metrics(company, start_date, end_date):
    """
    Gross Margin and Profitability KPIs derived from posted P&L journal lines.
    """
    valid_statuses = ["posted", "reversed"]

    lines = (
        JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__gte=start_date,
            journal_entry__transaction_date__lte=end_date,
        )
        .exclude(journal_entry__entry_type="closing")
        .values("account__code", "account__account_type__category")
        .annotate(tot_dr=Sum("debit"), tot_cr=Sum("credit"))
    )

    total_revenue = Decimal("0.00")
    total_cogs = Decimal("0.00")
    total_opex = Decimal("0.00")
    other_expenses = Decimal("0.00")

    for r in lines:
        code = r["account__code"]
        cat = r["account__account_type__category"]
        dr = r["tot_dr"] or Decimal("0.00")
        cr = r["tot_cr"] or Decimal("0.00")

        if cat == "revenue":
            total_revenue += (cr - dr)
        elif cat == "expense":
            net_exp = (dr - cr)
            if code.startswith("5"):
                total_cogs += net_exp
            elif code.startswith("7"):
                other_expenses += net_exp
            else:
                total_opex += net_exp

    gross_profit = total_revenue - total_cogs
    gross_margin_pct = (
        round((gross_profit / total_revenue) * Decimal("100.00"), 2)
        if total_revenue != Decimal("0.00")
        else Decimal("0.00")
    )

    operating_income = gross_profit - total_opex
    operating_margin_pct = (
        round((operating_income / total_revenue) * Decimal("100.00"), 2)
        if total_revenue != Decimal("0.00")
        else Decimal("0.00")
    )

    net_profit = operating_income - other_expenses
    net_margin_pct = (
        round((net_profit / total_revenue) * Decimal("100.00"), 2)
        if total_revenue != Decimal("0.00")
        else Decimal("0.00")
    )

    return {
        "total_revenue": total_revenue,
        "total_cogs": total_cogs,
        "gross_profit": gross_profit,
        "gross_margin_pct": gross_margin_pct,
        "total_opex": total_opex,
        "operating_income": operating_income,
        "operating_margin_pct": operating_margin_pct,
        "other_expenses": other_expenses,
        "net_profit": net_profit,
        "net_margin_pct": net_margin_pct,
    }


def get_monthly_trends(company, num_months=6):
    """
    Monthly Revenue, Expenses, and Profit trend data over the past N months.
    """
    today = timezone.now().date()
    trends = []
    valid_statuses = ["posted", "reversed"]

    for i in range(num_months - 1, -1, -1):
        # Calculate year and month for (today - i months)
        m = today.month - i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1

        _, last_day = calendar.monthrange(y, m)
        m_start = date(y, m, 1)
        m_end = date(y, m, last_day)

        lines = (
            JournalEntryLine.objects.filter(
                company=company,
                journal_entry__status__in=valid_statuses,
                journal_entry__transaction_date__gte=m_start,
                journal_entry__transaction_date__lte=m_end,
            )
            .exclude(journal_entry__entry_type="closing")
            .values("account__code", "account__account_type__category")
            .annotate(tot_dr=Sum("debit"), tot_cr=Sum("credit"))
        )

        m_rev = Decimal("0.00")
        m_cogs = Decimal("0.00")
        m_opex = Decimal("0.00")

        for r in lines:
            code = r["account__code"]
            cat = r["account__account_type__category"]
            dr = r["tot_dr"] or Decimal("0.00")
            cr = r["tot_cr"] or Decimal("0.00")

            if cat == "revenue":
                m_rev += (cr - dr)
            elif cat == "expense":
                net = (dr - cr)
                if code.startswith("5"):
                    m_cogs += net
                else:
                    m_opex += net

        m_exp = m_cogs + m_opex
        m_profit = m_rev - m_exp

        trends.append({
            "month": m_start.strftime("%b %Y"),
            "year": y,
            "month_num": m,
            "revenue": m_rev,
            "cogs": m_cogs,
            "opex": m_opex,
            "total_expenses": m_exp,
            "net_profit": m_profit,
        })

    return trends


def get_inventory_metrics(company, end_date=None):
    """
    Inventory Value breakdown in the General Ledger:
    Total, Raw Materials (1210), WIP (1220), Finished Goods (1230).
    """
    if not end_date:
        end_date = timezone.now().date()

    valid_statuses = ["posted", "reversed"]

    def _balance(code):
        agg = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__lte=end_date,
            account__code=code,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        dr = agg["dr"] or Decimal("0.00")
        cr = agg["cr"] or Decimal("0.00")
        return dr - cr

    raw_mat = _balance("1210")
    wip = _balance("1220")
    finished_goods = _balance("1230")
    total_inventory = raw_mat + wip + finished_goods

    return {
        "total_inventory_value": total_inventory,
        "raw_materials_value": raw_mat,
        "work_in_progress_value": wip,
        "finished_goods_value": finished_goods,
        "breakdown": [
            {"category": "Raw Materials", "code": "1210", "value": raw_mat},
            {"category": "Work-in-Progress (WIP)", "code": "1220", "value": wip},
            {"category": "Finished Goods", "code": "1230", "value": finished_goods},
        ],
    }


def get_production_cost_indicators(company, start_date, end_date):
    """
    Production Cost Indicators:
    Direct Materials, Direct Labor, Applied Factory Overhead, Prime Cost, Conversion Cost, COGM, Variances.
    """
    valid_statuses = ["posted", "reversed"]

    def _net_debit(code_prefix):
        agg = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__gte=start_date,
            journal_entry__transaction_date__lte=end_date,
            account__code__startswith=code_prefix,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        dr = agg["dr"] or Decimal("0.00")
        cr = agg["cr"] or Decimal("0.00")
        return dr - cr

    direct_materials = _net_debit("501")
    direct_packaging = _net_debit("502")
    direct_labor = _net_debit("510")
    factory_overhead = _net_debit("52")
    scrap_loss = _net_debit("508")
    variance = _net_debit("509")

    prime_cost = direct_materials + direct_packaging + direct_labor
    conversion_cost = direct_labor + factory_overhead
    total_mfg_costs = prime_cost + factory_overhead

    # Net WIP change
    def _wip_balance(cutoff):
        agg = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__lte=cutoff,
            account__code="1220",
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        dr = agg["dr"] or Decimal("0.00")
        cr = agg["cr"] or Decimal("0.00")
        return dr - cr

    wip_start = _wip_balance(start_date - timedelta(days=1))
    wip_end = _wip_balance(end_date)
    wip_net_change = wip_start - wip_end
    cogm = total_mfg_costs + wip_net_change

    return {
        "direct_materials": direct_materials + direct_packaging,
        "direct_labor": direct_labor,
        "factory_overhead": factory_overhead,
        "prime_cost": prime_cost,
        "conversion_cost": conversion_cost,
        "total_manufacturing_costs": total_mfg_costs,
        "wip_net_change": wip_net_change,
        "cost_of_goods_manufactured": cogm,
        "scrap_loss": scrap_loss,
        "manufacturing_variances": variance,
    }


def get_unreconciled_bank_metrics(company):
    """
    Unreconciled bank items and last reconciliation status.
    """
    unreconciled_qs = BankTransaction.objects.filter(
        company=company,
        reconciliation_status="unreconciled",
    )
    count = unreconciled_qs.count()
    tot_amt = unreconciled_qs.aggregate(tot=Sum("amount"))["tot"] or Decimal("0.00")

    last_rec = (
        BankReconciliation.objects.filter(company=company)
        .order_by("-statement_date", "-id")
        .first()
    )

    return {
        "unreconciled_count": count,
        "unreconciled_amount": tot_amt,
        "last_reconciliation_date": str(last_rec.statement_date) if last_rec else None,
        "last_reconciliation_status": last_rec.status if last_rec else "none",
    }


def get_pending_approvals_metrics(company):
    """
    Pending journal entries and transactions awaiting review/approval.
    """
    submitted_qs = JournalEntry.objects.filter(company=company, status="submitted")
    submitted_count = submitted_qs.count()
    submitted_amt = (
        JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status="submitted",
        ).aggregate(tot=Sum("debit"))["tot"]
        or Decimal("0.00")
    )

    draft_count = JournalEntry.objects.filter(company=company, status="draft").count()

    return {
        "pending_journals_count": submitted_count,
        "pending_journals_amount": submitted_amt,
        "draft_journals_count": draft_count,
        "total_pending_count": submitted_count,
    }


def get_period_closing_status_metrics(company):
    """
    Period closing status: active FY, active period, count of open/locked/closed periods,
    and period closing readiness.
    """
    today = timezone.now().date()

    settings_obj = AccountingSettings.objects.filter(company=company).first()
    current_fy = settings_obj.current_fiscal_year if settings_obj else None
    if not current_fy:
        current_fy = FiscalYear.objects.filter(
            company=company,
            start_date__lte=today,
            end_date__gte=today,
            is_closed=False,
        ).first()

    current_period = None
    if current_fy:
        current_period = AccountingPeriod.objects.filter(
            company=company,
            fiscal_year=current_fy,
            start_date__lte=today,
            end_date__gte=today,
        ).first()

    open_periods_count = AccountingPeriod.objects.filter(
        company=company,
        fiscal_year=current_fy,
        status="open",
    ).count() if current_fy else 0

    locked_periods_count = AccountingPeriod.objects.filter(
        company=company,
        fiscal_year=current_fy,
        status="locked",
    ).count() if current_fy else 0

    closed_periods_count = AccountingPeriod.objects.filter(
        company=company,
        fiscal_year=current_fy,
        status="closed",
    ).count() if current_fy else 0

    # Readiness check: can close if current period has no draft journals
    has_drafts = False
    if current_period:
        has_drafts = JournalEntry.objects.filter(
            company=company,
            accounting_period=current_period,
            status__in=["draft", "submitted"],
        ).exists()

    return {
        "fiscal_year": {
            "id": current_fy.id if current_fy else None,
            "name": current_fy.name if current_fy else "None",
            "is_closed": current_fy.is_closed if current_fy else False,
            "start_date": str(current_fy.start_date) if current_fy else None,
            "end_date": str(current_fy.end_date) if current_fy else None,
        } if current_fy else None,
        "current_period": {
            "id": current_period.id if current_period else None,
            "name": current_period.name if current_period else "None",
            "period_number": current_period.period_number if current_period else 0,
            "status": current_period.status if current_period else "none",
        } if current_period else None,
        "open_periods_count": open_periods_count,
        "locked_periods_count": locked_periods_count,
        "closed_periods_count": closed_periods_count,
        "is_ready_to_close": not has_drafts and current_period is not None and current_period.status != "closed",
    }


def get_recent_transactions(company, limit=10):
    """
    Recent accounting transactions for the dashboard activity feed.
    """
    entries = (
        JournalEntry.objects.filter(company=company)
        .order_by("-transaction_date", "-id")[:limit]
    )

    rows = []
    for je in entries:
        # Sum debits for total amount
        tot_deb = (
            JournalEntryLine.objects.filter(journal_entry=je).aggregate(tot=Sum("debit"))["tot"]
            or Decimal("0.00")
        )
        rows.append({
            "id": je.id,
            "entry_number": je.entry_number,
            "transaction_date": str(je.transaction_date),
            "reference": je.reference,
            "description": je.description,
            "entry_type": je.entry_type,
            "status": je.status,
            "source_module": je.source_module,
            "amount": tot_deb,
        })
    return rows


def get_accounting_dashboard_data(
    company,
    date_filter="this_month",
    start_date=None,
    end_date=None,
    fiscal_year_id=None,
    period_id=None,
):
    """
    Master aggregator assembling all 10 Blueprint #23 dashboard sections into a unified response.
    """
    s_date, e_date, fy, period = resolve_dashboard_date_range(
        company=company,
        date_filter=date_filter,
        start_date=start_date,
        end_date=end_date,
        fiscal_year_id=fiscal_year_id,
        period_id=period_id,
    )

    settings_obj = AccountingSettings.objects.filter(company=company).first()
    currency = settings_obj.default_currency if settings_obj else "USD"

    # Assemble all 10 core sections
    cash_bank = get_cash_bank_summary(company=company, end_date=e_date)
    receivables = get_receivables_kpis(company=company, end_date=e_date)
    payables = get_payables_kpis(company=company, end_date=e_date)
    profitability = get_profitability_metrics(company=company, start_date=s_date, end_date=e_date)
    trends = get_monthly_trends(company=company, num_months=6)
    inventory = get_inventory_metrics(company=company, end_date=e_date)
    production_cost = get_production_cost_indicators(company=company, start_date=s_date, end_date=e_date)
    unreconciled = get_unreconciled_bank_metrics(company=company)
    pending_approvals = get_pending_approvals_metrics(company=company)
    period_status = get_period_closing_status_metrics(company=company)
    recent_transactions = get_recent_transactions(company=company, limit=10)

    return {
        "company_name": company.name,
        "currency": currency,
        "date_filter": date_filter,
        "start_date": str(s_date),
        "end_date": str(e_date),
        "fiscal_year_id": fy.id if fy else None,
        "period_id": period.id if period else None,
        "cash_bank": cash_bank,
        "receivables": receivables,
        "payables": payables,
        "profitability": profitability,
        "revenue_expense_trends": trends,
        "inventory": inventory,
        "production_cost": production_cost,
        "unreconciled_bank": unreconciled,
        "pending_approvals": pending_approvals,
        "period_closing": period_status,
        "recent_transactions": recent_transactions,
    }
