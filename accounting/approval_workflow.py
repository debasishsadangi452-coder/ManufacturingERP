"""
Blueprint Section #28: Approval Workflow Engine.
Authoritative domain service implementing:
- Configurable approval thresholds (Standard vs High-Value tiers).
- Dynamic routing to appropriate approvers (Finance Manager vs Accounting Admin).
- Segregation of duties / 4-eyes principle (requester cannot self-approve).
- Rejection workflows with mandatory audit reasons.
- Approval timestamps, actors, and immutable lifecycle history.
- Pre-posting validation controls blocking unapproved transactions.
- Emergency manual journal controls with enhanced high-priority audit logging.
- Cross-module approval queues (Journal Entries, Expenses, etc.).
"""

from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import Company, User
from .models import (
    ApprovalThreshold,
    ApprovalAuditLog,
    JournalEntry,
    JournalEntryAuditLog,
    Expense,
)
from .roles_permissions import resolve_accounting_role


# Default threshold configurations for companies
DEFAULT_THRESHOLDS = {
    "all": {
        "standard_threshold": Decimal("1000.00"),
        "high_value_threshold": Decimal("10000.00"),
        "approver_role": "finance_manager",
        "high_value_approver_role": "accounting_admin",
        "enforce_segregation_of_duties": True,
        "allow_emergency_override": True,
    },
    "journal_entry": {
        "standard_threshold": Decimal("1000.00"),
        "high_value_threshold": Decimal("10000.00"),
        "approver_role": "finance_manager",
        "high_value_approver_role": "accounting_admin",
        "enforce_segregation_of_duties": True,
        "allow_emergency_override": True,
    },
    "expense": {
        "standard_threshold": Decimal("500.00"),
        "high_value_threshold": Decimal("5000.00"),
        "approver_role": "finance_manager",
        "high_value_approver_role": "accounting_admin",
        "enforce_segregation_of_duties": True,
        "allow_emergency_override": True,
    },
}


def get_or_create_threshold_policy(company, module="all"):
    """
    Retrieves or initializes the active ApprovalThreshold policy for a company and module.
    """
    policy = ApprovalThreshold.objects.filter(company=company, module=module).first()
    if not policy:
        defaults = DEFAULT_THRESHOLDS.get(module, DEFAULT_THRESHOLDS["all"])
        policy = ApprovalThreshold.objects.create(
            company=company,
            module=module,
            standard_threshold=defaults["standard_threshold"],
            high_value_threshold=defaults["high_value_threshold"],
            approver_role=defaults["approver_role"],
            high_value_approver_role=defaults["high_value_approver_role"],
            enforce_segregation_of_duties=defaults["enforce_segregation_of_duties"],
            allow_emergency_override=defaults["allow_emergency_override"],
            is_active=True,
        )
    return policy


def evaluate_approval_requirement(company, module, amount, requester=None):
    """
    Evaluates whether a transaction requires approval based on configured thresholds.
    Returns:
        {
            "requires_approval": bool,
            "routing_tier": "auto" | "standard" | "high_value",
            "required_role": str,
            "standard_threshold": Decimal,
            "high_value_threshold": Decimal,
            "enforce_segregation": bool,
            "allow_emergency": bool,
        }
    """
    amount = Decimal(str(amount or "0.00"))
    policy = ApprovalThreshold.objects.filter(company=company, module=module, is_active=True).first()
    if not policy:
        policy = ApprovalThreshold.objects.filter(company=company, module="all", is_active=True).first()
    if not policy:
        policy = get_or_create_threshold_policy(company, module=module)

    if not policy.is_active:
        return {
            "requires_approval": False,
            "routing_tier": "auto",
            "required_role": "none",
            "standard_threshold": policy.standard_threshold,
            "high_value_threshold": policy.high_value_threshold,
            "enforce_segregation": policy.enforce_segregation_of_duties,
            "allow_emergency": policy.allow_emergency_override,
        }

    if amount >= policy.high_value_threshold:
        return {
            "requires_approval": True,
            "routing_tier": "high_value",
            "required_role": policy.high_value_approver_role,
            "standard_threshold": policy.standard_threshold,
            "high_value_threshold": policy.high_value_threshold,
            "enforce_segregation": policy.enforce_segregation_of_duties,
            "allow_emergency": policy.allow_emergency_override,
        }
    elif amount >= policy.standard_threshold:
        return {
            "requires_approval": True,
            "routing_tier": "standard",
            "required_role": policy.approver_role,
            "standard_threshold": policy.standard_threshold,
            "high_value_threshold": policy.high_value_threshold,
            "enforce_segregation": policy.enforce_segregation_of_duties,
            "allow_emergency": policy.allow_emergency_override,
        }
    else:
        return {
            "requires_approval": False,
            "routing_tier": "auto",
            "required_role": "none",
            "standard_threshold": policy.standard_threshold,
            "high_value_threshold": policy.high_value_threshold,
            "enforce_segregation": policy.enforce_segregation_of_duties,
            "allow_emergency": policy.allow_emergency_override,
        }


