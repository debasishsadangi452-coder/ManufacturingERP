"""
Blueprint Section #17 — Cash & Bank Domain Service Engine.
Handles bank account master management, opening balance double-entry postings,
manual deposits, withdrawals, internal transfers, statement CSV imports,
reconciliation matching workflows, and auditable history.
"""

import csv
import io
import hashlib
from datetime import datetime, date
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounting.models import (
    Account,
    AccountingSettings,
    JournalEntry,
    JournalEntryLine,
    BankAccount,
    BankReconciliation,
    BankTransaction,
    BankAuditLog,
)
from accounting.engine import post_journal_entry


# ==============================================================================
# 1. OPENING BALANCE SERVICE
# ==============================================================================

def record_opening_balance(bank_account_id, amount, balance_date, user, equity_account_id=None, notes=""):
    """
    Sets and posts an auditable opening balance for a BankAccount.
    Creates a double-entry JournalEntry:
      - Positive amount: DR Bank Asset Account, CR Opening Balance Equity
      - Overdraft/Negative: DR Opening Balance Equity, CR Bank Asset Account
    Ensures Debit = Credit, validates lock dates and periods, prevents duplicate opening balances.
    """
    with transaction.atomic():
        bank_account = BankAccount.objects.select_for_update().get(pk=bank_account_id)
        company = bank_account.company

        if not bank_account.is_active:
            raise ValidationError(_(f"Bank account '{bank_account.account_name}' is inactive."))

        if bank_account.opening_balance_posted:
            raise ValidationError(_(
                f"Opening balance has already been posted for bank account '{bank_account.account_name}' "
                f"(JE: {bank_account.opening_balance_journal_entry.entry_number if bank_account.opening_balance_journal_entry else 'Posted'})."
            ))

        try:
            amount_dec = Decimal(str(amount))
        except (ValueError, TypeError, InvalidOperation):
            raise ValidationError(_("Invalid opening balance amount."))

        if amount_dec == Decimal("0.00"):
            raise ValidationError(_("Opening balance amount cannot be zero."))

        if isinstance(balance_date, str):
            balance_date = datetime.strptime(balance_date, "%Y-%m-%d").date()

        # Check AccountingSettings lock date
        settings = getattr(company, "accounting_settings", None)
        if settings and settings.lock_date and balance_date <= settings.lock_date:
            raise ValidationError(_(f"Cannot post opening balance on or prior to the accounting lock date ({settings.lock_date})."))

        # Resolve Equity Offset Account
        equity_acc = None
        if equity_account_id:
            equity_acc = Account.objects.filter(
                pk=equity_account_id,
                company=company,
                is_active=True
            ).exclude(children__isnull=False).first()
            if not equity_acc:
                raise ValidationError(_("Specified opening balance equity account is invalid or not an active leaf account."))
        elif settings and settings.opening_balance_equity_account:
            equity_acc = settings.opening_balance_equity_account
        else:
            # Fallback to standard 3010 or any leaf equity account
            equity_acc = Account.objects.filter(
                company=company,
                code="3010",
                is_active=True
            ).exclude(children__isnull=False).first()
            if not equity_acc:
                equity_acc = Account.objects.filter(
                    company=company,
                    account_type__category="equity",
                    is_active=True
                ).exclude(children__isnull=False).first()

        if not equity_acc:
            raise ValidationError(_(
                f"No active leaf Equity account found for company '{company.name}'. "
                "Ensure account 3010 or an Equity account exists in the Chart of Accounts."
            ))

        bank_gl_acc = bank_account.gl_account
        if not bank_gl_acc.is_active or bank_gl_acc.is_header:
            raise ValidationError(_(f"Bank GL account '{bank_gl_acc.code}' must be an active leaf account."))

        abs_amount = abs(amount_dec)
        ref_text = f"OB-{bank_account.bank_name[:6].upper()}-{bank_account.id}"

        # Create Draft Journal Entry
        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=balance_date,
            source_module="cash_bank",
            source_id=bank_account.id,
            reference=ref_text,
            description=f"Opening Balance for {bank_account.bank_name} - {bank_account.account_name} ({bank_account.masked_account_number})",
            created_by=user,
            status="draft",
        )

        if amount_dec > Decimal("0.00"):
            # Normal positive balance: DR Bank Asset, CR Equity
            line_bank = JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=bank_gl_acc,
                line_number=1,
                debit=abs_amount,
                credit=Decimal("0.00"),
                description=f"Opening Balance - {bank_account.account_name}",
                is_reconciled=True,
            )
            line_equity = JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=equity_acc,
                line_number=2,
                debit=Decimal("0.00"),
                credit=abs_amount,
                description=f"Opening Balance Equity Offset - {bank_account.account_name}",
            )
        else:
            # Overdraft negative balance: DR Equity, CR Bank Asset
            line_equity = JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=equity_acc,
                line_number=1,
                debit=abs_amount,
                credit=Decimal("0.00"),
                description=f"Opening Balance Overdraft Offset - {bank_account.account_name}",
            )
            line_bank = JournalEntryLine.objects.create(
                company=company,
                journal_entry=entry,
                account=bank_gl_acc,
                line_number=2,
                debit=Decimal("0.00"),
                credit=abs_amount,
                description=f"Opening Balance (Overdraft) - {bank_account.account_name}",
                is_reconciled=True,
            )

        # Post atomically through #8 engine
        posted_je = post_journal_entry(entry.id, user=user, company=company)

        # Create corresponding BankTransaction record
        direction = "inflow" if amount_dec > Decimal("0.00") else "outflow"
        bt = BankTransaction.objects.create(
            company=company,
            bank_account=bank_account,
            transaction_date=balance_date,
            amount=abs_amount,
            direction=direction,
            transaction_type="opening_balance",
            source="manual",
            description=f"Opening Balance - {bank_account.account_name}",
            reference=ref_text,
            counterparty="Opening Balance",
            external_id=f"OB-{bank_account.id}-{balance_date.isoformat()}",
            matching_status="matched",
            reconciliation_status="reconciled",
            journal_entry=posted_je,
            journal_entry_line=line_bank,
            source_document_ref=posted_je.entry_number,
            created_by=user,
        )

        # Update BankAccount state
        bank_account.opening_balance = amount_dec
        bank_account.opening_balance_date = balance_date
        bank_account.opening_balance_posted = True
        bank_account.opening_balance_journal_entry = posted_je
        bank_account.reconciled_balance = amount_dec
        bank_account.last_reconciliation_date = balance_date
        bank_account.save()

        # Audit log
        BankAuditLog.objects.create(
            company=company,
            bank_account=bank_account,
            action="opening_balance_posted",
            actor=user,
            details={
                "amount": str(amount_dec),
                "date": balance_date.isoformat(),
                "journal_entry": posted_je.entry_number,
                "equity_account": equity_acc.code,
            },
            notes=notes or "Opening balance established and posted to General Ledger."
        )

        return {
            "bank_account": bank_account,
            "journal_entry": posted_je,
            "transaction": bt,
        }


