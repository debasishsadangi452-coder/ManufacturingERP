from .models import AccountType, Account, FiscalYear, AccountingPeriod, AccountingSettings
from datetime import date
import calendar


DEFAULT_ACCOUNT_TYPES = [
    # Assets (Normal: Debit)
    {"name": "Cash & Cash Equivalents", "category": "asset", "normal_balance": "debit", "code_prefix": "10", "description": "Cash on hand, bank checking and savings accounts"},
    {"name": "Accounts Receivable", "category": "asset", "normal_balance": "debit", "code_prefix": "11", "description": "Amounts owed by customers for goods and services"},
    {"name": "Inventory", "category": "asset", "normal_balance": "debit", "code_prefix": "12", "description": "Raw materials, work-in-progress, and finished goods"},
    {"name": "Prepaid Expenses & Other Current Assets", "category": "asset", "normal_balance": "debit", "code_prefix": "13", "description": "Prepaid insurance, advance supplier payments"},
    {"name": "Property, Plant & Equipment", "category": "asset", "normal_balance": "debit", "code_prefix": "15", "description": "Machinery, production lines, vehicles, buildings"},
    {"name": "Accumulated Depreciation", "category": "asset", "normal_balance": "credit", "code_prefix": "16", "description": "Contra-asset reflecting depreciation of fixed assets"},
    
    # Liabilities (Normal: Credit)
    {"name": "Accounts Payable", "category": "liability", "normal_balance": "credit", "code_prefix": "20", "description": "Amounts owed to suppliers for materials and services"},
    {"name": "Accrued Expenses & Other Current Liabilities", "category": "liability", "normal_balance": "credit", "code_prefix": "21", "description": "Accrued payroll, utilities, and current debt"},
    {"name": "Long-Term Debt", "category": "liability", "normal_balance": "credit", "code_prefix": "25", "description": "Long-term loans, equipment financing, mortgages"},
    
    # Equity (Normal: Credit)
    {"name": "Common Stock & Owner's Capital", "category": "equity", "normal_balance": "credit", "code_prefix": "30", "description": "Contributed capital from company owners/shareholders"},
    {"name": "Retained Earnings", "category": "equity", "normal_balance": "credit", "code_prefix": "32", "description": "Cumulative net income retained in the business"},
    
    # Revenue (Normal: Credit)
    {"name": "Operating Sales Revenue", "category": "revenue", "normal_balance": "credit", "code_prefix": "40", "description": "Income earned from core manufactured product sales"},
    {"name": "Discounts & Returns", "category": "revenue", "normal_balance": "debit", "code_prefix": "41", "description": "Sales discounts, customer returns and allowances"},
    {"name": "Other Income", "category": "revenue", "normal_balance": "credit", "code_prefix": "49", "description": "Interest income, asset sale gains, miscellaneous revenue"},
    
    # Expenses (Normal: Debit)
    {"name": "Cost of Goods Sold (Raw Materials)", "category": "expense", "normal_balance": "debit", "code_prefix": "50", "description": "Direct material costs consumed in manufacturing"},
    {"name": "Cost of Goods Sold (Direct Labor)", "category": "expense", "normal_balance": "debit", "code_prefix": "51", "description": "Factory production line operator and supervisor wages"},
    {"name": "Cost of Goods Sold (Manufacturing Overhead)", "category": "expense", "normal_balance": "debit", "code_prefix": "52", "description": "Factory utilities, maintenance, packaging, facility costs"},
    {"name": "Selling, General & Administrative", "category": "expense", "normal_balance": "debit", "code_prefix": "60", "description": "Office rent, management salaries, advertising, software"},
    {"name": "Depreciation & Amortization Expense", "category": "expense", "normal_balance": "debit", "code_prefix": "65", "description": "Periodic depreciation of plant machinery and assets"},
]


def ensure_account_types():
    """Ensure standard system AccountTypes exist in database."""
    created_types = {}
    for item in DEFAULT_ACCOUNT_TYPES:
        obj, _ = AccountType.objects.get_or_create(
            name=item["name"],
            defaults={
                "category": item["category"],
                "normal_balance": item["normal_balance"],
                "code_prefix": item["code_prefix"],
                "description": item["description"],
                "is_system": True,
            }
        )
        created_types[item["name"]] = obj
    return created_types


