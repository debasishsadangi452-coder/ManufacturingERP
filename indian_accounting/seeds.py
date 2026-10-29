"""Tally-style default groups, ledgers, GST codes and financial year."""
import calendar
from datetime import date

from django.db import transaction

from .models import (AccountGroup, AccountingPeriod, FinancialYear, Ledger, LedgerMapping, TaxCode,
                     IndianCompanyProfile)

# (name, parent, nature, category)
GROUPS = [
    ("Capital Account", None, "liability", "capital"),
    ("Reserves & Surplus", "Capital Account", "liability", "capital"),
    ("Current Liabilities", None, "liability", "current"),
    ("Duties & Taxes", "Current Liabilities", "liability", "current"),
    ("Sundry Creditors", "Current Liabilities", "liability", "current"),
    ("Provisions", "Current Liabilities", "liability", "current"),
    ("Loans (Liability)", None, "liability", "long_term"),
    ("Current Assets", None, "asset", "current"),
    ("Sundry Debtors", "Current Assets", "asset", "current"),
    ("Cash-in-Hand", "Current Assets", "asset", "current"),
    ("Bank Accounts", "Current Assets", "asset", "current"),
    ("Stock-in-Hand", "Current Assets", "asset", "current"),
    ("Loans & Advances (Asset)", "Current Assets", "asset", "current"),
    ("Fixed Assets", None, "asset", "fixed"),
    ("Sales Accounts", None, "income", "direct"),
    ("Direct Incomes", None, "income", "direct"),
    ("Indirect Incomes", None, "income", "indirect"),
    ("Purchase Accounts", None, "expense", "direct"),
    ("Direct Expenses", None, "expense", "direct"),
    ("Indirect Expenses", None, "expense", "indirect"),
]

# (code, name, group, control_type, mapping_key)
LEDGERS = [
    ("1001", "Cash", "Cash-in-Hand", "cash", "cash"),
    ("1002", "Bank Account", "Bank Accounts", "bank", "bank"),
    ("1100", "Trade Receivables", "Sundry Debtors", "receivable", "receivable"),
    ("1201", "Raw Material Stock", "Stock-in-Hand", "inventory", "raw_material"),
    ("1202", "Work-in-Progress", "Stock-in-Hand", "inventory", "wip"),
    ("1203", "Finished Goods Stock", "Stock-in-Hand", "inventory", "finished_goods"),
    ("1301", "Input CGST", "Duties & Taxes", "tax", "input_cgst"),
    ("1302", "Input SGST", "Duties & Taxes", "tax", "input_sgst"),
    ("1303", "Input IGST", "Duties & Taxes", "tax", "input_igst"),
    ("2001", "Capital", "Capital Account", "", ""),
    ("2002", "Retained Earnings", "Reserves & Surplus", "", "retained_earnings"),
    ("2100", "Trade Payables", "Sundry Creditors", "payable", "payable"),
    ("2101", "Goods Received Not Invoiced", "Current Liabilities", "", "grni"),
    ("2201", "Output CGST", "Duties & Taxes", "tax", "output_cgst"),
    ("2202", "Output SGST", "Duties & Taxes", "tax", "output_sgst"),
    ("2203", "Output IGST", "Duties & Taxes", "tax", "output_igst"),
    ("4001", "Sales", "Sales Accounts", "", "sales"),
    ("4002", "Sales Returns", "Sales Accounts", "", "sales_returns"),
    ("4101", "Discount Received", "Indirect Incomes", "", ""),
    ("5001", "Purchases", "Purchase Accounts", "", "purchases"),
    ("5002", "Cost of Goods Sold", "Direct Expenses", "", "cogs"),
    ("5005", "Direct Labour Absorbed", "Direct Expenses", "", "labour_absorbed"),
    ("5006", "Factory Overhead Absorbed", "Direct Expenses", "", "overhead_absorbed"),
    ("5004", "Scrap & Wastage", "Direct Expenses", "", "scrap"),
    ("5003", "Freight & Carriage", "Direct Expenses", "", "freight"),
    ("5101", "Discount Allowed", "Indirect Expenses", "", "discount"),
    ("5102", "Round Off", "Indirect Expenses", "", "round_off"),
]

GST_RATES = [0, 5, 12, 18, 28]


def financial_year_bounds(on_date, start_month=4):
    start_year = on_date.year if on_date.month >= start_month else on_date.year - 1
    start = date(start_year, start_month, 1)
    end_month = (start_month - 2) % 12 + 1
    end_year = start_year + (1 if start_month > 1 else 0)
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    name = f"{start_year}-{str(end_year)[-2:]}" if start_month > 1 else str(start_year)
    return name, start, end


@transaction.atomic
def create_financial_year(company, on_date, start_month=4):
    name, start, end = financial_year_bounds(on_date, start_month)
    fy, created = FinancialYear.objects.get_or_create(
        company=company, name=name, defaults={"start_date": start, "end_date": end})
    if created:
        y, m = start.year, start.month
        for _ in range(12):
            last = calendar.monthrange(y, m)[1]
            AccountingPeriod.objects.create(company=company, financial_year=fy,
                                            start_date=date(y, m, 1), end_date=date(y, m, last))
            m += 1
            if m > 12:
                m, y = 1, y + 1
    return fy


def ensure_ledgers(company):
    """Add default ledgers/mappings introduced after a company was seeded."""
    groups = {g.name: g for g in AccountGroup.objects.filter(company=company)}
    if not groups:
        return
    have = set(LedgerMapping.objects.filter(company=company).values_list("key", flat=True))
    for code, name, group, control, key in LEDGERS:
        if key and key not in have and group in groups:
            ledger, _ = Ledger.objects.get_or_create(
                company=company, code=code, defaults={"name": name, "group": groups[group], "control_type": control})
            LedgerMapping.objects.get_or_create(company=company, key=key, defaults={"ledger": ledger})


@transaction.atomic
def seed_company(company, *, state_code, gstin="", pan="", legal_name="", today=None):
    """Idempotently provision profile, groups, ledgers, mappings, GST codes and FY."""
    from django.utils import timezone
    today = today or timezone.localdate()
    profile, _ = IndianCompanyProfile.objects.get_or_create(
        company=company, defaults={"state_code": state_code, "gstin": gstin, "pan": pan,
                                   "legal_name": legal_name or company.name})
    groups = {}
    for order, (name, parent, nature, cat) in enumerate(GROUPS):
        groups[name], _ = AccountGroup.objects.get_or_create(
            company=company, name=name,
            defaults={"parent": groups.get(parent), "nature": nature, "category": cat,
                      "is_system": True, "display_order": order})
    for code, name, group, control, key in LEDGERS:
        ledger, _ = Ledger.objects.get_or_create(
            company=company, code=code,
            defaults={"name": name, "group": groups[group], "control_type": control})
        if key:
            LedgerMapping.objects.get_or_create(company=company, key=key, defaults={"ledger": ledger})
    for rate in GST_RATES:
        TaxCode.objects.get_or_create(
            company=company, code=f"GST{rate}", effective_from=date(2017, 7, 1),
            defaults={"name": f"GST {rate}%", "gst_rate": rate})
    create_financial_year(company, today, profile.fy_start_month)
    missing = set(LedgerMapping.KEYS) - set(LedgerMapping.objects.filter(company=company).values_list("key", flat=True))
    profile.posting_enabled = not missing
    profile.save(update_fields=["posting_enabled"])
    return profile