# ==============================================================================
# 2. DEPOSITS, WITHDRAWALS & TRANSFERS
# ==============================================================================

def record_deposit(bank_account_id, amount, txn_date, offset_account_id, user, description="", reference="", counterparty=""):
    """
    Records a manual money-in Deposit into a BankAccount.
    DR Bank Asset Account
    CR Offset Account (e.g. Sales, Other Income, Clearing, Capital)
    """
    with transaction.atomic():
        bank_account = BankAccount.objects.select_for_update().get(pk=bank_account_id)
        company = bank_account.company

        if not bank_account.is_active:
            raise ValidationError(_(f"Bank account '{bank_account.account_name}' is inactive."))

        try:
            amount_dec = Decimal(str(amount))
        except (ValueError, TypeError, InvalidOperation):
            raise ValidationError(_("Invalid deposit amount."))

        if amount_dec <= Decimal("0.00"):
            raise ValidationError(_("Deposit amount must be greater than zero."))

        if isinstance(txn_date, str):
            txn_date = datetime.strptime(txn_date, "%Y-%m-%d").date()

        offset_acc = Account.objects.filter(
            pk=offset_account_id,
            company=company,
            is_active=True
        ).exclude(children__isnull=False).first()
        if not offset_acc:
            raise ValidationError(_("Offset account is invalid, inactive, or a parent header account."))

        bank_gl = bank_account.gl_account
        if bank_gl.pk == offset_acc.pk:
            raise ValidationError(_("Offset account cannot be the same as the bank account GL account."))

        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=txn_date,
            source_module="cash_bank",
            source_id=bank_account.id,
            reference=reference or f"DEP-{bank_account.id}",
            description=description or f"Bank Deposit: {bank_account.account_name}",
            created_by=user,
            status="draft",
        )

        line_bank = JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=bank_gl,
            line_number=1,
            debit=amount_dec,
            credit=Decimal("0.00"),
            description=description or f"Deposit into {bank_account.account_name}",
        )
        line_offset = JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=offset_acc,
            line_number=2,
            debit=Decimal("0.00"),
            credit=amount_dec,
            description=f"Offset for Deposit into {bank_account.account_name}",
        )

        posted_je = post_journal_entry(entry.id, user=user, company=company)

        bt = BankTransaction.objects.create(
            company=company,
            bank_account=bank_account,
            transaction_date=txn_date,
            amount=amount_dec,
            direction="inflow",
            transaction_type="deposit",
            source="manual",
            description=description or f"Deposit: {counterparty or bank_account.account_name}",
            reference=reference,
            counterparty=counterparty,
            matching_status="matched",
            reconciliation_status="unreconciled",
            journal_entry=posted_je,
            journal_entry_line=line_bank,
            source_document_ref=posted_je.entry_number,
            created_by=user,
        )

        BankAuditLog.objects.create(
            company=company,
            bank_account=bank_account,
            action="deposit_recorded",
            actor=user,
            details={
                "amount": str(amount_dec),
                "date": txn_date.isoformat(),
                "journal_entry": posted_je.entry_number,
                "offset_account": offset_acc.code,
            },
            notes=description,
        )

        return bt


