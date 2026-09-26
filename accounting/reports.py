"""
Financial Reports Service Layer for Blueprint #22.

Implements the official Financial Reports suite for Manufacturing ERP:
1. Trial Balance (with equilibrium verification and comparative periods)
2. General Ledger statements
3. Profit & Loss / Income Statement (Revenue, COGS, OpEx, Operating Income, Net Income, Comparatives)
4. Balance Sheet (Assets, Liabilities, Equity, Retained Earnings, Current Net Income, Comparatives)
5. Statement of Cash Flows (Indirect method: Operating, Investing, Financing, Cash Reconciliation)
6. Accounts Receivable Aging
7. Accounts Payable Aging
8. Tax Reports & GL Reconciliation
9. Inventory Valuation Report (Raw Materials, WIP, Finished Goods movements)
10. Manufacturing Cost & Variance Report (Direct Materials, Labor, Applied Overhead, Scrap, Variances)
11. Multi-Account Reconciliation Report (GL Control Accounts vs Subledgers)
12. Period Comparison and Transaction Drill-Down
13. Export Engine (CSV / JSON format)

Strictly multi-tenant (company scoped) and uses double-entry posted journals as the single source of truth.
"""

from decimal import Decimal
from datetime import date, timedelta
import csv
import io
from django.db import models
from django.db.models import Sum, Count, Q
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.utils import timezone

from .models import (
    Account,
    AccountType,
    AccountingPeriod,
    FiscalYear,
    JournalEntry,
    JournalEntryLine,
    AccountingSettings,
)
from .general_ledger import get_account_ledger, get_general_ledger_summary
from .receivables import get_ar_aging_report
from .payables import get_ap_aging_report
from .tax import get_tax_report
from .period_closing import get_period_trial_balance


def resolve_report_period(
    company,
    period_id=None,
    fiscal_year_id=None,
    start_date=None,
    end_date=None,
    as_of_date=None,
    compare_to=None,
):
    """
    Standardizes date range resolution for financial reports.
    Computes primary date range and comparative date range if requested.
    """
    period = None
    fiscal_year = None

    if period_id:
        try:
            period = AccountingPeriod.objects.get(pk=period_id, company=company)
            start_date = period.start_date
            end_date = period.end_date
            fiscal_year = period.fiscal_year
        except AccountingPeriod.DoesNotExist:
            raise ValidationError(_("Specified accounting period does not exist or does not belong to this company."))
    elif fiscal_year_id:
        try:
            fiscal_year = FiscalYear.objects.get(pk=fiscal_year_id, company=company)
            start_date = fiscal_year.start_date
            end_date = fiscal_year.end_date
        except FiscalYear.DoesNotExist:
            raise ValidationError(_("Specified fiscal year does not exist or does not belong to this company."))
    elif as_of_date:
        if isinstance(as_of_date, str):
            as_of_date = date.fromisoformat(as_of_date)
        end_date = as_of_date
        if not start_date:
            # Attempt to find fiscal year covering as_of_date
            fy = FiscalYear.objects.filter(
                company=company,
                start_date__lte=as_of_date,
                end_date__gte=as_of_date,
            ).first()
            if fy:
                start_date = fy.start_date
                fiscal_year = fy
            else:
                start_date = date(as_of_date.year, 1, 1)

    # Convert string dates if necessary
    if start_date and isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if end_date and isinstance(end_date, str):
        end_date = date.fromisoformat(end_date)

    # Defaults if completely unconstrained
    if not end_date:
        end_date = timezone.now().date()
    if not start_date:
        start_date = date(end_date.year, 1, 1)

    # Comparative period computation
    cmp_start_date = None
    cmp_end_date = None
    if compare_to:
        duration_days = (end_date - start_date).days + 1
        if compare_to == "previous_period":
            cmp_end_date = start_date - timedelta(days=1)
            cmp_start_date = cmp_end_date - timedelta(days=duration_days - 1)
        elif compare_to == "previous_year":
            try:
                cmp_start_date = start_date.replace(year=start_date.year - 1)
                cmp_end_date = end_date.replace(year=end_date.year - 1)
            except ValueError:
                cmp_start_date = start_date - timedelta(days=365)
                cmp_end_date = end_date - timedelta(days=365)

    return {
        "period": period,
        "fiscal_year": fiscal_year,
        "start_date": start_date,
        "end_date": end_date,
        "as_of_date": end_date,
        "compare_to": compare_to,
        "cmp_start_date": cmp_start_date,
        "cmp_end_date": cmp_end_date,
    }


def _calc_variance(current, prior):
    """Computes dollar variance and percentage variance."""
    current = current or Decimal("0.00")
    prior = prior or Decimal("0.00")
    variance = current - prior
    if prior != Decimal("0.00"):
        pct = round((variance / abs(prior)) * Decimal("100.00"), 2)
    else:
        pct = Decimal("100.00") if current != Decimal("0.00") else Decimal("0.00")
    return variance, pct


