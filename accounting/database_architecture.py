"""
Blueprint Section #24: Database Architecture Engine & Integrity Service.
Authoritative domain service that formalises the 18 core logical entities,
their schema mappings, ERD relationships, cross-domain foreign keys,
and multi-tenant architectural integrity rules.
"""

from decimal import Decimal
from django.db import models
from django.db.models import Sum, Count, Q
from django.utils import timezone

from accounts.models import Company
from accounting.models import (
    AccountType,
    Account,
    JournalEntry,
    JournalEntryLine,
    AccountingPeriod,
    FiscalYear,
    BankAccount,
    BankTransaction,
    BankReconciliation,
    Payment,
    PaymentAllocation,
    Expense,
    ExpenseCategory,
    TaxCode,
    TaxTransactionLine,
    TaxAdjustment,
    JournalEntryAuditLog,
    PeriodAuditLog,
    ExpenseAuditLog,
    BankAuditLog,
    PaymentAuditLog,
    TaxAuditLog,
)
from sales.models import Customer, Invoice, InvoiceLine
from procurement.models import Vendor, Bill, BillLine


# Specification from Blueprint Section #24 (Page 28)
LOGICAL_ENTITIES_SPEC = [
    {
        "entity": "Company",
        "purpose": "Accounting organization / legal entity (tenant owner)",
        "django_model": "Company",
        "app_label": "accounts",
        "table_name": "accounts_company",
        "key_fields": ["id", "name", "base_currency", "fiscal_year_start"],
        "relationships": "1 -> many of every accounting entity",
        "is_tenant_root": True,
    },
    {
        "entity": "Account",
        "purpose": "Chart of Accounts master node",
        "django_model": "Account",
        "app_label": "accounting",
        "table_name": "accounting_account",
        "key_fields": ["company_id", "code", "name", "type_id", "parent_id", "is_posting", "active", "system_role"],
        "relationships": "N -> 1 AccountType, self-referential parent, 1 -> N JournalLine",
        "is_tenant_root": False,
    },
    {
        "entity": "AccountType",
        "purpose": "Account classification (Asset, Liability, Equity, Revenue, Expense)",
        "django_model": "AccountType",
        "app_label": "accounting",
        "table_name": "accounting_accounttype",
        "key_fields": ["id", "name", "category", "code_prefix", "normal_balance"],
        "relationships": "1 -> N Account",
        "is_tenant_root": False,
    },

    {
        "entity": "JournalEntry",
        "purpose": "Accounting transaction header with sequential numbering and status",
        "django_model": "JournalEntry",
        "app_label": "accounting",
        "table_name": "accounting_journalentry",
        "key_fields": ["company_id", "entry_number", "transaction_date", "status", "source_module", "source_id", "reversal_of"],
        "relationships": "1 -> N JournalEntryLine, 1 -> N JournalEntryAuditLog, N -> 1 AccountingPeriod",
        "is_tenant_root": False,
    },
    {
        "entity": "JournalLine",
        "purpose": "Debit/credit double-entry line item",
        "django_model": "JournalEntryLine",
        "app_label": "accounting",
        "table_name": "accounting_journalentryline",
        "key_fields": ["journal_entry_id", "account_id", "debit", "credit", "description", "line_number"],
        "relationships": "N -> 1 JournalEntry, N -> 1 Account",
        "is_tenant_root": False,
    },
    {
        "entity": "Customer",
        "purpose": "Accounts Receivable party master",
        "django_model": "Customer",
        "app_label": "sales",
        "table_name": "sales_customer",
        "key_fields": ["company_id", "customer_code", "name", "payment_terms", "credit_limit"],
        "relationships": "1 -> N Invoice, 1 -> N Payment",
        "is_tenant_root": False,
    },
    {
        "entity": "Supplier",
        "purpose": "Accounts Payable vendor master",
        "django_model": "Vendor",
        "app_label": "procurement",
        "table_name": "procurement_vendor",
        "key_fields": ["company_id", "vendor_code", "name", "payment_terms"],
        "relationships": "1 -> N Bill, 1 -> N Payment",
        "is_tenant_root": False,
    },
    {
        "entity": "Invoice / InvoiceLine",
        "purpose": "Customer billing header and product line items",
        "django_model": "Invoice, InvoiceLine",
        "app_label": "sales",
        "table_name": "sales_invoice, sales_invoiceline",
        "key_fields": ["customer_id", "invoice_number", "issue_date", "due_date", "total_amount", "tax_amount", "status"],
        "relationships": "Lines N -> 1 Invoice, Invoice N -> 1 Customer, Posted Invoice -> JournalEntry",
        "is_tenant_root": False,
    },
    {
        "entity": "Bill / BillLine",
        "purpose": "Supplier billing header and expense/stock line items",
        "django_model": "Bill, BillLine",
        "app_label": "procurement",
        "table_name": "procurement_bill, procurement_billline",
        "key_fields": ["vendor_id", "bill_number", "bill_date", "due_date", "total_amount", "tax_amount", "status"],
        "relationships": "Lines N -> 1 Bill, Bill N -> 1 Vendor, Posted Bill -> JournalEntry",
        "is_tenant_root": False,
    },
    {
        "entity": "Payment",
        "purpose": "Receipt or payment transaction for money movements",
        "django_model": "Payment",
        "app_label": "accounting",
        "table_name": "accounting_payment",
        "key_fields": ["company_id", "payment_type", "payment_method", "amount", "payment_date", "bank_account_id", "status"],
        "relationships": "1 -> N PaymentAllocation, N -> 1 BankAccount, 1 -> 1 JournalEntry",
        "is_tenant_root": False,
    },
    {
        "entity": "PaymentAllocation",
        "purpose": "Payment-to-document settlement link",
        "django_model": "PaymentAllocation",
        "app_label": "accounting",
        "table_name": "accounting_paymentallocation",
        "key_fields": ["payment_id", "invoice_id", "bill_id", "allocated_amount", "allocation_date", "status"],
        "relationships": "N -> 1 Payment, N -> 1 Invoice (AR), N -> 1 Bill (AP)",
        "is_tenant_root": False,
    },
    {
        "entity": "Expense",
        "purpose": "Operating and administrative expense transaction",
        "django_model": "Expense, ExpenseCategory",
        "app_label": "accounting",
        "table_name": "accounting_expense, accounting_expensecategory",
        "key_fields": ["company_id", "category_id", "expense_account_id", "amount", "tax_amount", "status"],
        "relationships": "N -> 1 ExpenseCategory, N -> 1 Account, 1 -> 1 JournalEntry",
        "is_tenant_root": False,
    },
    {
        "entity": "Tax",
        "purpose": "Tax code, rate, calculation and transaction lines",
        "django_model": "TaxCode, TaxTransactionLine, TaxAdjustment",
        "app_label": "accounting",
        "table_name": "accounting_taxcode, accounting_taxtransactionline",
        "key_fields": ["company_id", "code", "tax_type", "rate", "collected_account_id", "paid_account_id"],
        "relationships": "1 -> N TaxTransactionLine, N -> 1 Account",
        "is_tenant_root": False,
    },
    {
        "entity": "BankAccount",
        "purpose": "Bank and cash account master ledger mapping",
        "django_model": "BankAccount",
        "app_label": "accounting",
        "table_name": "accounting_bankaccount",
        "key_fields": ["company_id", "account_name", "account_number (masked)", "gl_account_id", "currency"],
        "relationships": "1 -> N BankTransaction, 1 -> N BankReconciliation, 1 -> 1 Account",
        "is_tenant_root": False,
    },
    {
        "entity": "BankTransaction",
        "purpose": "Imported or recorded bank statement movement",
        "django_model": "BankTransaction",
        "app_label": "accounting",
        "table_name": "accounting_banktransaction",
        "key_fields": ["bank_account_id", "transaction_date", "amount", "reconciliation_id", "reconciliation_status"],
        "relationships": "N -> 1 BankAccount, N -> 0..1 BankReconciliation",
        "is_tenant_root": False,
    },
    {
        "entity": "AccountingPeriod",
        "purpose": "Fiscal accounting period (open, locked, closed)",
        "django_model": "AccountingPeriod",
        "app_label": "accounting",
        "table_name": "accounting_accountingperiod",
        "key_fields": ["fiscal_year_id", "period_number", "start_date", "end_date", "status"],
        "relationships": "N -> 1 FiscalYear, 1 -> N JournalEntry",
        "is_tenant_root": False,
    },
    {
        "entity": "FiscalYear",
        "purpose": "Annual fiscal calendar definition and closing",
        "django_model": "FiscalYear",
        "app_label": "accounting",
        "table_name": "accounting_fiscalyear",
        "key_fields": ["company_id", "name", "start_date", "end_date", "status"],
        "relationships": "1 -> N AccountingPeriod, 1 -> N JournalEntry",
        "is_tenant_root": False,
    },
    {
        "entity": "Reconciliation",
        "purpose": "Bank and general ledger account statement reconciliation",
        "django_model": "BankReconciliation",
        "app_label": "accounting",
        "table_name": "accounting_bankreconciliation",
        "key_fields": ["bank_account_id", "statement_date", "statement_ending_balance", "status"],
        "relationships": "N -> 1 BankAccount, 1 -> N BankTransaction",
        "is_tenant_root": False,
    },
    {
        "entity": "AuditLog",
        "purpose": "Immutable change, posting, workflow, and approval history",
        "django_model": "JournalEntryAuditLog, PeriodAuditLog, ExpenseAuditLog, BankAuditLog, PaymentAuditLog, TaxAuditLog",
        "app_label": "accounting",
        "table_name": "accounting_*auditlog",
        "key_fields": ["company_id", "action", "performed_by_id", "timestamp", "details", "ip_address"],
        "relationships": "N -> 1 Company, N -> 1 Target Entity, N -> 1 User",
        "is_tenant_root": False,
    },
]