def record_withdrawal(bank_account_id, amount, txn_date, offset_account_id, user, description="", reference="", counterparty="", transaction_type="withdrawal"):
    """
    Records a manual money-out Withdrawal or Fee from a BankAccount.
    DR Offset Account (e.g. Operating Expense, Bank Charges, Clearing)
    CR Bank Asset Account
    """
    with transaction.atomic():
        bank_account = BankAccount.objects.select_for_update().get(pk=bank_account_id)
        company = bank_account.company

        if not bank_account.is_active:
            raise ValidationError(_(f"Bank account '{bank_account.account_name}' is inactive."))

        try:
            amount_dec = Decimal(str(amount))
        except (ValueError, TypeError, InvalidOperation):
            raise ValidationError(_("Invalid withdrawal amount."))

        if amount_dec <= Decimal("0.00"):
            raise ValidationError(_("Withdrawal amount must be greater than zero."))

        if isinstance(txn_date, str):
            txn_date = datetime.strptime(txn_date, "%Y-%m-%d").date()

        offset_acc = Account.objects.filter(
            pk=offset_account_id,
            company=company,
            is_active=True
        ).exclude(children__isnull=False).first()
        if not offset_acc:
            raise ValidationError(_("Offset account is invalid, inactive, or a parent header account."))

        bank_gl = bank_account.gl_account
        if bank_gl.pk == offset_acc.pk:
            raise ValidationError(_("Offset account cannot be the same as the bank account GL account."))

        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=txn_date,
            source_module="cash_bank",
            source_id=bank_account.id,
            reference=reference or f"WTH-{bank_account.id}",
            description=description or f"Bank Withdrawal: {bank_account.account_name}",
            created_by=user,
            status="draft",
        )

        line_offset = JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=offset_acc,
            line_number=1,
            debit=amount_dec,
            credit=Decimal("0.00"),
            description=f"Offset for Withdrawal from {bank_account.account_name}",
        )
        line_bank = JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=bank_gl,
            line_number=2,
            debit=Decimal("0.00"),
            credit=amount_dec,
            description=description or f"Withdrawal from {bank_account.account_name}",
        )

        posted_je = post_journal_entry(entry.id, user=user, company=company)

        actual_txn_type = transaction_type if transaction_type in ["withdrawal", "fee", "other"] else "withdrawal"
        bt = BankTransaction.objects.create(
            company=company,
            bank_account=bank_account,
            transaction_date=txn_date,
            amount=amount_dec,
            direction="outflow",
            transaction_type=actual_txn_type,
            source="manual",
            description=description or f"Withdrawal: {counterparty or bank_account.account_name}",
            reference=reference,
            counterparty=counterparty,
            matching_status="matched",
            reconciliation_status="unreconciled",
            journal_entry=posted_je,
            journal_entry_line=line_bank,
            source_document_ref=posted_je.entry_number,
            created_by=user,
        )

        BankAuditLog.objects.create(
            company=company,
            bank_account=bank_account,
            action="withdrawal_recorded",
            actor=user,
            details={
                "amount": str(amount_dec),
                "date": txn_date.isoformat(),
                "journal_entry": posted_je.entry_number,
                "offset_account": offset_acc.code,
            },
            notes=description,
        )

        return bt