def validate_approver_eligibility(user, document_type, amount, requester=None, company=None):
    """
    Validates that the user is authorized to approve the document:
    1. Checks role authorization (standard vs high-value).
    2. Enforces segregation of duties (requester cannot self-approve).
    """
    if not user or not user.is_authenticated:
        raise ValidationError(_("Authentication required to approve transactions."))

    target_company = company or getattr(user, "company", None)
    if not target_company:
        raise ValidationError(_("User has no associated tenant company."))

    eval_result = evaluate_approval_requirement(target_company, document_type, amount, requester=requester)
    user_role = resolve_accounting_role(user)

    # 1. Segregation of duties check (Maker != Checker)
    if eval_result["enforce_segregation"] and requester and user == requester:
        raise ValidationError(
            _("Segregation of Duties violation: The creator/requester cannot approve their own transaction (4-eyes principle).")
        )

    # 2. High-value tier requires executive/admin role
    if eval_result["routing_tier"] == "high_value":
        if user_role not in ["accounting_admin", "admin"]:
            raise ValidationError(
                _(f"High-Value Approval Required: Amount ({amount}) exceeds high-value threshold ({eval_result['high_value_threshold']}). "
                  f"Requires {eval_result['required_role'].replace('_', ' ').title()} approval.")
            )

    # 3. Standard tier requires at least finance manager or admin
    elif eval_result["routing_tier"] == "standard":
        if user_role not in ["accounting_admin", "finance_manager", "admin"]:
            raise ValidationError(
                _(f"Approval Required: Amount ({amount}) exceeds threshold ({eval_result['standard_threshold']}). "
                  f"Requires {eval_result['required_role'].replace('_', ' ').title()} approval.")
            )

    return eval_result


def record_approval_audit(company, document_type, document_id, document_number, amount, action, actor,
                          actor_role=None, rejection_reason="", emergency_reason="", notes="", routing_tier="standard"):
    """
    Creates an immutable ApprovalAuditLog record capturing lifecycle actions and actors.
    """
    role = actor_role or (resolve_accounting_role(actor) if actor else "system")
    return ApprovalAuditLog.objects.create(
        company=company,
        document_type=document_type,
        document_id=str(document_id),
        document_number=str(document_number),
        amount=Decimal(str(amount or "0.00")),
        action=action,
        actor=actor if (actor and actor.is_authenticated) else None,
        actor_role=role,
        rejection_reason=rejection_reason or "",
        emergency_reason=emergency_reason or "",
        notes=notes or "",
        routing_tier=routing_tier or "standard",
    )