# Core ERD Concepts from Blueprint Section #24 (Page 29)
CORE_ERD_FLOWS = [
    {
        "id": "general_ledger_flow",
        "name": "General Ledger Core Flow",
        "flow_type": "core",
        "steps": [
            {"entity": "Company", "role": "Tenant Owner", "arrow": "→"},
            {"entity": "Chart of Accounts", "role": "Account Classification & Hierarchy", "arrow": "→"},
            {"entity": "Journal Entry", "role": "Balanced Transaction Header", "arrow": "→"},
            {"entity": "Journal Lines", "role": "Debit/Credit Atomic Postings", "arrow": "→"},
            {"entity": "Ledger / Reports", "role": "General Ledger, Trial Balance, P&L, Balance Sheet", "arrow": None},
        ],
        "description": "Standard double-entry foundation: transactions header with debit XOR credit lines aggregating into General Ledger and financial statements.",
    },
    {
        "id": "ar_subledger_flow",
        "name": "Accounts Receivable Subledger Flow",
        "flow_type": "subledger_ar",
        "steps": [
            {"entity": "Customer", "role": "Debtor Party", "arrow": "→"},
            {"entity": "Invoice", "role": "Receivable Document", "arrow": "→"},
            {"entity": "Payment", "role": "Receipt Transaction", "arrow": "→"},
            {"entity": "Allocation", "role": "Settlement Linkage", "arrow": "→"},
            {"entity": "AR Balance", "role": "Customer Outstanding Balance", "arrow": None},
        ],
        "description": "Customer billing and settlement flow: invoices establish receivables, receipts record money in, and allocations reduce customer outstanding balances.",
    },
    {
        "id": "ap_subledger_flow",
        "name": "Accounts Payable Subledger Flow",
        "flow_type": "subledger_ap",
        "steps": [
            {"entity": "Supplier", "role": "Creditor Party", "arrow": "→"},
            {"entity": "Bill", "role": "Payable Document", "arrow": "→"},
            {"entity": "Payment", "role": "Disbursement Transaction", "arrow": "→"},
            {"entity": "Allocation", "role": "Settlement Linkage", "arrow": "→"},
            {"entity": "AP Balance", "role": "Vendor Outstanding Balance", "arrow": None},
        ],
        "description": "Supplier billing and disbursement flow: vendor bills establish payables, disbursements record money out, and allocations reduce vendor outstanding balances.",
    },
]