def record_transfer(from_account_id, to_account_id, amount, txn_date, user, description="", reference=""):
    """
    Records an internal transfer between two bank accounts within the same company.
    DR Destination Bank GL Account
    CR Source Bank GL Account
    Creates 2 linked BankTransaction records (one outflow, one inflow).
    """
    with transaction.atomic():
        if str(from_account_id) == str(to_account_id):
            raise ValidationError(_("Source and destination bank accounts must be different."))

        from_acc = BankAccount.objects.select_for_update().get(pk=from_account_id)
        to_acc = BankAccount.objects.select_for_update().get(pk=to_account_id)

        if from_acc.company_id != to_acc.company_id:
            raise ValidationError(_("Cannot transfer funds between different companies."))

        company = from_acc.company
        if not from_acc.is_active or not to_acc.is_active:
            raise ValidationError(_("Both source and destination bank accounts must be active."))

        try:
            amount_dec = Decimal(str(amount))
        except (ValueError, TypeError, InvalidOperation):
            raise ValidationError(_("Invalid transfer amount."))

        if amount_dec <= Decimal("0.00"):
            raise ValidationError(_("Transfer amount must be greater than zero."))

        if isinstance(txn_date, str):
            txn_date = datetime.strptime(txn_date, "%Y-%m-%d").date()

        ref_str = reference or f"TRF-{from_acc.id}-TO-{to_acc.id}"
        memo = description or f"Transfer from {from_acc.account_name} to {to_acc.account_name}"

        entry = JournalEntry.objects.create(
            company=company,
            transaction_date=txn_date,
            source_module="cash_bank",
            source_id=from_acc.id,
            reference=ref_str,
            description=memo,
            created_by=user,
            status="draft",
        )

        line_to = JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=to_acc.gl_account,
            line_number=1,
            debit=amount_dec,
            credit=Decimal("0.00"),
            description=f"Transfer In from {from_acc.account_name}",
        )
        line_from = JournalEntryLine.objects.create(
            company=company,
            journal_entry=entry,
            account=from_acc.gl_account,
            line_number=2,
            debit=Decimal("0.00"),
            credit=amount_dec,
            description=f"Transfer Out to {to_acc.account_name}",
        )

        posted_je = post_journal_entry(entry.id, user=user, company=company)

        # Source bank transaction (Outflow)
        bt_from = BankTransaction.objects.create(
            company=company,
            bank_account=from_acc,
            transaction_date=txn_date,
            amount=amount_dec,
            direction="outflow",
            transaction_type="transfer",
            source="manual",
            description=f"Transfer Out to {to_acc.account_name}",
            reference=ref_str,
            counterparty=to_acc.account_name,
            matching_status="matched",
            reconciliation_status="unreconciled",
            journal_entry=posted_je,
            journal_entry_line=line_from,
            source_document_ref=posted_je.entry_number,
            created_by=user,
        )

        # Destination bank transaction (Inflow)
        bt_to = BankTransaction.objects.create(
            company=company,
            bank_account=to_acc,
            transaction_date=txn_date,
            amount=amount_dec,
            direction="inflow",
            transaction_type="transfer",
            source="manual",
            description=f"Transfer In from {from_acc.account_name}",
            reference=ref_str,
            counterparty=from_acc.account_name,
            matching_status="matched",
            reconciliation_status="unreconciled",
            journal_entry=posted_je,
            journal_entry_line=line_to,
            source_document_ref=posted_je.entry_number,
            created_by=user,
        )

        # Link counterparties
        bt_from.transfer_counterpart = bt_to
        bt_from.save(update_fields=["transfer_counterpart"])
        bt_to.transfer_counterpart = bt_from
        bt_to.save(update_fields=["transfer_counterpart"])

        BankAuditLog.objects.create(
            company=company,
            bank_account=from_acc,
            action="transfer_recorded",
            actor=user,
            details={
                "amount": str(amount_dec),
                "to_account": to_acc.account_name,
                "journal_entry": posted_je.entry_number,
            },
            notes=memo,
        )

        return {
            "journal_entry": posted_je,
            "outflow_transaction": bt_from,
            "inflow_transaction": bt_to,
        }