def submit_document_for_approval(company, document_type, document_id, user, notes=""):
    """
    Submits a draft or rejected document for approval workflow routing.
    """
    with transaction.atomic():
        if document_type == "journal_entry":
            entry = JournalEntry.objects.select_for_update().get(pk=document_id, company=company)
            if entry.status not in ["draft", "rejected"]:
                raise ValidationError(_(f"Cannot submit journal entry in status '{entry.status}'. Must be draft or rejected."))
            entry.validate_double_entry()
            eval_result = evaluate_approval_requirement(company, "journal_entry", entry.total_debit, requester=user)
            entry.status = "submitted"
            entry.submitted_at = timezone.now()
            entry.submitted_by = user if (user and user.is_authenticated) else None
            entry.save(update_fields=["status", "submitted_at", "submitted_by", "updated_at"])

            from .engine import record_journal_audit_log
            record_journal_audit_log(
                entry,
                action="SUBMITTED",
                user=user,
                details={
                    "submitted_at": entry.submitted_at.isoformat(),
                    "routing_tier": eval_result["routing_tier"],
                    "required_role": eval_result["required_role"],
                    "notes": notes,
                }
            )
            record_approval_audit(
                company=company,
                document_type="journal_entry",
                document_id=entry.id,
                document_number=entry.entry_number,
                amount=entry.total_debit,
                action="SUBMITTED",
                actor=user,
                notes=notes,
                routing_tier=eval_result["routing_tier"],
            )
            return {
                "document_type": "journal_entry",
                "id": entry.id,
                "number": entry.entry_number,
                "status": entry.status,
                "amount": float(entry.total_debit),
                "routing_tier": eval_result["routing_tier"],
                "required_role": eval_result["required_role"],
            }

        elif document_type == "expense":
            expense = Expense.objects.select_for_update().get(pk=document_id, company=company)
            if expense.approval_status not in ["draft", "rejected"]:
                raise ValidationError(_(f"Cannot submit expense in status '{expense.approval_status}'. Must be draft or rejected."))
            eval_result = evaluate_approval_requirement(company, "expense", expense.total_amount, requester=user)
            expense.approval_status = "submitted"
            expense.submitted_at = timezone.now()
            expense.submitted_by = user if (user and user.is_authenticated) else None
            expense.save(update_fields=["approval_status", "submitted_at", "submitted_by"])

            record_approval_audit(
                company=company,
                document_type="expense",
                document_id=expense.id,
                document_number=expense.expense_number,
                amount=expense.total_amount,
                action="SUBMITTED",
                actor=user,
                notes=notes,
                routing_tier=eval_result["routing_tier"],
            )
            return {
                "document_type": "expense",
                "id": expense.id,
                "number": expense.expense_number,
                "status": expense.approval_status,
                "amount": float(expense.total_amount),
                "routing_tier": eval_result["routing_tier"],
                "required_role": eval_result["required_role"],
            }

        else:
            raise ValidationError(_(f"Unsupported document type: {document_type}"))


def approve_document(company, document_type, document_id, user, notes=""):
    """
    Approves a submitted document after verifying approver eligibility,
    threshold routing, and segregation of duties.
    """
    with transaction.atomic():
        if document_type == "journal_entry":
            entry = JournalEntry.objects.select_for_update().get(pk=document_id, company=company)
            if entry.status != "submitted":
                raise ValidationError(_(f"Cannot approve journal entry in status '{entry.status}'. Must be submitted."))
            entry.validate_double_entry()

            # Enforce eligibility & segregation of duties
            eval_result = validate_approver_eligibility(
                user=user,
                document_type="journal_entry",
                amount=entry.total_debit,
                requester=entry.submitted_by or entry.created_by,
                company=company,
            )

            entry.status = "approved"
            entry.approved_at = timezone.now()
            entry.approved_by = user if (user and user.is_authenticated) else None
            entry.save(update_fields=["status", "approved_at", "approved_by", "updated_at"])

            from .engine import record_journal_audit_log
            record_journal_audit_log(
                entry,
                action="APPROVED",
                user=user,
                details={
                    "approved_at": entry.approved_at.isoformat(),
                    "routing_tier": eval_result["routing_tier"],
                    "notes": notes,
                }
            )
            record_approval_audit(
                company=company,
                document_type="journal_entry",
                document_id=entry.id,
                document_number=entry.entry_number,
                amount=entry.total_debit,
                action="APPROVED",
                actor=user,
                notes=notes,
                routing_tier=eval_result["routing_tier"],
            )
            return {
                "document_type": "journal_entry",
                "id": entry.id,
                "number": entry.entry_number,
                "status": entry.status,
                "amount": float(entry.total_debit),
                "approved_by": user.email if user else None,
                "approved_at": entry.approved_at.isoformat(),
            }

        elif document_type == "expense":
            expense = Expense.objects.select_for_update().get(pk=document_id, company=company)
            if expense.approval_status != "submitted":
                raise ValidationError(_(f"Cannot approve expense in status '{expense.approval_status}'. Must be submitted."))

            eval_result = validate_approver_eligibility(
                user=user,
                document_type="expense",
                amount=expense.total_amount,
                requester=expense.submitted_by or expense.created_by,
                company=company,
            )

            expense.approval_status = "approved"
            expense.approved_at = timezone.now()
            expense.approved_by = user if (user and user.is_authenticated) else None
            expense.accounting_status = "ready"
            expense.save(update_fields=["approval_status", "approved_at", "approved_by", "accounting_status"])

            record_approval_audit(
                company=company,
                document_type="expense",
                document_id=expense.id,
                document_number=expense.expense_number,
                amount=expense.total_amount,
                action="APPROVED",
                actor=user,
                notes=notes,
                routing_tier=eval_result["routing_tier"],
            )
            return {
                "document_type": "expense",
                "id": expense.id,
                "number": expense.expense_number,
                "status": expense.approval_status,
                "amount": float(expense.total_amount),
                "approved_by": user.email if user else None,
                "approved_at": expense.approved_at.isoformat(),
            }

        else:
            raise ValidationError(_(f"Unsupported document type: {document_type}"))


