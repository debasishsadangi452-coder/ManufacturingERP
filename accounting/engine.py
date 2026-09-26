"""
Double-Entry Accounting Engine (Blueprint Section No. 8)
Provides transaction-safe atomic posting, balance equilibrium enforcement,
and reverse-entry accounting workflows.
"""

from decimal import Decimal
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounting.models import JournalEntry, JournalEntryLine


def record_journal_audit_log(entry, action, user=None, details=None, ip_address=""):
    """
    Appends an immutable audit event for a JournalEntry lifecycle transition (Blueprint #20).
    """
    try:
        from accounting.models import JournalEntryAuditLog
        return JournalEntryAuditLog.objects.create(
            company=entry.company,
            journal_entry=entry,
            action=action,
            performed_by=user if (user and user.is_authenticated) else None,
            details=details or {},
            ip_address=ip_address or "",
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Failed to record journal audit log: {e}")
        return None


def post_journal_entry(entry_id, user, company=None):
    """
    Atomically posts a draft or approved JournalEntry to the General Ledger.
    Enforces double-entry equilibrium, period status, lock dates,
    and prevents duplicate posting or modification.
    """
    with transaction.atomic():
        qs = JournalEntry.objects.select_for_update()
        if company:
            qs = qs.filter(company=company)
        
        try:
            entry = qs.get(pk=entry_id)
        except JournalEntry.DoesNotExist:
            raise ValidationError(_("Journal entry not found."))

        # 1. State check
        if entry.status == "posted":
            raise ValidationError(_("This journal entry has already been posted."))
        if entry.status == "reversed":
            raise ValidationError(_("Cannot post a reversed journal entry."))
        if entry.status == "rejected":
            raise ValidationError(_("Cannot post a rejected journal entry. Re-open or adjust the draft entry first."))
        if entry.status not in ["draft", "approved"]:
            raise ValidationError(_(f"Invalid entry status '{entry.status}'. Only draft or approved entries can be posted."))

        # 2. Assign and validate period
        entry.auto_assign_period()
        if not entry.accounting_period:
            raise ValidationError({
                "accounting_period": _(
                    f"No accounting period found for transaction date {entry.transaction_date}. "
                    "Ensure an open accounting period exists for this date."
                )
            })

        # 3. Double-entry mathematical balance validation
        entry.validate_double_entry()

        # 4. Model clean (period status, lock date, boundaries)
        # Temporarily mark status as posted to run posted-specific validations
        entry.status = "posted"
        entry.clean()

        # 5. Commit posting metadata
        entry.posted_at = timezone.now()
        entry.posted_by = user if (user and user.is_authenticated) else None
        entry.save()

        # 6. Immutable audit log record
        record_journal_audit_log(
            entry,
            action="POSTED",
            user=user,
            details={
                "entry_number": entry.entry_number,
                "total_debit": str(entry.total_debit),
                "total_credit": str(entry.total_credit),
                "period": entry.accounting_period.name if entry.accounting_period else None,
            }
        )

        return entry


def reverse_journal_entry(entry_id, user, reason="", reversal_date=None, company=None):
    """
    Creates an atomic reversing journal entry that exactly inverts the debit/credit
    lines of the original posted journal entry, establishing full audit trail lineage.
    """
    with transaction.atomic():
        qs = JournalEntry.objects.select_for_update()
        if company:
            qs = qs.filter(company=company)

        try:
            original = qs.get(pk=entry_id)
        except JournalEntry.DoesNotExist:
            raise ValidationError(_("Journal entry not found."))

        if original.status != "posted":
            raise ValidationError(_("Only posted journal entries can be reversed."))

        if original.reversals.exists():
            raise ValidationError(_(f"Journal entry {original.entry_number} has already been reversed."))

        effective_date = reversal_date or original.transaction_date
        if isinstance(effective_date, str):
            from datetime import datetime
            effective_date = datetime.strptime(effective_date, "%Y-%m-%d").date()
        narration = f"Reversal of {original.entry_number}"
        if reason:
            narration += f" - Reason: {reason}"

        # Create new reversing entry in draft
        reversal = JournalEntry.objects.create(
            company=original.company,
            transaction_date=effective_date,
            reference=f"REV-{original.entry_number}",
            description=narration,
            explanation=f"Reversal of {original.entry_number}. Reason: {reason}" if reason else f"Reversal of {original.entry_number}",
            entry_type="reversal",
            source_module="reversal",
            source_id=original.id,
            reversal_of=original,
            created_by=user if (user and user.is_authenticated) else None,
            status="draft",
        )

        # Clone and invert all lines (Debit becomes Credit, Credit becomes Debit)
        for orig_line in original.lines.all():
            JournalEntryLine.objects.create(
                company=original.company,
                journal_entry=reversal,
                account=orig_line.account,
                line_number=orig_line.line_number,
                debit=orig_line.credit,   # Invert!
                credit=orig_line.debit,   # Invert!
                description=f"Reversal: {orig_line.description}" if orig_line.description else "Reversal line",
            )

        # Post the reversal entry
        posted_reversal = post_journal_entry(reversal.id, user, company=original.company)

        # Update original entry status to reversed with tracking metadata
        original.status = "reversed"
        original.reversed_at = timezone.now()
        original.reversed_by = user if (user and user.is_authenticated) else None
        original.reversal_reason = reason or ""
        original.save(update_fields=["status", "reversed_at", "reversed_by", "reversal_reason", "updated_at"])

        # Record audit log for original entry reversal
        record_journal_audit_log(
            original,
            action="REVERSED",
            user=user,
            details={
                "reversal_entry_id": posted_reversal.id,
                "reversal_entry_number": posted_reversal.entry_number,
                "reason": reason or "",
            }
        )

        return posted_reversal