def get_profit_and_loss_report(
    company,
    start_date=None,
    end_date=None,
    period_id=None,
    fiscal_year_id=None,
    compare_to=None,
):
    """
    Computes Profit & Loss (Income Statement) derived from posted double-entry journal lines.
    
    Structure:
    - Revenue (Operating Revenue, Other Income)
    - Cost of Goods Sold (Raw materials, direct labor, applied overhead, variances)
    - Gross Profit & Margin %
    - Operating Expenses (SG&A, payroll, utilities, depreciation)
    - Operating Income & Margin %
    - Other Income / Expenses
    - Net Profit / (Loss) & Net Margin %
    - Comparative period columns (Variance $ and %)
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        start_date=start_date,
        end_date=end_date,
        compare_to=compare_to,
    )
    s_date = dates["start_date"]
    e_date = dates["end_date"]
    cmp_s = dates["cmp_start_date"]
    cmp_e = dates["cmp_end_date"]

    valid_statuses = ["posted", "reversed"]

    def _query_activity(st, en):
        # Exclude official closing entries from P&L calculation so operational reporting isn't zeroed
        return (
            JournalEntryLine.objects.filter(
                company=company,
                journal_entry__status__in=valid_statuses,
                journal_entry__transaction_date__gte=st,
                journal_entry__transaction_date__lte=en,
            )
            .exclude(journal_entry__entry_type="closing")
            .values(
                "account_id",
                "account__code",
                "account__name",
                "account__account_type__category",
                "account__account_type__name",
                "account__account_type__normal_balance",
            )
            .annotate(
                total_debit=Sum("debit"),
                total_credit=Sum("credit"),
                count=Count("id"),
            )
        )

    current_data = _query_activity(s_date, e_date)
    prior_data = _query_activity(cmp_s, cmp_e) if (cmp_s and cmp_e) else []

    # Index prior period activity
    prior_map = {}
    for r in prior_data:
        acc_id = r["account_id"]
        cat = r["account__account_type__category"]
        deb = r["total_debit"] or Decimal("0.00")
        crd = r["total_credit"] or Decimal("0.00")
        if cat == "revenue":
            prior_net = crd - deb
        else:
            prior_net = deb - crd
        prior_map[acc_id] = prior_net

    # Classify current accounts
    revenue_rows = []
    cogs_rows = []
    opex_rows = []
    other_exp_rows = []

    total_revenue = Decimal("0.00")
    total_cogs = Decimal("0.00")
    total_opex = Decimal("0.00")
    total_other_exp = Decimal("0.00")

    prior_total_revenue = Decimal("0.00")
    prior_total_cogs = Decimal("0.00")
    prior_total_opex = Decimal("0.00")
    prior_total_other_exp = Decimal("0.00")

    seen_accounts = set()

    for r in current_data:
        acc_id = r["account_id"]
        seen_accounts.add(acc_id)
        code = r["account__code"]
        name = r["account__name"]
        cat = r["account__account_type__category"]
        type_name = r["account__account_type__name"]
        deb = r["total_debit"] or Decimal("0.00")
        crd = r["total_credit"] or Decimal("0.00")

        prior_amt = prior_map.get(acc_id, Decimal("0.00"))

        if cat == "revenue":
            # Net revenue = credit - debit
            current_amt = crd - deb
            var_amt, var_pct = _calc_variance(current_amt, prior_amt)
            row = {
                "account_id": acc_id,
                "code": code,
                "name": name,
                "account_type": type_name,
                "amount": current_amt,
                "prior_amount": prior_amt,
                "variance_amount": var_amt,
                "variance_pct": var_pct,
            }
            revenue_rows.append(row)
            total_revenue += current_amt
            prior_total_revenue += prior_amt

        elif cat == "expense":
            # Net expense = debit - credit
            current_amt = deb - crd
            var_amt, var_pct = _calc_variance(current_amt, prior_amt)
            row = {
                "account_id": acc_id,
                "code": code,
                "name": name,
                "account_type": type_name,
                "amount": current_amt,
                "prior_amount": prior_amt,
                "variance_amount": var_amt,
                "variance_pct": var_pct,
            }

            # Check if Cost of Goods Sold
            is_cogs = (
                code.startswith("5")
                or "cost of goods sold" in type_name.lower()
                or "cogs" in type_name.lower()
                or "raw materials" in name.lower()
                or "packaging" in name.lower()
                or "direct labor" in name.lower()
                or "overhead" in name.lower()
            )
            is_other = code.startswith("7") or "other expense" in type_name.lower() or "tax" in type_name.lower()

            if is_cogs:
                cogs_rows.append(row)
                total_cogs += current_amt
                prior_total_cogs += prior_amt
            elif is_other:
                other_exp_rows.append(row)
                total_other_exp += current_amt
                prior_total_other_exp += prior_amt
            else:
                opex_rows.append(row)
                total_opex += current_amt
                prior_total_opex += prior_amt

    # If comparative mode, add accounts present only in prior period
    if cmp_s and cmp_e:
        for r in prior_data:
            acc_id = r["account_id"]
            if acc_id not in seen_accounts:
                code = r["account__code"]
                name = r["account__name"]
                cat = r["account__account_type__category"]
                type_name = r["account__account_type__name"]
                prior_amt = prior_map.get(acc_id, Decimal("0.00"))
                var_amt, var_pct = _calc_variance(Decimal("0.00"), prior_amt)

                row = {
                    "account_id": acc_id,
                    "code": code,
                    "name": name,
                    "account_type": type_name,
                    "amount": Decimal("0.00"),
                    "prior_amount": prior_amt,
                    "variance_amount": var_amt,
                    "variance_pct": var_pct,
                }
                if cat == "revenue":
                    revenue_rows.append(row)
                    prior_total_revenue += prior_amt
                elif cat == "expense":
                    is_cogs = code.startswith("5") or "cogs" in type_name.lower()
                    is_other = code.startswith("7") or "other" in type_name.lower()
                    if is_cogs:
                        cogs_rows.append(row)
                        prior_total_cogs += prior_amt
                    elif is_other:
                        other_exp_rows.append(row)
                        prior_total_other_exp += prior_amt
                    else:
                        opex_rows.append(row)
                        prior_total_opex += prior_amt

    # Sort rows by account code
    revenue_rows.sort(key=lambda x: x["code"])
    cogs_rows.sort(key=lambda x: x["code"])
    opex_rows.sort(key=lambda x: x["code"])
    other_exp_rows.sort(key=lambda x: x["code"])

    # Profit calculations
    gross_profit = total_revenue - total_cogs
    prior_gross_profit = prior_total_revenue - prior_total_cogs
    gross_var, gross_pct = _calc_variance(gross_profit, prior_gross_profit)
    gross_margin_pct = (
        round((gross_profit / total_revenue) * Decimal("100.00"), 2)
        if total_revenue != Decimal("0.00")
        else Decimal("0.00")
    )

    operating_income = gross_profit - total_opex
    prior_operating_income = prior_gross_profit - prior_total_opex
    op_var, op_pct = _calc_variance(operating_income, prior_operating_income)
    operating_margin_pct = (
        round((operating_income / total_revenue) * Decimal("100.00"), 2)
        if total_revenue != Decimal("0.00")
        else Decimal("0.00")
    )

    net_profit = operating_income - total_other_exp
    prior_net_profit = prior_operating_income - prior_total_other_exp
    net_var, net_pct = _calc_variance(net_profit, prior_net_profit)
    net_margin_pct = (
        round((net_profit / total_revenue) * Decimal("100.00"), 2)
        if total_revenue != Decimal("0.00")
        else Decimal("0.00")
    )

    rev_var, rev_pct = _calc_variance(total_revenue, prior_total_revenue)
    cogs_var, cogs_pct = _calc_variance(total_cogs, prior_total_cogs)
    opex_var, opex_pct = _calc_variance(total_opex, prior_total_opex)
    other_var, other_pct = _calc_variance(total_other_exp, prior_total_other_exp)

    settings = AccountingSettings.objects.filter(company=company).first()
    currency = settings.default_currency if settings else "USD"

    return {
        "report_type": "profit_and_loss",
        "company_name": company.name,
        "currency": currency,
        "start_date": str(s_date),
        "end_date": str(e_date),
        "period_id": dates["period"].id if dates["period"] else None,
        "period_name": dates["period"].name if dates["period"] else "",
        "fiscal_year_id": dates["fiscal_year"].id if dates["fiscal_year"] else None,
        "fiscal_year_name": dates["fiscal_year"].name if dates["fiscal_year"] else "",
        "compare_to": compare_to,
        "comparative_start_date": str(cmp_s) if cmp_s else None,
        "comparative_end_date": str(cmp_e) if cmp_e else None,
        "sections": {
            "revenue": {
                "title": "Revenue & Operating Income",
                "accounts": revenue_rows,
                "total": total_revenue,
                "prior_total": prior_total_revenue,
                "variance_amount": rev_var,
                "variance_pct": rev_pct,
            },
            "cogs": {
                "title": "Cost of Goods Sold (COGS)",
                "accounts": cogs_rows,
                "total": total_cogs,
                "prior_total": prior_total_cogs,
                "variance_amount": cogs_var,
                "variance_pct": cogs_pct,
            },
            "gross_profit": {
                "title": "Gross Profit",
                "amount": gross_profit,
                "margin_pct": gross_margin_pct,
                "prior_amount": prior_gross_profit,
                "variance_amount": gross_var,
                "variance_pct": gross_pct,
            },
            "operating_expenses": {
                "title": "Operating Expenses (OpEx)",
                "accounts": opex_rows,
                "total": total_opex,
                "prior_total": prior_total_opex,
                "variance_amount": opex_var,
                "variance_pct": opex_pct,
            },
            "operating_income": {
                "title": "Operating Income (EBIT)",
                "amount": operating_income,
                "margin_pct": operating_margin_pct,
                "prior_amount": prior_operating_income,
                "variance_amount": op_var,
                "variance_pct": op_pct,
            },
            "other_expenses": {
                "title": "Other Expenses & Taxes",
                "accounts": other_exp_rows,
                "total": total_other_exp,
                "prior_total": prior_total_other_exp,
                "variance_amount": other_var,
                "variance_pct": other_pct,
            },
            "net_profit": {
                "title": "Net Profit / (Loss)",
                "amount": net_profit,
                "margin_pct": net_margin_pct,
                "prior_amount": prior_net_profit,
                "variance_amount": net_var,
                "variance_pct": net_pct,
            },
        },
        "summary": {
            "total_revenue": total_revenue,
            "total_cogs": total_cogs,
            "gross_profit": gross_profit,
            "gross_margin_pct": gross_margin_pct,
            "total_opex": total_opex,
            "operating_income": operating_income,
            "net_profit": net_profit,
            "net_margin_pct": net_margin_pct,
        },
    }


def get_balance_sheet_report(
    company,
    as_of_date=None,
    period_id=None,
    fiscal_year_id=None,
    compare_to=None,
):
    """
    Computes a cumulative Balance Sheet as of a specific date or period.
    
    Validates:
    Total Assets = Total Liabilities + Total Equity
    
    Includes:
    - Current Assets (Cash, AR, Inventory, Prepaids)
    - Non-Current / Fixed Assets (Machinery, Vehicles, Acc. Depreciation)
    - Current Liabilities (AP, Accrued Payroll, Taxes)
    - Non-Current Liabilities (Long-Term Debt)
    - Equity (Share Capital, Retained Earnings, Current Period Net Income)
    - Mathematical equilibrium verification
    - Comparative period variance
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        as_of_date=as_of_date,
        compare_to=compare_to,
    )
    as_of = dates["as_of_date"]
    cmp_as_of = dates["cmp_end_date"]

    valid_statuses = ["posted", "reversed"]

    def _query_balances(cutoff_date):
        lines = (
            JournalEntryLine.objects.filter(
                company=company,
                journal_entry__status__in=valid_statuses,
                journal_entry__transaction_date__lte=cutoff_date,
            )
            .values(
                "account_id",
                "account__code",
                "account__name",
                "account__account_type__category",
                "account__account_type__name",
                "account__account_type__normal_balance",
            )
            .annotate(
                total_debit=Sum("debit"),
                total_credit=Sum("credit"),
            )
        )
        return lines

    current_lines = _query_balances(as_of)
    prior_lines = _query_balances(cmp_as_of) if cmp_as_of else []

    prior_map = {}
    for r in prior_lines:
        acc_id = r["account_id"]
        cat = r["account__account_type__category"]
        deb = r["total_debit"] or Decimal("0.00")
        crd = r["total_credit"] or Decimal("0.00")
        if cat == "asset":
            norm = r["account__account_type__normal_balance"]
            prior_net = (deb - crd) if norm == "debit" else (crd - deb)
        else:
            prior_net = crd - deb
        prior_map[acc_id] = prior_net

    current_assets = []
    non_current_assets = []
    current_liabilities = []
    non_current_liabilities = []
    equity_rows = []

    tot_cur_assets = Decimal("0.00")
    tot_non_cur_assets = Decimal("0.00")
    tot_cur_liabilities = Decimal("0.00")
    tot_non_cur_liabilities = Decimal("0.00")
    tot_equity_recorded = Decimal("0.00")

    prior_tot_cur_assets = Decimal("0.00")
    prior_tot_non_cur_assets = Decimal("0.00")
    prior_tot_cur_liab = Decimal("0.00")
    prior_tot_non_cur_liab = Decimal("0.00")
    prior_tot_equity = Decimal("0.00")

    # Cumulative revenue and expenses up to as_of date to calculate unclosed net income
    cum_revenue = Decimal("0.00")
    cum_expense = Decimal("0.00")

    for r in current_lines:
        acc_id = r["account_id"]
        code = r["account__code"]
        name = r["account__name"]
        cat = r["account__account_type__category"]
        type_name = r["account__account_type__name"]
        norm = r["account__account_type__normal_balance"]
        deb = r["total_debit"] or Decimal("0.00")
        crd = r["total_credit"] or Decimal("0.00")

        prior_amt = prior_map.get(acc_id, Decimal("0.00"))

        if cat == "asset":
            # For contra-assets like Accumulated Depreciation (normal credit), credit balance reduces assets
            if norm == "credit":
                net = -(crd - deb)
            else:
                net = deb - crd

            var_amt, var_pct = _calc_variance(net, prior_amt)
            row = {
                "account_id": acc_id,
                "code": code,
                "name": name,
                "account_type": type_name,
                "amount": net,
                "prior_amount": prior_amt,
                "variance_amount": var_amt,
                "variance_pct": var_pct,
            }
            # Distinguish current vs fixed/non-current assets
            is_fixed = (
                code.startswith("15")
                or code.startswith("16")
                or "property" in type_name.lower()
                or "machinery" in type_name.lower()
                or "depreciation" in type_name.lower()
                or "fixed" in type_name.lower()
            )
            if is_fixed:
                non_current_assets.append(row)
                tot_non_cur_assets += net
                prior_tot_non_cur_assets += prior_amt
            else:
                current_assets.append(row)
                tot_cur_assets += net
                prior_tot_cur_assets += prior_amt

        elif cat == "liability":
            net = crd - deb
            var_amt, var_pct = _calc_variance(net, prior_amt)
            row = {
                "account_id": acc_id,
                "code": code,
                "name": name,
                "account_type": type_name,
                "amount": net,
                "prior_amount": prior_amt,
                "variance_amount": var_amt,
                "variance_pct": var_pct,
            }
            is_long_term = (
                code.startswith("25")
                or "long-term" in type_name.lower()
                or "long term" in type_name.lower()
            )
            if is_long_term:
                non_current_liabilities.append(row)
                tot_non_cur_liabilities += net
                prior_tot_non_cur_liab += prior_amt
            else:
                current_liabilities.append(row)
                tot_cur_liabilities += net
                prior_tot_cur_liab += prior_amt

        elif cat == "equity":
            net = crd - deb
            var_amt, var_pct = _calc_variance(net, prior_amt)
            row = {
                "account_id": acc_id,
                "code": code,
                "name": name,
                "account_type": type_name,
                "amount": net,
                "prior_amount": prior_amt,
                "variance_amount": var_amt,
                "variance_pct": var_pct,
            }
            equity_rows.append(row)
            tot_equity_recorded += net
            prior_tot_equity += prior_amt

        elif cat == "revenue":
            cum_revenue += (crd - deb)
        elif cat == "expense":
            cum_expense += (deb - crd)

    # Sort sections by code
    current_assets.sort(key=lambda x: x["code"])
    non_current_assets.sort(key=lambda x: x["code"])
    current_liabilities.sort(key=lambda x: x["code"])
    non_current_liabilities.sort(key=lambda x: x["code"])
    equity_rows.sort(key=lambda x: x["code"])

    total_assets = tot_cur_assets + tot_non_cur_assets
    total_liabilities = tot_cur_liabilities + tot_non_cur_liabilities

    # Unclosed Net Income from P&L accounts (Revenue - Expense)
    current_net_income = cum_revenue - cum_expense

    # Add Current Year Net Income into Equity presentation
    equity_rows.append({
        "account_id": None,
        "code": "NET-INC",
        "name": "Current Year Net Income / (Loss)",
        "account_type": "Retained Earnings",
        "amount": current_net_income,
        "prior_amount": Decimal("0.00"),
        "variance_amount": current_net_income,
        "variance_pct": Decimal("100.00") if current_net_income != Decimal("0.00") else Decimal("0.00"),
        "is_computed": True,
    })

    total_equity = tot_equity_recorded + current_net_income
    total_liabilities_and_equity = total_liabilities + total_equity

    # Equilibrium validation: Assets = Liabilities + Equity
    discrepancy = total_assets - total_liabilities_and_equity
    is_balanced = (abs(discrepancy) < Decimal("0.01"))

    settings = AccountingSettings.objects.filter(company=company).first()
    currency = settings.default_currency if settings else "USD"

    return {
        "report_type": "balance_sheet",
        "company_name": company.name,
        "currency": currency,
        "as_of_date": str(as_of),
        "period_id": dates["period"].id if dates["period"] else None,
        "period_name": dates["period"].name if dates["period"] else "",
        "fiscal_year_id": dates["fiscal_year"].id if dates["fiscal_year"] else None,
        "fiscal_year_name": dates["fiscal_year"].name if dates["fiscal_year"] else "",
        "compare_to": compare_to,
        "comparative_as_of_date": str(cmp_as_of) if cmp_as_of else None,
        "is_balanced": is_balanced,
        "discrepancy": discrepancy,
        "sections": {
            "current_assets": {
                "title": "Current Assets",
                "accounts": current_assets,
                "total": tot_cur_assets,
                "prior_total": prior_tot_cur_assets,
            },
            "non_current_assets": {
                "title": "Non-Current / Fixed Assets",
                "accounts": non_current_assets,
                "total": tot_non_cur_assets,
                "prior_total": prior_tot_non_cur_assets,
            },
            "total_assets": {
                "title": "Total Assets",
                "amount": total_assets,
                "prior_amount": prior_tot_cur_assets + prior_tot_non_cur_assets,
            },
            "current_liabilities": {
                "title": "Current Liabilities",
                "accounts": current_liabilities,
                "total": tot_cur_liabilities,
                "prior_total": prior_tot_cur_liab,
            },
            "non_current_liabilities": {
                "title": "Non-Current Liabilities",
                "accounts": non_current_liabilities,
                "total": tot_non_cur_liabilities,
                "prior_total": prior_tot_non_cur_liab,
            },
            "total_liabilities": {
                "title": "Total Liabilities",
                "amount": total_liabilities,
                "prior_amount": prior_tot_cur_liab + prior_tot_non_cur_liab,
            },
            "equity": {
                "title": "Shareholders' Equity",
                "accounts": equity_rows,
                "total": total_equity,
                "prior_total": prior_tot_equity,
            },
            "total_liabilities_and_equity": {
                "title": "Total Liabilities & Equity",
                "amount": total_liabilities_and_equity,
                "prior_amount": prior_tot_cur_liab + prior_tot_non_cur_liab + prior_tot_equity,
            },
        },
        "summary": {
            "total_assets": total_assets,
            "total_liabilities": total_liabilities,
            "total_equity": total_equity,
            "total_liabilities_and_equity": total_liabilities_and_equity,
            "is_balanced": is_balanced,
            "discrepancy": discrepancy,
        },
    }