def reject_document(company, document_type, document_id, user, reason=""):
    """
    Rejects a submitted document with a mandatory rejection reason.
    """
    if not reason or len(str(reason).strip()) < 3:
        raise ValidationError(_("A documented rejection reason of at least 3 characters is required."))

    clean_reason = str(reason).strip()

    with transaction.atomic():
        if document_type == "journal_entry":
            entry = JournalEntry.objects.select_for_update().get(pk=document_id, company=company)
            if entry.status != "submitted":
                raise ValidationError(_(f"Cannot reject journal entry in status '{entry.status}'. Must be submitted."))

            user_role = resolve_accounting_role(user)
            if user_role not in ["accounting_admin", "finance_manager", "admin"]:
                raise ValidationError(_("Only Finance Managers or Accounting Admins can reject journal entries."))

            entry.status = "rejected"
            entry.rejected_at = timezone.now()
            entry.rejected_by = user if (user and user.is_authenticated) else None
            entry.rejection_reason = clean_reason
            entry.save(update_fields=["status", "rejected_at", "rejected_by", "rejection_reason", "updated_at"])

            from .engine import record_journal_audit_log
            record_journal_audit_log(
                entry,
                action="REJECTED",
                user=user,
                details={
                    "rejected_at": entry.rejected_at.isoformat(),
                    "reason": clean_reason,
                }
            )
            record_approval_audit(
                company=company,
                document_type="journal_entry",
                document_id=entry.id,
                document_number=entry.entry_number,
                amount=entry.total_debit,
                action="REJECTED",
                actor=user,
                rejection_reason=clean_reason,
            )
            return {
                "document_type": "journal_entry",
                "id": entry.id,
                "number": entry.entry_number,
                "status": entry.status,
                "rejection_reason": clean_reason,
            }

        elif document_type == "expense":
            expense = Expense.objects.select_for_update().get(pk=document_id, company=company)
            if expense.approval_status != "submitted":
                raise ValidationError(_(f"Cannot reject expense in status '{expense.approval_status}'. Must be submitted."))

            user_role = resolve_accounting_role(user)
            if user_role not in ["accounting_admin", "finance_manager", "admin"]:
                raise ValidationError(_("Only Finance Managers or Accounting Admins can reject expenses."))

            expense.approval_status = "rejected"
            expense.rejection_reason = clean_reason
            expense.accounting_status = "not_ready"
            expense.save(update_fields=["approval_status", "rejection_reason", "accounting_status"])

            record_approval_audit(
                company=company,
                document_type="expense",
                document_id=expense.id,
                document_number=expense.expense_number,
                amount=expense.total_amount,
                action="REJECTED",
                actor=user,
                rejection_reason=clean_reason,
            )
            return {
                "document_type": "expense",
                "id": expense.id,
                "number": expense.expense_number,
                "status": expense.approval_status,
                "rejection_reason": clean_reason,
            }

        else:
            raise ValidationError(_(f"Unsupported document type: {document_type}"))