# ==============================================================================
# 3. STATEMENT CSV IMPORT & IDEMPOTENCY
# ==============================================================================

def import_bank_statement_csv(bank_account_id, csv_text_or_file, user):
    """
    Parses and imports bank statement records from a CSV file or string.
    Features:
      - Validates columns: date, amount (or debit/credit), description, reference/external_id.
      - Enforces idempotency via external_id / deterministic cryptographic hash.
      - Skips existing duplicate rows without failing the entire import.
      - Atomic transaction safety.
    Returns:
      dict with imported_count, skipped_count, errors list.
    """
    with transaction.atomic():
        bank_account = BankAccount.objects.select_for_update().get(pk=bank_account_id)
        company = bank_account.company

        if not bank_account.is_active:
            raise ValidationError(_(f"Bank account '{bank_account.account_name}' is inactive."))

        if hasattr(csv_text_or_file, "read"):
            content = csv_text_or_file.read()
            if isinstance(content, bytes):
                content = content.decode("utf-8-sig", errors="replace")
        else:
            content = str(csv_text_or_file)

        reader = csv.DictReader(io.StringIO(content))
        if not reader.fieldnames:
            raise ValidationError(_("Uploaded CSV file is empty or missing headers."))

        # Normalize fieldnames lowercase stripped
        field_map = {fn.strip().lower().replace(" ", "_"): fn for fn in reader.fieldnames}

        # Date column resolution
        date_col = next((field_map[k] for k in ["date", "txn_date", "transaction_date", "post_date", "booking_date"] if k in field_map), None)
        if not date_col:
            raise ValidationError(_("Required column 'date' or 'transaction_date' missing from CSV."))

        # Amount column resolution (either amount or debit & credit)
        amount_col = next((field_map[k] for k in ["amount", "txn_amount", "transaction_amount"] if k in field_map), None)
        debit_col = next((field_map[k] for k in ["debit", "withdrawal", "money_out"] if k in field_map), None)
        credit_col = next((field_map[k] for k in ["credit", "deposit", "money_in"] if k in field_map), None)

        if not amount_col and not (debit_col and credit_col):
            raise ValidationError(_("CSV must contain either an 'amount' column or both 'debit' and 'credit' columns."))

        desc_col = next((field_map[k] for k in ["description", "memo", "details", "narration", "payee"] if k in field_map), None)
        ref_col = next((field_map[k] for k in ["reference", "ref", "check", "check_no", "check_number", "cheque", "cheque_no", "chk_no"] if k in field_map), None)
        ext_col = next((field_map[k] for k in ["external_id", "id", "fitid", "transaction_id", "bank_ref"] if k in field_map), None)

        imported = 0
        skipped = 0
        errors = []

        for row_idx, row in enumerate(reader, start=2):
            raw_date = row.get(date_col, "").strip()
            if not raw_date:
                continue

            # Parse date
            parsed_date = None
            for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%m-%Y"):
                try:
                    parsed_date = datetime.strptime(raw_date, fmt).date()
                    break
                except ValueError:
                    continue

            if not parsed_date:
                errors.append(f"Row {row_idx}: Unable to parse date '{raw_date}'.")
                continue

            # Parse amount & direction
            direction = "inflow"
            amt_dec = Decimal("0.00")

            try:
                if amount_col:
                    raw_amt = row.get(amount_col, "").strip().replace(",", "").replace("$", "")
                    raw_amt_dec = Decimal(raw_amt)
                    if raw_amt_dec >= Decimal("0.00"):
                        direction = "inflow"
                        amt_dec = raw_amt_dec
                    else:
                        direction = "outflow"
                        amt_dec = abs(raw_amt_dec)
                else:
                    raw_dr = row.get(debit_col, "").strip().replace(",", "").replace("$", "") or "0"
                    raw_cr = row.get(credit_col, "").strip().replace(",", "").replace("$", "") or "0"
                    dr_dec = Decimal(raw_dr)
                    cr_dec = Decimal(raw_cr)

                    if dr_dec > Decimal("0.00"):
                        # Debit in statement is money out (withdrawal)
                        direction = "outflow"
                        amt_dec = dr_dec
                    elif cr_dec > Decimal("0.00"):
                        # Credit in statement is money in (deposit)
                        direction = "inflow"
                        amt_dec = cr_dec
                    else:
                        continue
            except (ValueError, TypeError, InvalidOperation):
                errors.append(f"Row {row_idx}: Invalid amount format.")
                continue

            if amt_dec == Decimal("0.00"):
                continue

            desc = (row.get(desc_col, "") if desc_col else "").strip()
            ref = (row.get(ref_col, "") if ref_col else "").strip()
            ext_id = (row.get(ext_col, "") if ext_col else "").strip()

            # Deterministic hash if external_id not provided
            if not ext_id:
                hash_input = f"{bank_account.id}-{parsed_date.isoformat()}-{amt_dec}-{direction}-{ref}-{desc}"
                ext_id = f"CSV-{hashlib.sha256(hash_input.encode('utf-8')).hexdigest()[:16]}"

            # Idempotency check: duplicate prevention
            if BankTransaction.objects.filter(bank_account=bank_account, external_id=ext_id).exists():
                skipped += 1
                continue

            # Auto-suggest matching against unreconciled journal lines for this bank GL account
            # Match condition: same amount, direction match (inflow=debit>0, outflow=credit>0), within 7 days
            matching_jl = None
            delta_days = 7
            potential_lines = JournalEntryLine.objects.filter(
                company=company,
                account=bank_account.gl_account,
                journal_entry__status="posted",
                is_reconciled=False,
                journal_entry__transaction_date__gte=parsed_date - timezone.timedelta(days=delta_days),
                journal_entry__transaction_date__lte=parsed_date + timezone.timedelta(days=delta_days),
            )
            if direction == "inflow":
                potential_lines = potential_lines.filter(debit=amt_dec, credit=Decimal("0.00"))
            else:
                potential_lines = potential_lines.filter(credit=amt_dec, debit=Decimal("0.00"))

            # Exclude lines already matched to another bank transaction
            potential_lines = potential_lines.exclude(bank_transactions__isnull=False)
            matched_candidate = potential_lines.first()

            bt = BankTransaction.objects.create(
                company=company,
                bank_account=bank_account,
                transaction_date=parsed_date,
                amount=amt_dec,
                direction=direction,
                transaction_type="statement_line",
                source="import",
                description=desc or f"Imported statement line - {ref}",
                reference=ref,
                external_id=ext_id,
                matching_status="suggested" if matched_candidate else "unmatched",
                reconciliation_status="unreconciled",
                journal_entry=matched_candidate.journal_entry if matched_candidate else None,
                journal_entry_line=matched_candidate if matched_candidate else None,
                created_by=user,
            )
            imported += 1

        BankAuditLog.objects.create(
            company=company,
            bank_account=bank_account,
            action="statement_imported",
            actor=user,
            details={
                "imported_count": imported,
                "skipped_count": skipped,
                "error_count": len(errors),
            },
            notes=f"Statement imported: {imported} new rows, {skipped} duplicates skipped.",
        )

        return {
            "imported_count": imported,
            "skipped_count": skipped,
            "errors": errors,
        }