def get_cash_flow_statement_report(
    company,
    start_date=None,
    end_date=None,
    period_id=None,
    fiscal_year_id=None,
):
    """
    Computes Statement of Cash Flows using the standard Indirect Method:
    
    1. Operating Activities:
       - Net Profit from Income Statement
       - Working Capital Adjustments:
         - Change in Accounts Receivable (- increase / + decrease)
         - Change in Inventories (- increase / + decrease)
         - Change in Accounts Payable (+ increase / - decrease)
         - Change in Taxes & Accruals (+ increase / - decrease)
    2. Investing Activities:
       - Change in Fixed Assets / CapEx (- increase / + decrease)
    3. Financing Activities:
       - Change in Debt & Equity (+ increase / - decrease)
    4. Net Increase / Decrease in Cash
    5. Beginning Cash Balance
    6. Ending Cash Balance (reconciles with GL Cash & Bank accounts)
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        start_date=start_date,
        end_date=end_date,
    )
    s_date = dates["start_date"]
    e_date = dates["end_date"]

    valid_statuses = ["posted", "reversed"]

    # 1. Net Profit for the period
    pnl = get_profit_and_loss_report(
        company,
        start_date=s_date,
        end_date=e_date,
    )
    net_profit = pnl["summary"]["net_profit"]

    # 2. Compute balance differences for balance sheet accounts between start and end
    def _account_balance_as_of(cutoff):
        lines = (
            JournalEntryLine.objects.filter(
                company=company,
                journal_entry__status__in=valid_statuses,
                journal_entry__transaction_date__lte=cutoff,
            )
            .values("account__code", "account__account_type__category", "account__account_type__normal_balance")
            .annotate(tot_dr=Sum("debit"), tot_cr=Sum("credit"))
        )
        balances = {}
        for r in lines:
            code = r["account__code"]
            cat = r["account__account_type__category"]
            norm = r["account__account_type__normal_balance"]
            dr = r["tot_dr"] or Decimal("0.00")
            cr = r["tot_cr"] or Decimal("0.00")
            if cat == "asset":
                bal = (dr - cr) if norm == "debit" else (cr - dr)
            else:
                bal = cr - dr
            balances[code] = bal
        return balances

    prior_date = s_date - timedelta(days=1)
    bal_start = _account_balance_as_of(prior_date)
    bal_end = _account_balance_as_of(e_date)

    def _get_delta_by_prefix(prefix):
        all_codes = set(bal_start.keys()) | set(bal_end.keys())
        target_codes = [c for c in all_codes if c.startswith(prefix)]
        s_val = sum(bal_start.get(c, Decimal("0.00")) for c in target_codes)
        e_val = sum(bal_end.get(c, Decimal("0.00")) for c in target_codes)
        return e_val - s_val

    # Working capital deltas
    delta_ar = _get_delta_by_prefix("11")  # Accounts Receivable
    delta_inv = _get_delta_by_prefix("12")  # Inventory
    delta_prepaids = _get_delta_by_prefix("13")  # Prepaids
    delta_ap = _get_delta_by_prefix("20")  # Accounts Payable
    delta_accrued = _get_delta_by_prefix("21")  # Accrued Expenses
    delta_tax = _get_delta_by_prefix("22")  # Taxes Payable

    # Cash flow adjustments:
    # Asset increases consume cash (negative flow)
    # Liability increases generate cash (positive flow)
    adj_ar = -delta_ar
    adj_inv = -delta_inv
    adj_prepaids = -delta_prepaids
    adj_ap = delta_ap
    adj_accrued = delta_accrued
    adj_tax = delta_tax

    net_working_capital = adj_ar + adj_inv + adj_prepaids + adj_ap + adj_accrued + adj_tax
    net_operating_cash = net_profit + net_working_capital

    # Investing activities: Fixed assets (15xx)
    delta_ppe = _get_delta_by_prefix("15")
    net_investing_cash = -delta_ppe

    # Financing activities: Long-term debt (25xx) and Equity (30xx)
    delta_debt = _get_delta_by_prefix("25")
    delta_equity = _get_delta_by_prefix("30")
    net_financing_cash = delta_debt + delta_equity

    # Net change in cash
    net_change_in_cash = net_operating_cash + net_investing_cash + net_financing_cash

    # Cash & Cash Equivalents accounts (10xx)
    def _get_cash_balance(cutoff):
        lines = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__lte=cutoff,
            account__code__startswith="10",
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        dr = lines["dr"] or Decimal("0.00")
        cr = lines["cr"] or Decimal("0.00")
        return dr - cr

    beginning_cash = _get_cash_balance(prior_date)
    ending_cash_actual = _get_cash_balance(e_date)
    ending_cash_calculated = beginning_cash + net_change_in_cash

    cash_reconciled = (abs(ending_cash_actual - ending_cash_calculated) < Decimal("0.01"))

    settings = AccountingSettings.objects.filter(company=company).first()
    currency = settings.default_currency if settings else "USD"

    return {
        "report_type": "cash_flow",
        "company_name": company.name,
        "currency": currency,
        "start_date": str(s_date),
        "end_date": str(e_date),
        "period_id": dates["period"].id if dates["period"] else None,
        "period_name": dates["period"].name if dates["period"] else "",
        "fiscal_year_id": dates["fiscal_year"].id if dates["fiscal_year"] else None,
        "fiscal_year_name": dates["fiscal_year"].name if dates["fiscal_year"] else "",
        "operating_activities": {
            "title": "Cash Flow from Operating Activities",
            "net_profit": net_profit,
            "adjustments": [
                {"name": "Change in Accounts Receivable", "amount": adj_ar},
                {"name": "Change in Inventories", "amount": adj_inv},
                {"name": "Change in Prepaid Expenses", "amount": adj_prepaids},
                {"name": "Change in Accounts Payable", "amount": adj_ap},
                {"name": "Change in Accrued Payroll & Expenses", "amount": adj_accrued},
                {"name": "Change in Taxes Payable", "amount": adj_tax},
            ],
            "total_adjustments": net_working_capital,
            "net_operating_cash": net_operating_cash,
        },
        "investing_activities": {
            "title": "Cash Flow from Investing Activities",
            "items": [
                {"name": "Capital Expenditures (Equipment & Plant Additions)", "amount": net_investing_cash}
            ],
            "net_investing_cash": net_investing_cash,
        },
        "financing_activities": {
            "title": "Cash Flow from Financing Activities",
            "items": [
                {"name": "Proceeds from / (Repayment of) Long-Term Debt", "amount": delta_debt},
                {"name": "Owner Capital Contributions / (Drawings)", "amount": delta_equity},
            ],
            "net_financing_cash": net_financing_cash,
        },
        "summary": {
            "net_operating_cash": net_operating_cash,
            "net_investing_cash": net_investing_cash,
            "net_financing_cash": net_financing_cash,
            "net_change_in_cash": net_change_in_cash,
            "beginning_cash": beginning_cash,
            "ending_cash_calculated": ending_cash_calculated,
            "ending_cash_actual": ending_cash_actual,
            "cash_reconciled": cash_reconciled,
        },
    }


def get_trial_balance_report(
    company,
    period_id=None,
    fiscal_year_id=None,
    as_of_date=None,
    start_date=None,
    end_date=None,
    search=None,
    category=None,
    compare_to=None,
):
    """
    Standard Trial Balance report returning opening balance, period debits, period credits,
    closing debit/credit columns, and mathematical equilibrium verification.
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        start_date=start_date,
        end_date=end_date,
        as_of_date=as_of_date,
        compare_to=compare_to,
    )
    period = dates["period"]
    fy = dates["fiscal_year"]
    as_of = dates["as_of_date"]

    tb_data = get_period_trial_balance(
        company=company,
        period=period,
        fiscal_year=fy,
        as_of_date=as_of,
        search=search,
        category=category,
    )

    settings = AccountingSettings.objects.filter(company=company).first()
    currency = settings.default_currency if settings else "USD"
    tb_data["currency"] = currency
    tb_data["report_type"] = "trial_balance"
    tb_data["compare_to"] = compare_to
    tb_data["is_balanced"] = tb_data.get("totals", {}).get("is_balanced", False)

    return tb_data