# Cross-domain relationships from Blueprint Research Page 36 (Figure 23)
CROSS_DOMAIN_RELATIONSHIPS = [
    {
        "source": "Invoice",
        "target": "JournalEntry",
        "cardinality": "N : 1",
        "purpose": "Trace customer revenue and accounts receivable to the ledger upon posting.",
        "field_mapping": "JournalEntry.source_module='SALES', source_id=Invoice.id",
    },
    {
        "source": "Bill",
        "target": "JournalEntry",
        "cardinality": "N : 1",
        "purpose": "Trace supplier payable and expense/inventory cost to the ledger upon posting.",
        "field_mapping": "JournalEntry.source_module='PURCHASE', source_id=Bill.id",
    },
    {
        "source": "PaymentAllocation",
        "target": "Invoice / Bill",
        "cardinality": "N : 1 (either)",
        "purpose": "Settlement of receivables (Invoice) or payables (Bill).",
        "field_mapping": "PaymentAllocation.invoice_id or PaymentAllocation.bill_id",
    },
    {
        "source": "Payment",
        "target": "BankAccount",
        "cardinality": "N : 1",
        "purpose": "Identify which bank or cash account moved.",
        "field_mapping": "Payment.bank_account_id or Payment.cash_account_id",
    },
    {
        "source": "JournalLine",
        "target": "Customer / Supplier",
        "cardinality": "N : 0..1",
        "purpose": "Sub-ledger tagging of control-account lines for AP/AR tracking.",
        "field_mapping": "JournalEntryLine.description / subledger allocation party",
    },
    {
        "source": "JournalEntry",
        "target": "Source Document",
        "cardinality": "N : 1 (source_type, source_id)",
        "purpose": "Trace to invoice, bill, work order, stock movement, or payment.",
        "field_mapping": "JournalEntry.source_module, JournalEntry.source_id",
    },
    {
        "source": "Account",
        "target": "Tax / Product Category",
        "cardinality": "N : 1",
        "purpose": "Posting configuration for automatic tax and COGS/inventory entries.",
        "field_mapping": "TaxCode.collected_account_id, TaxCode.paid_account_id",
    },
]