def seed_standard_chart_of_accounts(company):
    """Seeds standard manufacturing chart of accounts for a given company."""
    types = ensure_account_types()
    
    # Standard Accounts Specification: (code, name, type_name, parent_code, description)
    standard_accounts = [
        # --- 1000: ASSETS ---
        ("1000", "Current Assets", "Cash & Cash Equivalents", None, "Header account for all current assets"),
        ("1010", "Operating Bank Account", "Cash & Cash Equivalents", "1000", "Main commercial operating account"),
        ("1020", "Payroll Bank Account", "Cash & Cash Equivalents", "1000", "Dedicated account for payroll disbursements"),
        ("1030", "Petty Cash", "Cash & Cash Equivalents", "1000", "On-site petty cash fund"),
        ("1100", "Accounts Receivable", "Accounts Receivable", "1000", "Trade receivables from customer invoicing"),
        ("1150", "Allowance for Doubtful Accounts", "Accounts Receivable", "1000", "Contra-receivable provision for bad debts"),
        ("1200", "Inventories", "Inventory", "1000", "Header for all production and warehouse inventory"),
        ("1210", "Raw Materials Inventory", "Inventory", "1200", "Stock of ingredients, metals, syrups, and packaging components"),
        ("1220", "Work-in-Progress (WIP)", "Inventory", "1200", "Items currently in active production batches"),
        ("1230", "Finished Goods Inventory", "Inventory", "1200", "Completed manufactured products ready for sale/shipment"),
        ("1300", "Prepaid Expenses", "Prepaid Expenses & Other Current Assets", "1000", "Prepaid commercial insurance, annual licenses"),
        
        # --- 1500: FIXED ASSETS ---
        ("1500", "Property, Plant & Equipment", "Property, Plant & Equipment", None, "Header for all capital assets"),
        ("1510", "Factory Machinery & Equipment", "Property, Plant & Equipment", "1500", "Bottling, canning, mixing, coiling and extrusion machinery"),
        ("1520", "Delivery Vehicles", "Property, Plant & Equipment", "1500", "Fleet delivery trucks, vans, and forklifts"),
        ("1530", "Office Computer Hardware", "Property, Plant & Equipment", "1500", "Servers, workstations, laptops"),
        ("1600", "Accumulated Depreciation - Plant & Machinery", "Accumulated Depreciation", "1500", "Accumulated depreciation on manufacturing machinery"),
        
        # --- 2000: LIABILITIES ---
        ("2000", "Current Liabilities", "Accounts Payable", None, "Header for all short-term obligations"),
        ("2010", "Accounts Payable (Trade Vendors)", "Accounts Payable", "2000", "Amounts due to material and packaging suppliers"),
        ("2100", "Accrued Payroll & Wages", "Accrued Expenses & Other Current Liabilities", "2000", "Unpaid accrued employee compensation"),
        ("2110", "Accrued Operating Expenses", "Accrued Expenses & Other Current Liabilities", "2000", "Accrued utilities, freight, and service invoices"),
        ("2500", "Long-Term Bank Loan", "Long-Term Debt", None, "Commercial bank facility for plant expansion"),
        
        # --- 3000: EQUITY ---
        ("3000", "Shareholders' Equity", "Common Stock & Owner's Capital", None, "Total equity header"),
        ("3010", "Common Stock / Owner Capital", "Common Stock & Owner's Capital", "3000", "Contributed equity capital"),
        ("3200", "Retained Earnings", "Retained Earnings", "3000", "Cumulative prior years net retained profits"),
        
        # --- 4000: REVENUE ---
        ("4000", "Manufacturing Revenue", "Operating Sales Revenue", None, "Total product sales revenue header"),
        ("4010", "Finished Goods Sales", "Operating Sales Revenue", "4000", "Wholesale & retail product sales"),
        ("4100", "Sales Discounts & Allowances", "Discounts & Returns", "4000", "Early payment discounts and return deductions"),
        ("4900", "Interest & Miscellaneous Income", "Other Income", None, "Non-operating secondary revenue"),
        
        # --- 5000: COST OF GOODS SOLD ---
        ("5000", "Cost of Goods Sold", "Cost of Goods Sold (Raw Materials)", None, "Total production cost header"),
        ("5010", "Direct Raw Materials Consumed", "Cost of Goods Sold (Raw Materials)", "5000", "Cost of syrup, billets, chemicals, concentrate used in recipes"),
        ("5020", "Direct Packaging Consumed", "Cost of Goods Sold (Raw Materials)", "5000", "Cost of bottles, cans, caps, boxes, pallets used in batches"),
        ("5100", "Direct Manufacturing Labor", "Cost of Goods Sold (Direct Labor)", "5000", "Direct hourly wages for line operators & technicians"),
        ("5200", "Factory Power & Utilities", "Cost of Goods Sold (Manufacturing Overhead)", "5000", "Electricity, gas, and water consumed in plant operations"),
        ("5210", "Factory Maintenance & Consumables", "Cost of Goods Sold (Manufacturing Overhead)", "5000", "Machine repair parts, lubricants, sanitation supplies"),
        
        # --- 6000: OPERATING EXPENSES ---
        ("6000", "Operating Expenses (SG&A)", "Selling, General & Administrative", None, "Operating expense header"),
        ("6010", "Salaries & Benefits (Admin & Sales)", "Selling, General & Administrative", "6000", "Administrative, sales, and HR compensation"),
        ("6020", "Sales, Marketing & Advertising", "Selling, General & Administrative", "6000", "Customer acquisition, marketing campaigns"),
        ("6030", "Logistics & Outbound Freight", "Selling, General & Administrative", "6000", "Shipping and customer delivery freight expense"),
        ("6040", "IT, Software & Subscriptions", "Selling, General & Administrative", "6000", "ERP, communication, and business software licenses"),
        ("6050", "Facility Rent & Insurance", "Selling, General & Administrative", "6000", "Headquarters lease, commercial liability insurance"),
        ("6500", "Depreciation Expense", "Depreciation & Amortization Expense", "6000", "Periodic depreciation of plant and fleet equipment"),
    ]
    
    account_lookup = {}
    created_count = 0

    # First pass: create accounts without parents
    for code, name, type_name, parent_code, desc in standard_accounts:
        acc_type = types.get(type_name)
        acc, created = Account.objects.get_or_create(
            company=company,
            code=code,
            defaults={
                "name": name,
                "account_type": acc_type,
                "description": desc,
                "currency": "USD",
                "is_active": True,
            }
        )
        account_lookup[code] = acc
        if created:
            created_count += 1

    # Second pass: wire up parent-child relationships
    for code, name, type_name, parent_code, desc in standard_accounts:
        if parent_code and parent_code in account_lookup:
            acc = account_lookup[code]
            acc.parent = account_lookup[parent_code]
            acc.save()

    # Link Retained Earnings in AccountingSettings
    retained_acc = account_lookup.get("3200")
    settings, _ = AccountingSettings.objects.get_or_create(
        company=company,
        defaults={
            "default_currency": "USD",
            "retained_earnings_account": retained_acc,
        }
    )
    if retained_acc and not settings.retained_earnings_account:
        settings.retained_earnings_account = retained_acc
        settings.save()

    return created_count


def seed_standard_fiscal_year(company, year: int = None):
    """Creates a standard 12-month fiscal year with monthly periods."""
    if year is None:
        year = date.today().year

    fy_name = f"FY {year}"
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)

    fy, created = FiscalYear.objects.get_or_create(
        company=company,
        name=fy_name,
        defaults={
            "start_date": start_date,
            "end_date": end_date,
            "is_closed": False,
        }
    )

    # Generate 12 monthly periods
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for i in range(1, 13):
        m_start = date(year, i, 1)
        last_day = calendar.monthrange(year, i)[1]
        m_end = date(year, i, last_day)
        p_name = f"{month_names[i-1]} {year}"
        AccountingPeriod.objects.get_or_create(
            company=company,
            fiscal_year=fy,
            period_number=i,
            defaults={
                "name": p_name,
                "start_date": m_start,
                "end_date": m_end,
                "status": "open",
            }
        )

    # Set as current fiscal year in settings
    settings, _ = AccountingSettings.objects.get_or_create(
        company=company,
        defaults={"default_currency": "USD", "current_fiscal_year": fy}
    )
    if not settings.current_fiscal_year:
        settings.current_fiscal_year = fy
        settings.save()

    return fy
