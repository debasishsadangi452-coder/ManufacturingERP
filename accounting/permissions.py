"""
Blueprint Section #27: DRF Permission Classes for Accounting.
Extends the base ERP role-based access control with granular
accounting permissions for the 7 Blueprint personas.
"""

from rest_framework.permissions import BasePermission, SAFE_METHODS
from accounts.permission import IsFinanceOrAdmin, IsAdmin
from .roles_permissions import (
    resolve_accounting_role,
    can_perform_action,
    evaluate_permission_pipeline,
)


class IsAccountingAdmin(BasePermission):
    """Allows access only to Accounting Admins."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return resolve_accounting_role(request.user) == "accounting_admin"


class IsFinanceManager(BasePermission):
    """Allows access to Finance Managers and Accounting Admins."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return resolve_accounting_role(request.user) in ["accounting_admin", "finance_manager"]


class IsAccountant(BasePermission):
    """Allows access to operational Accountants, Finance Managers, and Admins."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return resolve_accounting_role(request.user) in [
            "accounting_admin",
            "finance_manager",
            "accountant",
        ]


class IsARUser(BasePermission):
    """Allows access to Sales/AR users and finance staff."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return resolve_accounting_role(request.user) in [
            "accounting_admin",
            "finance_manager",
            "accountant",
            "ar_user",
        ]


class IsAPUser(BasePermission):
    """Allows access to Procurement/AP users and finance staff."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return resolve_accounting_role(request.user) in [
            "accounting_admin",
            "finance_manager",
            "accountant",
            "ap_user",
        ]


class IsAuditorOrReadOnly(BasePermission):
    """Allows read-only safe methods to auditors, full access to finance/admin."""
    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        role = resolve_accounting_role(request.user)
        if role in ["accounting_admin", "finance_manager", "accountant"]:
            return True
        if role == "auditor" and request.method in SAFE_METHODS:
            return True
        return False


class HasActionPermission(BasePermission):
    """
    Dynamic permission class evaluating the 6-stage permission pipeline
    (User -> Role -> Permission -> Action -> Approval Rule -> Post/Reject).
    """
    def __init__(self, required_action: str):
        self.required_action = required_action

    def __call__(self):
        return self

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        return can_perform_action(request.user, self.required_action)