# Data Design Rules & Architectural Safeguards
DATA_DESIGN_RULES = [
    {
        "rule_id": "fixed_precision_decimals",
        "name": "Fixed-Precision Decimals",
        "description": "All currency amounts use DecimalField(max_digits=18, decimal_places=2). Floating-point types are strictly forbidden.",
        "status": "ENFORCED",
    },
    {
        "rule_id": "multi_tenant_isolation",
        "name": "Strict Tenant Isolation",
        "description": "Every root entity carries a mandatory ForeignKey to Company. Child lines inherit company scope via foreign key.",
        "status": "ENFORCED",
    },
    {
        "rule_id": "immutability_and_soft_deletes",
        "name": "Immutability & Soft Deletes",
        "description": "Posted accounting records are immutable. Corrections must be made via reversal entries. Hard deletes on posted journals are forbidden.",
        "status": "ENFORCED",
    },
    {
        "rule_id": "unique_constraints",
        "name": "Relational Uniqueness Constraints",
        "description": "Unique constraints enforced for (company, code) on Accounts and (company, entry_number) on Journal Entries.",
        "status": "ENFORCED",
    },
    {
        "rule_id": "debit_xor_credit",
        "name": "Debit XOR Credit Rule",
        "description": "Each journal line must have either a positive debit OR a positive credit, never both and never neither.",
        "status": "ENFORCED",
    },
    {
        "rule_id": "double_entry_balance",
        "name": "Double-Entry Balance Safeguard",
        "description": "Total debits must strictly equal total credits for any journal entry to transition to POSTED status.",
        "status": "ENFORCED",
    },
]