# ==============================================================================
# 4. RECONCILIATION WORKFLOW & QUEUE
# ==============================================================================

def get_unreconciled_transactions_queue(bank_account_id, start_date=None, end_date=None, search=None):
    """
    Returns unreconciled BankTransaction records and unreconciled JournalEntryLine records
    for the bank account, providing full visibility for two-sided bank reconciliation.
    """
    bank_account = BankAccount.objects.get(pk=bank_account_id)
    company = bank_account.company

    # 1. Unreconciled Bank Transactions
    bt_qs = BankTransaction.objects.filter(
        bank_account=bank_account,
        reconciliation_status="unreconciled"
    )
    if start_date:
        bt_qs = bt_qs.filter(transaction_date__gte=start_date)
    if end_date:
        bt_qs = bt_qs.filter(transaction_date__lte=end_date)
    if search:
        bt_qs = bt_qs.filter(
            models.Q(description__icontains=search) |
            models.Q(reference__icontains=search) |
            models.Q(counterparty__icontains=search) |
            models.Q(external_id__icontains=search)
        )

    # 2. Unreconciled General Ledger Journal Lines
    jl_qs = JournalEntryLine.objects.filter(
        company=company,
        account=bank_account.gl_account,
        journal_entry__status="posted",
        is_reconciled=False
    ).select_related("journal_entry", "account")

    if start_date:
        jl_qs = jl_qs.filter(journal_entry__transaction_date__gte=start_date)
    if end_date:
        jl_qs = jl_qs.filter(journal_entry__transaction_date__lte=end_date)
    if search:
        jl_qs = jl_qs.filter(
            models.Q(description__icontains=search) |
            models.Q(journal_entry__entry_number__icontains=search) |
            models.Q(journal_entry__reference__icontains=search)
        )

    return {
        "bank_account_id": bank_account.id,
        "bank_account_name": bank_account.account_name,
        "bank_transactions": bt_qs.order_by("-transaction_date", "-id"),
        "journal_lines": jl_qs.order_by("-journal_entry__transaction_date", "-id"),
    }