def emergency_override_posting(company, document_type, document_id, user, emergency_reason=""):
    """
    Blueprint Section #28: Emergency manual posting control with heightened audit logging.
    Allows designated executive roles to bypass pending approvals during critical operations
    while requiring a documented justification and producing indelible audit logs.
    """
    if not emergency_reason or len(str(emergency_reason).strip()) < 10:
        raise ValidationError(_("Emergency override requires a documented business justification of at least 10 characters."))

    user_role = resolve_accounting_role(user)
    if user_role not in ["accounting_admin", "finance_manager", "admin"]:
        raise ValidationError(_("Emergency override is restricted to Finance Managers and Accounting Admins."))

    policy = ApprovalThreshold.objects.filter(company=company, module=document_type, is_active=True).first()
    if not policy:
        policy = ApprovalThreshold.objects.filter(company=company, module="all", is_active=True).first()
    if policy and not policy.allow_emergency_override:
        raise ValidationError(_("Emergency posting override is currently disabled in your company approval policy."))

    clean_reason = str(emergency_reason).strip()

    with transaction.atomic():
        if document_type == "journal_entry":
            from .engine import post_journal_entry
            posted_entry = post_journal_entry(
                entry_id=document_id,
                user=user,
                company=company,
                emergency_override=True,
                emergency_reason=clean_reason,
            )
            record_approval_audit(
                company=company,
                document_type="journal_entry",
                document_id=posted_entry.id,
                document_number=posted_entry.entry_number,
                amount=posted_entry.total_debit,
                action="EMERGENCY_OVERRIDE",
                actor=user,
                emergency_reason=clean_reason,
                routing_tier="emergency",
            )
            return {
                "document_type": "journal_entry",
                "id": posted_entry.id,
                "number": posted_entry.entry_number,
                "status": posted_entry.status,
                "is_emergency": True,
                "emergency_reason": clean_reason,
                "posted_at": posted_entry.posted_at.isoformat() if posted_entry.posted_at else None,
            }
        else:
            raise ValidationError(_(f"Emergency override currently supported for journal entries."))


def get_pending_approvals_queue(company, user=None, module=None):
    """
    Returns an aggregated list of transactions waiting for review/approval across ERP modules,
    including routing tiers, segregation warnings, and eligibility status for the requesting user.
    """
    user_role = resolve_accounting_role(user) if user else "none"
    queue = []

    # 1. Journal entries pending approval
    if not module or module in ["all", "journal_entry"]:
        je_qs = JournalEntry.objects.filter(company=company, status="submitted").select_related(
            "submitted_by", "created_by", "accounting_period"
        ).order_by("submitted_at", "created_at")

        for je in je_qs:
            amount = je.total_debit
            eval_res = evaluate_approval_requirement(company, "journal_entry", amount, requester=je.submitted_by)
            is_submitter = (user and (user == je.submitted_by or user == je.created_by))
            segregation_blocked = bool(eval_res["enforce_segregation"] and is_submitter)
            can_approve = False

            if not segregation_blocked:
                if eval_res["routing_tier"] == "high_value":
                    can_approve = user_role in ["accounting_admin", "admin"]
                else:
                    can_approve = user_role in ["accounting_admin", "finance_manager", "admin"]

            days_waiting = 0
            if je.submitted_at:
                days_waiting = (timezone.now() - je.submitted_at).days

            queue.append({
                "id": je.id,
                "document_type": "journal_entry",
                "document_number": je.entry_number,
                "title": je.description or f"Journal Entry {je.entry_number}",
                "amount": float(amount),
                "status": "submitted",
                "routing_tier": eval_res["routing_tier"],
                "required_role": eval_res["required_role"],
                "requester": je.submitted_by.email if je.submitted_by else (je.created_by.email if je.created_by else "System"),
                "submitted_at": je.submitted_at.isoformat() if je.submitted_at else je.created_at.isoformat(),
                "days_waiting": days_waiting,
                "can_approve": can_approve,
                "segregation_blocked": segregation_blocked,
                "explanation": je.explanation or "",
            })

    # 2. Expenses pending approval
    if not module or module in ["all", "expense"]:
        exp_qs = Expense.objects.filter(company=company, approval_status="submitted").select_related(
            "submitted_by", "created_by", "category"
        ).order_by("submitted_at", "expense_date")

        for exp in exp_qs:
            amount = exp.total_amount
            eval_res = evaluate_approval_requirement(company, "expense", amount, requester=exp.submitted_by)
            is_submitter = (user and (user == exp.submitted_by or user == exp.created_by))
            segregation_blocked = bool(eval_res["enforce_segregation"] and is_submitter)
            can_approve = False

            if not segregation_blocked:
                if eval_res["routing_tier"] == "high_value":
                    can_approve = user_role in ["accounting_admin", "admin"]
                else:
                    can_approve = user_role in ["accounting_admin", "finance_manager", "admin"]

            days_waiting = 0
            if exp.submitted_at:
                days_waiting = (timezone.now() - exp.submitted_at).days

            queue.append({
                "id": exp.id,
                "document_type": "expense",
                "document_number": exp.expense_number,
                "title": exp.title or f"Expense {exp.expense_number}",
                "amount": float(amount),
                "status": "submitted",
                "routing_tier": eval_res["routing_tier"],
                "required_role": eval_res["required_role"],
                "requester": exp.submitted_by.email if exp.submitted_by else (exp.created_by.email if exp.created_by else "System"),
                "submitted_at": exp.submitted_at.isoformat() if exp.submitted_at else None,
                "days_waiting": days_waiting,
                "can_approve": can_approve,
                "segregation_blocked": segregation_blocked,
                "category": exp.category.name if exp.category else "",
            })

    return queue