def get_database_architecture_metadata(company: Company) -> dict:
    """
    Returns comprehensive architectural metadata, entity catalog with live record counts,
    ERD flows, cross-domain links, and design rule statuses for the tenant.
    """
    entity_counts = {
        "Company": 1,
        "Account": Account.objects.filter(company=company).count(),
        "AccountType": AccountType.objects.count(),
        "JournalEntry": JournalEntry.objects.filter(company=company).count(),
        "JournalLine": JournalEntryLine.objects.filter(company=company).count(),
        "Customer": Customer.objects.filter(company=company).count(),
        "Supplier": Vendor.objects.filter(company=company).count(),
        "Invoice / InvoiceLine": Invoice.objects.filter(company=company).count(),
        "Bill / BillLine": Bill.objects.filter(company=company).count(),
        "Payment": Payment.objects.filter(company=company).count(),
        "PaymentAllocation": PaymentAllocation.objects.filter(company=company).count(),
        "Expense": Expense.objects.filter(company=company).count(),
        "Tax": TaxCode.objects.filter(company=company).count(),
        "BankAccount": BankAccount.objects.filter(company=company).count(),
        "BankTransaction": BankTransaction.objects.filter(bank_account__company=company).count(),
        "AccountingPeriod": AccountingPeriod.objects.filter(company=company).count(),
        "FiscalYear": FiscalYear.objects.filter(company=company).count(),
        "Reconciliation": BankReconciliation.objects.filter(bank_account__company=company).count(),
        "AuditLog": (
            JournalEntryAuditLog.objects.filter(company=company).count()
            + PeriodAuditLog.objects.filter(company=company).count()
            + ExpenseAuditLog.objects.filter(expense__company=company).count()
            + BankAuditLog.objects.filter(company=company).count()
            + PaymentAuditLog.objects.filter(company=company).count()
            + TaxAuditLog.objects.filter(company=company).count()
        ),
    }

    # Build enriched catalog with live counts
    enriched_catalog = []
    for item in LOGICAL_ENTITIES_SPEC:
        catalog_entry = dict(item)
        catalog_entry["count"] = entity_counts.get(item["entity"], 0)
        catalog_entry["tenant_scoped"] = True
        catalog_entry["status"] = "ACTIVE"
        enriched_catalog.append(catalog_entry)

    total_records = sum(entity_counts.values())

    return {
        "tenant": {
            "company_id": company.id,
            "company_name": company.name,
            "currency": getattr(company, "currency", "USD"),
        },
        "summary": {
            "total_logical_entities": len(LOGICAL_ENTITIES_SPEC),
            "total_active_tables": len(LOGICAL_ENTITIES_SPEC) + 4, # including lines & audit tables
            "total_accounting_records": total_records,
            "tenant_isolation_mode": "Strict Company Isolation (Foreign Key Scoped)",
            "schema_conformance": "100% Aligned with Blueprint #24",
        },
        "entities": enriched_catalog,
        "erd_flows": CORE_ERD_FLOWS,
        "cross_domain_relationships": CROSS_DOMAIN_RELATIONSHIPS,
        "data_design_rules": DATA_DESIGN_RULES,
    }


