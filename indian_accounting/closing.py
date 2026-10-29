"""Period locking and financial-year closing / reopening (all actions audited)."""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .engine import audit
from .models import AccountingPeriod, FinancialYear, PostingEvent, Voucher


def _user(u):
    return u if getattr(u, "is_authenticated", False) else None


@transaction.atomic
def set_period_lock(company, user, period_id, lock, reason=""):
    p = AccountingPeriod.objects.select_for_update().select_related("financial_year").filter(
        company=company, pk=period_id).first()
    if p is None:
        raise ValidationError("Period not found.")
    if p.financial_year.status == "closed":
        raise ValidationError("The financial year is closed; reopen it before changing its periods.")
    if not lock and not reason:
        raise ValidationError("A reason is required to unlock a period.")
    new = "locked" if lock else "open"
    if p.status == new:
        return p
    before = p.status
    p.status, p.locked_at, p.locked_by = new, (timezone.now() if lock else None), (_user(user) if lock else None)
    p.save()
    audit(company, user, "period", p.pk, "lock" if lock else "unlock", before={"status": before},
          after={"status": new, "start": str(p.start_date)}, reason=reason)
    return p


def close_checks(company, fy):
    """Blocking problems that must be fixed before a year can be closed."""
    from .reports import trial_balance
    problems = []
    pending = Voucher.objects.filter(company=company, financial_year=fy, status__in=["draft", "submitted", "approved"]).count()
    if pending:
        problems.append(f"{pending} voucher(s) are still draft/submitted/approved. Post or cancel them first.")
    failed = PostingEvent.objects.filter(company=company, status="failed").count()
    if failed:
        problems.append(f"{failed} auto-posting event(s) have failed. Resolve or retry them so nothing is missing from the books.")
    tb = trial_balance(company, fy.start_date, fy.end_date)
    if not tb["is_balanced"]:
        problems.append(f"Trial balance is out of balance by {tb['difference']}.")
    return problems


@transaction.atomic
def close_year(company, user, fy_id):
    from .seeds import create_financial_year
    fy = FinancialYear.objects.select_for_update().filter(company=company, pk=fy_id).first()
    if fy is None:
        raise ValidationError("Financial year not found.")
    if fy.status == "closed":
        raise ValidationError("Financial year is already closed.")
    if FinancialYear.objects.filter(company=company, status="open", end_date__lt=fy.start_date).exists():
        raise ValidationError("Close earlier financial years first.")
    problems = close_checks(company, fy)
    if problems:
        raise ValidationError(problems)
    now = timezone.now()
    AccountingPeriod.objects.filter(financial_year=fy).update(status="locked", locked_at=now, locked_by=_user(user))
    fy.status, fy.closed_at, fy.closed_by = "closed", now, _user(user)
    fy.save()
    # Balance-sheet ledgers carry forward automatically (balances are cumulative); profit moves to accumulated profit.
    nxt = create_financial_year(company, fy.end_date + timedelta(days=1),
                                getattr(getattr(company, "indian_profile", None), "fy_start_month", 4))
    audit(company, user, "financial_year", fy.pk, "close", after={"name": fy.name, "next": nxt.name})
    return fy


@transaction.atomic
def reopen_year(company, user, fy_id, reason):
    if not reason:
        raise ValidationError("A reason is required to reopen a financial year.")
    fy = FinancialYear.objects.select_for_update().filter(company=company, pk=fy_id).first()
    if fy is None:
        raise ValidationError("Financial year not found.")
    if fy.status != "closed":
        raise ValidationError("Financial year is not closed.")
    if FinancialYear.objects.filter(company=company, status="closed", start_date__gt=fy.start_date).exists():
        raise ValidationError("A later financial year is closed; reopen that one first.")
    fy.status, fy.closed_at, fy.closed_by = "open", None, None
    fy.save()  # periods stay locked until each is unlocked deliberately
    audit(company, user, "financial_year", fy.pk, "reopen", reason=reason)
    return fy