def get_approval_summary_metrics(company):
    """
    Computes high-level KPI metrics for the approval workflow dashboard.
    """
    now = timezone.now()
    thirty_days_ago = now - timezone.timedelta(days=30)

    # Thresholds
    thresholds = list(ApprovalThreshold.objects.filter(company=company, is_active=True).values(
        "module", "standard_threshold", "high_value_threshold",
        "approver_role", "high_value_approver_role", "enforce_segregation_of_duties",
        "allow_emergency_override"
    ))

    # Pending items
    pending_queue = get_pending_approvals_queue(company)
    total_pending_count = len(pending_queue)
    total_pending_amount = sum(item["amount"] for item in pending_queue)
    high_value_count = sum(1 for item in pending_queue if item["routing_tier"] == "high_value")

    # Historical stats from ApprovalAuditLog
    audit_qs = ApprovalAuditLog.objects.filter(company=company, created_at__gte=thirty_days_ago)
    approved_count = audit_qs.filter(action="APPROVED").count()
    rejected_count = audit_qs.filter(action="REJECTED").count()
    emergency_count = audit_qs.filter(action="EMERGENCY_OVERRIDE").count()

    return {
        "pending_count": total_pending_count,
        "pending_amount": total_pending_amount,
        "high_value_count": high_value_count,
        "approved_last_30d": approved_count,
        "rejected_last_30d": rejected_count,
        "emergency_overrides_last_30d": emergency_count,
        "thresholds": thresholds,
    }


def get_approval_audit_history(company, document_type=None, document_id=None, limit=50):
    """
    Retrieves chronological approval audit history records with actors and details.
    """
    qs = ApprovalAuditLog.objects.filter(company=company).select_related("actor")
    if document_type:
        qs = qs.filter(document_type=document_type)
    if document_id:
        qs = qs.filter(document_id=str(document_id))

    records = []
    for log in qs[:limit]:
        records.append({
            "id": log.id,
            "document_type": log.document_type,
            "document_id": log.document_id,
            "document_number": log.document_number,
            "amount": float(log.amount),
            "action": log.action,
            "actor": log.actor.email if log.actor else "System",
            "actor_role": log.actor_role,
            "rejection_reason": log.rejection_reason,
            "emergency_reason": log.emergency_reason,
            "notes": log.notes,
            "routing_tier": log.routing_tier,
            "timestamp": log.created_at.isoformat(),
        })
    return records