def get_inventory_valuation_report(
    company,
    as_of_date=None,
    period_id=None,
    fiscal_year_id=None,
):
    """
    Computes Inventory Valuation Report aggregating general ledger inventory movements:
    - Raw Materials (1210)
    - Work-in-Progress (1220)
    - Finished Goods (1230)
    
    Shows:
    - Opening balance
    - Purchases / Inbound receipts (Debits)
    - Production issues / Consumption / Shipments (Credits)
    - Closing balance valuation
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        as_of_date=as_of_date,
    )
    s_date = dates["start_date"]
    e_date = dates["end_date"]

    valid_statuses = ["posted", "reversed"]

    inventory_accounts = Account.objects.filter(
        company=company,
        is_active=True,
    ).filter(
        Q(code__startswith="12") | Q(account_type__name__icontains="inventory")
    ).select_related("account_type").order_by("code")

    items = []
    tot_opening = Decimal("0.00")
    tot_receipts = Decimal("0.00")
    tot_issues = Decimal("0.00")
    tot_closing = Decimal("0.00")

    for acc in inventory_accounts:
        # Opening balance before start_date
        prior_agg = JournalEntryLine.objects.filter(
            company=company,
            account=acc,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__lt=s_date,
        ).aggregate(tot_dr=Sum("debit"), tot_cr=Sum("credit"))
        open_dr = prior_agg["tot_dr"] or Decimal("0.00")
        open_cr = prior_agg["tot_cr"] or Decimal("0.00")
        opening_val = open_dr - open_cr

        # Period movements
        period_agg = JournalEntryLine.objects.filter(
            company=company,
            account=acc,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__gte=s_date,
            journal_entry__transaction_date__lte=e_date,
        ).aggregate(tot_dr=Sum("debit"), tot_cr=Sum("credit"))
        receipts = period_agg["tot_dr"] or Decimal("0.00")
        issues = period_agg["tot_cr"] or Decimal("0.00")
        closing_val = opening_val + receipts - issues

        items.append({
            "account_id": acc.id,
            "code": acc.code,
            "name": acc.name,
            "account_type": acc.account_type.name,
            "opening_valuation": opening_val,
            "receipts_additions": receipts,
            "issues_consumption": issues,
            "closing_valuation": closing_val,
        })
        tot_opening += opening_val
        tot_receipts += receipts
        tot_issues += issues
        tot_closing += closing_val

    settings = AccountingSettings.objects.filter(company=company).first()
    currency = settings.default_currency if settings else "USD"

    return {
        "report_type": "inventory_valuation",
        "company_name": company.name,
        "currency": currency,
        "start_date": str(s_date),
        "end_date": str(e_date),
        "items": items,
        "summary": {
            "total_opening_valuation": tot_opening,
            "total_receipts_additions": tot_receipts,
            "total_issues_consumption": tot_issues,
            "total_closing_valuation": tot_closing,
        },
    }


def get_manufacturing_cost_report(
    company,
    start_date=None,
    end_date=None,
    period_id=None,
    fiscal_year_id=None,
):
    """
    Manufacturing Cost & Variance Statement:
    - Direct Materials Consumed (5010)
    - Direct Packaging Consumed (5020)
    - Direct Manufacturing Labor (5100)
    - Prime Cost (Direct Materials + Direct Labor)
    - Factory Overhead Applied (5200, 5210)
    - Conversion Cost (Direct Labor + Overhead)
    - Total Manufacturing Costs
    - WIP Beginning vs Ending Adjustment (1220)
    - Cost of Goods Manufactured (COGM)
    - Scrap & Production Losses (5080)
    - Manufacturing Variances (5090)
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        start_date=start_date,
        end_date=end_date,
    )
    s_date = dates["start_date"]
    e_date = dates["end_date"]

    valid_statuses = ["posted", "reversed"]

    def _period_net_debit(code_prefix, name_filter=None):
        qs = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__gte=s_date,
            journal_entry__transaction_date__lte=e_date,
            account__code__startswith=code_prefix,
        )
        if name_filter:
            qs = qs.filter(account__name__icontains=name_filter)
        agg = qs.aggregate(dr=Sum("debit"), cr=Sum("credit"))
        dr = agg["dr"] or Decimal("0.00")
        cr = agg["cr"] or Decimal("0.00")
        return dr - cr

    direct_materials = _period_net_debit("501")
    direct_packaging = _period_net_debit("502")
    direct_labor = _period_net_debit("510")
    factory_overhead = _period_net_debit("52")
    scrap_loss = _period_net_debit("508")
    variance = _period_net_debit("509")

    prime_cost = direct_materials + direct_packaging + direct_labor
    conversion_cost = direct_labor + factory_overhead
    total_manufacturing_costs = prime_cost + factory_overhead

    # WIP inventory delta
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

    wip_start = _wip_balance(s_date - timedelta(days=1))
    wip_end = _wip_balance(e_date)
    wip_change = wip_start - wip_end  # Positive if beginning WIP was higher (consumed)

    cogm = total_manufacturing_costs + wip_change

    settings = AccountingSettings.objects.filter(company=company).first()
    currency = settings.default_currency if settings else "USD"

    return {
        "report_type": "manufacturing_cost_and_variance",
        "company_name": company.name,
        "currency": currency,
        "start_date": str(s_date),
        "end_date": str(e_date),
        "cost_components": [
            {"name": "Direct Raw Materials Consumed", "account_code": "5010", "amount": direct_materials},
            {"name": "Direct Packaging Consumed", "account_code": "5020", "amount": direct_packaging},
            {"name": "Direct Manufacturing Labor", "account_code": "5100", "amount": direct_labor},
            {"name": "Factory Overhead & Utilities Applied", "account_code": "5200-5210", "amount": factory_overhead},
            {"name": "Production Scrap & Losses", "account_code": "5080", "amount": scrap_loss},
            {"name": "Manufacturing Cost Variances", "account_code": "5090", "amount": variance},
        ],
        "kpis": {
            "prime_cost": prime_cost,
            "conversion_cost": conversion_cost,
            "total_manufacturing_costs": total_manufacturing_costs,
            "wip_beginning": wip_start,
            "wip_ending": wip_end,
            "wip_net_change": wip_change,
            "cost_of_goods_manufactured": cogm,
        },
    }


