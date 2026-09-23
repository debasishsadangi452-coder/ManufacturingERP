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


def post_journal_entry(entry_id, user, company=None):
    """
    Atomically posts a draft JournalEntry to the General Ledger.
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
        if entry.status != "draft":
            raise ValidationError(_(f"Invalid entry status '{entry.status}'. Only draft entries can be posted."))

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
        entry.posted_by = user
        entry.save()

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
        narration = f"Reversal of {original.entry_number}"
        if reason:
            narration += f" - Reason: {reason}"

        # Create new reversing entry in draft
        reversal = JournalEntry.objects.create(
            company=original.company,
            transaction_date=effective_date,
            reference=f"REV-{original.entry_number}",
            description=narration,
            source_module="reversal",
            source_id=original.id,
            reversal_of=original,
            created_by=user,
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

        # Update original entry status to reversed
        original.status = "reversed"
        original.save(update_fields=["status", "updated_at"])

        return posted_reversal