def reconcile_bank_account(bank_account_id, statement_date, statement_balance, transaction_ids, journal_line_ids, user, notes=""):
    """
    Executes atomic bank reconciliation.
    Clears confirmed bank transactions and general ledger lines, creates an immutable
    BankReconciliation record, updates bank_account.reconciled_balance, and records audit trail.
    """
    with transaction.atomic():
        bank_account = BankAccount.objects.select_for_update().get(pk=bank_account_id)
        company = bank_account.company

        if not bank_account.is_active:
            raise ValidationError(_(f"Bank account '{bank_account.account_name}' is inactive."))

        if isinstance(statement_date, str):
            statement_date = datetime.strptime(statement_date, "%Y-%m-%d").date()

        try:
            stmt_balance_dec = Decimal(str(statement_balance))
        except (ValueError, TypeError, InvalidOperation):
            raise ValidationError(_("Invalid statement balance amount."))

        # 1. Fetch & lock transactions
        txns = list(BankTransaction.objects.select_for_update().filter(
            pk__in=transaction_ids or [],
            bank_account=bank_account
        ))

        for t in txns:
            if t.reconciliation_status == "reconciled":
                raise ValidationError(_(f"Transaction ID {t.id} ({t.description}) is already reconciled."))

        # 2. Fetch & lock journal lines
        jlines = list(JournalEntryLine.objects.select_for_update().filter(
            pk__in=journal_line_ids or [],
            company=company,
            account=bank_account.gl_account,
            journal_entry__status="posted"
        ))

        for jl in jlines:
            if jl.is_reconciled:
                raise ValidationError(_(f"Journal line ID {jl.id} is already reconciled."))

        starting_bal = bank_account.reconciled_balance
        diff = stmt_balance_dec - (starting_bal + sum(
            t.amount if t.direction == "inflow" else -t.amount for t in txns
        ))

        # Create BankReconciliation record
        rec = BankReconciliation.objects.create(
            company=company,
            bank_account=bank_account,
            statement_date=statement_date,
            statement_balance=stmt_balance_dec,
            starting_balance=starting_bal,
            reconciled_balance=stmt_balance_dec,
            difference=diff,
            status="completed",
            notes=notes,
            reconciled_by=user,
            reconciled_at=timezone.now(),
        )

        # Mark transactions as reconciled
        for t in txns:
            t.reconciliation_status = "reconciled"
            t.matching_status = "matched"
            t.reconciliation = rec
            t.save(update_fields=["reconciliation_status", "matching_status", "reconciliation"])

        # Mark journal lines as reconciled
        for jl in jlines:
            jl.is_reconciled = True
            jl.reconciliation = rec
            jl.save(update_fields=["is_reconciled", "reconciliation"])

        # Update bank account reconciled balance
        bank_account.reconciled_balance = stmt_balance_dec
        bank_account.last_reconciliation_date = statement_date
        bank_account.save(update_fields=["reconciled_balance", "last_reconciliation_date"])

        BankAuditLog.objects.create(
            company=company,
            bank_account=bank_account,
            reconciliation=rec,
            action="reconciliation_completed",
            actor=user,
            details={
                "reconciliation_number": rec.reconciliation_number,
                "statement_date": statement_date.isoformat(),
                "statement_balance": str(stmt_balance_dec),
                "cleared_transactions_count": len(txns),
                "cleared_journal_lines_count": len(jlines),
            },
            notes=notes or "Reconciliation cycle successfully completed.",
        )

        return rec