def get_account_reconciliation_report(
    company,
    as_of_date=None,
    period_id=None,
    fiscal_year_id=None,
):
    """
    Subledger vs General Ledger Control Accounts Reconciliation Report.
    Audits:
    - Accounts Receivable: GL 1100 vs AR Subledger Open Invoices
    - Accounts Payable: GL 2010 vs AP Subledger Open Bills
    - Cash & Bank: GL 1010/1020 vs Bank Account Statement Reconciled Balances
    - Taxes: GL 22xx vs Tax Subledger Report Net Liability
    - Inventory: GL 12xx vs Inventory Valuation
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        as_of_date=as_of_date,
    )
    as_of = dates["as_of_date"]

    valid_statuses = ["posted", "reversed"]

    def _gl_account_balance(code_prefix):
        agg = JournalEntryLine.objects.filter(
            company=company,
            journal_entry__status__in=valid_statuses,
            journal_entry__transaction_date__lte=as_of,
            account__code__startswith=code_prefix,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        dr = agg["dr"] or Decimal("0.00")
        cr = agg["cr"] or Decimal("0.00")
        return dr, cr

    reconciliations = []

    # 1. Accounts Receivable
    gl_ar_dr, gl_ar_cr = _gl_account_balance("1100")
    gl_ar_bal = gl_ar_dr - gl_ar_cr
    try:
        ar_aging = get_ar_aging_report(company, as_of_date=as_of)
        sub_ar_bal = ar_aging["totals"]["total_ar"]
    except Exception:
        sub_ar_bal = Decimal("0.00")
    ar_diff = gl_ar_bal - sub_ar_bal
    reconciliations.append({
        "module": "Accounts Receivable",
        "gl_account": "1100 Accounts Receivable",
        "gl_balance": gl_ar_bal,
        "subledger_balance": sub_ar_bal,
        "variance": ar_diff,
        "status": "reconciled" if abs(ar_diff) < Decimal("0.01") else "discrepancy",
    })

    # 2. Accounts Payable
    gl_ap_dr, gl_ap_cr = _gl_account_balance("2010")
    gl_ap_bal = gl_ap_cr - gl_ap_dr
    try:
        ap_aging = get_ap_aging_report(company, as_of_date=as_of)
        sub_ap_bal = ap_aging["totals"]["total_ap"]
    except Exception:
        sub_ap_bal = Decimal("0.00")
    ap_diff = gl_ap_bal - sub_ap_bal
    reconciliations.append({
        "module": "Accounts Payable",
        "gl_account": "2010 Accounts Payable (Trade)",
        "gl_balance": gl_ap_bal,
        "subledger_balance": sub_ap_bal,
        "variance": ap_diff,
        "status": "reconciled" if abs(ap_diff) < Decimal("0.01") else "discrepancy",
    })

    # 3. Cash & Bank
    gl_bank_dr, gl_bank_cr = _gl_account_balance("10")
    gl_bank_bal = gl_bank_dr - gl_bank_cr
    # Reconciled bank balance from BankAccounts
    from .models import BankAccount
    bank_accs = BankAccount.objects.filter(company=company, is_active=True)
    sub_bank_bal = sum((b.current_balance for b in bank_accs), Decimal("0.00"))
    bank_diff = gl_bank_bal - sub_bank_bal
    reconciliations.append({
        "module": "Cash & Bank Accounts",
        "gl_account": "1010-1030 Cash & Bank Accounts",
        "gl_balance": gl_bank_bal,
        "subledger_balance": sub_bank_bal,
        "variance": bank_diff,
        "status": "reconciled" if abs(bank_diff) < Decimal("0.01") else "discrepancy",
    })

    # 4. Tax Liability
    gl_tax_dr, gl_tax_cr = _gl_account_balance("22")
    gl_tax_bal = gl_tax_cr - gl_tax_dr
    try:
        tax_rep = get_tax_report(company, end_date=as_of)
        sub_tax_bal = tax_rep["summary"]["net_tax_payable"]
    except Exception:
        sub_tax_bal = Decimal("0.00")
    tax_diff = gl_tax_bal - sub_tax_bal
    reconciliations.append({
        "module": "Taxation",
        "gl_account": "2200 Taxes Payable",
        "gl_balance": gl_tax_bal,
        "subledger_balance": sub_tax_bal,
        "variance": tax_diff,
        "status": "reconciled" if abs(tax_diff) < Decimal("0.01") else "discrepancy",
    })

    # 5. Inventory
    gl_inv_dr, gl_inv_cr = _gl_account_balance("12")
    gl_inv_bal = gl_inv_dr - gl_inv_cr
    inv_rep = get_inventory_valuation_report(company, as_of_date=as_of)
    sub_inv_bal = inv_rep["summary"]["total_closing_valuation"]
    inv_diff = gl_inv_bal - sub_inv_bal
    reconciliations.append({
        "module": "Inventory",
        "gl_account": "1200 Inventories (Raw, WIP, Finished)",
        "gl_balance": gl_inv_bal,
        "subledger_balance": sub_inv_bal,
        "variance": inv_diff,
        "status": "reconciled" if abs(inv_diff) < Decimal("0.01") else "discrepancy",
    })

    all_reconciled = all(item["status"] == "reconciled" for item in reconciliations)

    settings = AccountingSettings.objects.filter(company=company).first()
    currency = settings.default_currency if settings else "USD"

    return {
        "report_type": "account_reconciliations",
        "company_name": company.name,
        "currency": currency,
        "as_of_date": str(as_of),
        "all_reconciled": all_reconciled,
        "reconciliations": reconciliations,
    }


def get_report_drill_down(
    company,
    account_id,
    start_date=None,
    end_date=None,
    period_id=None,
    fiscal_year_id=None,
):
    """
    Interactive Drill-Down service:
    Fetches chronological journal transactions composing any account's balance
    for the selected reporting period.
    """
    dates = resolve_report_period(
        company,
        period_id=period_id,
        fiscal_year_id=fiscal_year_id,
        start_date=start_date,
        end_date=end_date,
    )
    return get_account_ledger(
        company=company,
        account_id=account_id,
        start_date=dates["start_date"],
        end_date=dates["end_date"],
    )


def export_financial_report(report_data, export_format="csv"):
    """
    Formats report data into downloadable CSV stream.
    """
    if export_format == "json":
        return report_data

    output = io.StringIO()
    writer = csv.writer(output)

    rep_type = report_data.get("report_type", "financial_report")
    company_name = report_data.get("company_name", "Manufacturing ERP")
    currency = report_data.get("currency", "USD")

    # Header block
    writer.writerow([f"{company_name} — {rep_type.replace('_', ' ').upper()}"])
    if "start_date" in report_data and "end_date" in report_data:
        writer.writerow(["Period", f"{report_data['start_date']} to {report_data['end_date']}"])
    elif "as_of_date" in report_data:
        writer.writerow(["As of Date", report_data["as_of_date"]])
    writer.writerow(["Currency", currency])
    writer.writerow([])

    if rep_type == "profit_and_loss":
        writer.writerow(["Category", "Account Code", "Account Name", "Amount", "Prior Amount", "Variance $", "Variance %"])
        for sec_key, sec in report_data.get("sections", {}).items():
            if "accounts" in sec:
                writer.writerow([sec.get("title", sec_key)])
                for acc in sec["accounts"]:
                    writer.writerow([
                        "",
                        acc.get("code", ""),
                        acc.get("name", ""),
                        acc.get("amount", "0.00"),
                        acc.get("prior_amount", "0.00"),
                        acc.get("variance_amount", "0.00"),
                        f"{acc.get('variance_pct', '0.00')}%",
                    ])
                writer.writerow(["", "", f"Total {sec.get('title')}", sec.get("total", "0.00"), sec.get("prior_total", "0.00")])
                writer.writerow([])
            elif "amount" in sec:
                writer.writerow([sec.get("title", sec_key), "", "", sec.get("amount", "0.00"), sec.get("prior_amount", "0.00")])
                writer.writerow([])

    elif rep_type == "balance_sheet":
        writer.writerow(["Section", "Account Code", "Account Name", "Amount", "Prior Amount"])
        for sec_key, sec in report_data.get("sections", {}).items():
            if "accounts" in sec:
                writer.writerow([sec.get("title", sec_key)])
                for acc in sec["accounts"]:
                    writer.writerow([
                        "",
                        acc.get("code", ""),
                        acc.get("name", ""),
                        acc.get("amount", "0.00"),
                        acc.get("prior_amount", "0.00"),
                    ])
                writer.writerow(["", "", f"Total {sec.get('title')}", sec.get("total", "0.00")])
                writer.writerow([])
            elif "amount" in sec:
                writer.writerow([sec.get("title", sec_key), "", "", sec.get("amount", "0.00")])
                writer.writerow([])

    elif rep_type == "trial_balance":
        writer.writerow(["Account Code", "Account Name", "Category", "Opening Balance", "Period Debit", "Period Credit", "Closing Debit", "Closing Credit"])
        for acc in report_data.get("accounts", []):
            writer.writerow([
                acc.get("code", ""),
                acc.get("name", ""),
                acc.get("category", ""),
                acc.get("opening_balance", "0.00"),
                acc.get("period_debit", "0.00"),
                acc.get("period_credit", "0.00"),
                acc.get("closing_debit", "0.00"),
                acc.get("closing_credit", "0.00"),
            ])
        writer.writerow([
            "TOTALS", "", "", "",
            report_data.get("totals", {}).get("total_period_debit", "0.00"),
            report_data.get("totals", {}).get("total_period_credit", "0.00"),
            report_data.get("totals", {}).get("total_closing_debit", "0.00"),
            report_data.get("totals", {}).get("total_closing_credit", "0.00"),
        ])

    else:
        # Generic CSV dumper for remaining reports
        writer.writerow(["Key", "Value"])
        for k, v in report_data.items():
            if isinstance(v, (str, int, float, Decimal)):
                writer.writerow([k, str(v)])

    output.seek(0)
    return output.getvalue()