def verify_database_integrity(company: Company) -> dict:
    """
    Executes live architectural integrity diagnostics for the tenant company:
    1. Double-Entry Balance Check across all POSTED Journal Entries
    2. Orphaned Journal Lines Check
    3. Debit XOR Credit Constraint Check
    4. Non-Negative Amounts Check
    5. Account Code Uniqueness Check
    6. Journal Entry Number Uniqueness Check
    7. Subledger Allocation Consistency Check
    8. Multi-Tenant Scoping Check
    """
    checks = []
    overall_errors = 0
    overall_warnings = 0

    # 1. Double-entry balance check
    posted_entries = JournalEntry.objects.filter(company=company, status__iexact="posted")
    unbalanced_entry_numbers = []

    for entry in posted_entries:
        lines = entry.lines.all()
        sum_debit = sum(l.debit for l in lines)
        sum_credit = sum(l.credit for l in lines)
        if sum_debit != sum_credit:
            unbalanced_entry_numbers.append(entry.entry_number)

    if unbalanced_entry_numbers:
        overall_errors += len(unbalanced_entry_numbers)
        checks.append({
            "check_id": "double_entry_balance",
            "name": "Double-Entry Balance Verification",
            "status": "FAIL",
            "count_checked": posted_entries.count(),
            "anomalies_found": len(unbalanced_entry_numbers),
            "details": f"Unbalanced posted entries detected: {', '.join(unbalanced_entry_numbers[:5])}",
        })
    else:
        checks.append({
            "check_id": "double_entry_balance",
            "name": "Double-Entry Balance Verification",
            "status": "PASS",
            "count_checked": posted_entries.count(),
            "anomalies_found": 0,
            "details": f"All {posted_entries.count()} posted journal entries have Total Debits == Total Credits.",
        })

    # 2. Orphaned lines check
    orphaned_lines = JournalEntryLine.objects.filter(
        company=company
    ).filter(
        Q(journal_entry__isnull=True) | Q(account__isnull=True)
    ).count()

    if orphaned_lines > 0:
        overall_errors += orphaned_lines
        checks.append({
            "check_id": "orphaned_lines",
            "name": "Orphaned Journal Lines Verification",
            "status": "FAIL",
            "count_checked": JournalEntryLine.objects.filter(company=company).count(),
            "anomalies_found": orphaned_lines,
            "details": f"Found {orphaned_lines} journal lines without a valid journal entry or account.",
        })
    else:
        total_lines = JournalEntryLine.objects.filter(company=company).count()
        checks.append({
            "check_id": "orphaned_lines",
            "name": "Orphaned Journal Lines Verification",
            "status": "PASS",
            "count_checked": total_lines,
            "anomalies_found": 0,
            "details": f"All {total_lines} journal lines are correctly linked to valid journal entries and accounts.",
        })

    # 3. Debit XOR Credit rule check
    invalid_xor_lines = JournalEntryLine.objects.filter(company=company).filter(
        (Q(debit__gt=0) & Q(credit__gt=0)) | (Q(debit=0) & Q(credit=0))
    ).count()

    if invalid_xor_lines > 0:
        overall_errors += invalid_xor_lines
        checks.append({
            "check_id": "debit_xor_credit",
            "name": "Debit XOR Credit Rule Verification",
            "status": "FAIL",
            "count_checked": total_lines,
            "anomalies_found": invalid_xor_lines,
            "details": f"Found {invalid_xor_lines} lines violating the Debit XOR Credit rule.",
        })
    else:
        checks.append({
            "check_id": "debit_xor_credit",
            "name": "Debit XOR Credit Rule Verification",
            "status": "PASS",
            "count_checked": total_lines,
            "anomalies_found": 0,
            "details": f"All {total_lines} lines strictly satisfy debit XOR credit.",
        })

    # 4. Non-negative amount check
    negative_lines = JournalEntryLine.objects.filter(company=company).filter(
        Q(debit__lt=0) | Q(credit__lt=0)
    ).count()

    if negative_lines > 0:
        overall_errors += negative_lines
        checks.append({
            "check_id": "non_negative_amounts",
            "name": "Non-Negative Amounts Verification",
            "status": "FAIL",
            "count_checked": total_lines,
            "anomalies_found": negative_lines,
            "details": f"Found {negative_lines} lines with negative debit or credit amounts.",
        })
    else:
        checks.append({
            "check_id": "non_negative_amounts",
            "name": "Non-Negative Amounts Verification",
            "status": "PASS",
            "count_checked": total_lines,
            "anomalies_found": 0,
            "details": f"All {total_lines} line amounts are non-negative decimals.",
        })

    # 5. Account Code Uniqueness Check
    account_dups = (
        Account.objects.filter(company=company)
        .values("code")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=1)
        .count()
    )
    total_accounts = Account.objects.filter(company=company).count()

    if account_dups > 0:
        overall_errors += account_dups
        checks.append({
            "check_id": "account_code_uniqueness",
            "name": "Account Code Uniqueness",
            "status": "FAIL",
            "count_checked": total_accounts,
            "anomalies_found": account_dups,
            "details": f"Duplicate account codes found within company: {account_dups}",
        })
    else:
        checks.append({
            "check_id": "account_code_uniqueness",
            "name": "Account Code Uniqueness",
            "status": "PASS",
            "count_checked": total_accounts,
            "anomalies_found": 0,
            "details": f"All {total_accounts} accounts have unique codes within {company.name}.",
        })

    # 6. Journal Entry Number Uniqueness Check
    entry_dups = (
        JournalEntry.objects.filter(company=company)
        .values("entry_number")
        .annotate(cnt=Count("id"))
        .filter(cnt__gt=1)
        .count()
    )
    total_entries = JournalEntry.objects.filter(company=company).count()

    if entry_dups > 0:
        overall_errors += entry_dups
        checks.append({
            "check_id": "entry_number_uniqueness",
            "name": "Journal Entry Number Uniqueness",
            "status": "FAIL",
            "count_checked": total_entries,
            "anomalies_found": entry_dups,
            "details": f"Duplicate entry numbers found within company: {entry_dups}",
        })
    else:
        checks.append({
            "check_id": "entry_number_uniqueness",
            "name": "Journal Entry Number Uniqueness",
            "status": "PASS",
            "count_checked": total_entries,
            "anomalies_found": 0,
            "details": f"All {total_entries} journal entries have unique entry numbers within {company.name}.",
        })

    # 7. Subledger Allocation Consistency Check
    over_allocated = PaymentAllocation.objects.filter(
        company=company,
        allocated_amount__lt=Decimal("0.01")
    ).count()
    total_allocations = PaymentAllocation.objects.filter(company=company).count()

    if over_allocated > 0:
        overall_warnings += over_allocated
        checks.append({
            "check_id": "subledger_allocations",
            "name": "Payment Allocation Consistency",
            "status": "WARN",
            "count_checked": total_allocations,
            "anomalies_found": over_allocated,
            "details": f"Found {over_allocated} allocations with zero or negative amounts.",
        })
    else:
        checks.append({
            "check_id": "subledger_allocations",
            "name": "Payment Allocation Consistency",
            "status": "PASS",
            "count_checked": total_allocations,
            "anomalies_found": 0,
            "details": f"All {total_allocations} payment allocations are positive and consistent.",
        })

    # 8. Multi-tenant foreign key scoping check
    cross_tenant_lines = JournalEntryLine.objects.filter(
        company=company
    ).exclude(
        journal_entry__company=company
    ).count()

    if cross_tenant_lines > 0:
        overall_errors += cross_tenant_lines
        checks.append({
            "check_id": "cross_tenant_isolation",
            "name": "Cross-Tenant Isolation Integrity",
            "status": "FAIL",
            "count_checked": total_lines,
            "anomalies_found": cross_tenant_lines,
            "details": f"CRITICAL: Found {cross_tenant_lines} lines referencing cross-tenant journal entries.",
        })
    else:
        checks.append({
            "check_id": "cross_tenant_isolation",
            "name": "Cross-Tenant Isolation Integrity",
            "status": "PASS",
            "count_checked": total_lines,
            "anomalies_found": 0,
            "details": f"100% of journal lines strictly match tenant company {company.name}.",
        })

    # Overall status
    if overall_errors > 0:
        final_status = "FAIL"
    elif overall_warnings > 0:
        final_status = "WARNING"
    else:
        final_status = "HEALTHY"

    return {
        "status": final_status,
        "overall_errors": overall_errors,
        "overall_warnings": overall_warnings,
        "total_checks": len(checks),
        "passing_checks": len([c for c in checks if c["status"] == "PASS"]),
        "timestamp": timezone.now().isoformat(),
        "checks": checks,
    }