def reopen_reconciliation(reconciliation_id, user, reason=""):
    """
    Reopens a previously completed reconciliation cycle.
    Un-reconciles all cleared transactions and journal lines, restores starting balance,
    and records an immutable audit log.
    """
    with transaction.atomic():
        rec = BankReconciliation.objects.select_for_update().get(pk=reconciliation_id)
        bank_account = BankAccount.objects.select_for_update().get(pk=rec.bank_account_id)
        company = rec.company

        if rec.status == "reopened":
            raise ValidationError(_(f"Reconciliation {rec.reconciliation_number} is already reopened."))

        # Reset transactions
        txns = list(rec.transactions.select_for_update().all())
        for t in txns:
            t.reconciliation_status = "unreconciled"
            t.reconciliation = None
            t.save(update_fields=["reconciliation_status", "reconciliation"])

        # Reset journal lines
        jlines = list(rec.reconciled_journal_lines.select_for_update().all())
        for jl in jlines:
            jl.is_reconciled = False
            jl.reconciliation = None
            jl.save(update_fields=["is_reconciled", "reconciliation"])

        # Restore bank account reconciled balance
        bank_account.reconciled_balance = rec.starting_balance
        # Find latest completed reconciliation before this one
        prev = BankReconciliation.objects.filter(
            bank_account=bank_account,
            status="completed"
        ).exclude(pk=rec.pk).order_by("-statement_date").first()
        bank_account.last_reconciliation_date = prev.statement_date if prev else bank_account.opening_balance_date
        bank_account.save(update_fields=["reconciled_balance", "last_reconciliation_date"])

        rec.status = "reopened"
        rec.notes = (rec.notes + f"\n[Reopened on {timezone.now().strftime('%Y-%m-%d %H:%M')}: {reason}]").strip()
        rec.save(update_fields=["status", "notes"])

        BankAuditLog.objects.create(
            company=company,
            bank_account=bank_account,
            reconciliation=rec,
            action="reconciliation_reopened",
            actor=user,
            details={
                "reconciliation_number": rec.reconciliation_number,
                "reopened_transactions": len(txns),
                "reopened_journal_lines": len(jlines),
            },
            notes=reason or "Reconciliation cycle reopened.",
        )

        return rec


# ==============================================================================
# 5. CASH & BANK SUMMARY KPIS
# ==============================================================================

def get_cash_bank_summary(company):
    """
    Calculates tenant-wide Cash & Bank KPIs:
      - Total Book GL Balance
      - Total Reconciled Balance
      - Total Unreconciled Discrepancy
      - Active Accounts Count
      - Unreconciled Transactions Count
    """
    accounts = BankAccount.objects.filter(company=company, is_active=True)
    
    total_book_balance = Decimal("0.00")
    total_reconciled_balance = Decimal("0.00")
    
    for acc in accounts:
        total_book_balance += acc.current_gl_balance
        total_reconciled_balance += acc.reconciled_balance

    total_unreconciled_diff = total_book_balance - total_reconciled_balance

    unreconciled_txns_count = BankTransaction.objects.filter(
        company=company,
        reconciliation_status="unreconciled"
    ).count()

    reconciliations_count = BankReconciliation.objects.filter(
        company=company,
        status="completed"
    ).count()

    return {
        "active_accounts_count": accounts.count(),
        "total_book_balance": total_book_balance,
        "total_reconciled_balance": total_reconciled_balance,
        "total_unreconciled_difference": total_unreconciled_diff,
        "unreconciled_transactions_count": unreconciled_txns_count,
        "completed_reconciliations_count": reconciliations_count,
    }
