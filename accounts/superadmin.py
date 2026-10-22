"""Platform super admin: a fixed account above every company.

The super admin has no company and no tenant role, so none of the company
screens or APIs open to it. It can list every user on the platform, block
(deactivate) and unblock them, and delete them. A blocked user is signed out at
once (JWT auth rejects inactive users) and cannot log in until unblocked.

The login is configured through the environment — SUPERADMIN_EMAIL and
SUPERADMIN_PASSWORD — so no credential lives in source control. Without both
set, no super admin is created.
"""
import os

from django.db import transaction
from django.db.models import ProtectedError, Q
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.utils import log_activity
from .models import Company, User

SUPERADMIN_ROLE = "superadmin"


def ensure_superadmin(reset_password=False):
    """Create the super admin from SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD.

    Returns (user, created), or (None, False) when the variables are not set.
    The password is only applied on creation or when reset_password is set, so
    a password changed later is never silently overwritten on deploy.
    """
    email = os.getenv("SUPERADMIN_EMAIL", "").strip()
    password = os.getenv("SUPERADMIN_PASSWORD", "")
    if not email or not password:
        return None, False
    user = User.objects.filter(username=email).first()
    created = user is None
    if created:
        user = User(username=email)
    user.email = email
    user.role = SUPERADMIN_ROLE
    user.company = None
    user.is_superuser = True
    user.is_staff = True
    user.is_active = True
    user.first_name = user.first_name or "Super"
    user.last_name = user.last_name or "Admin"
    if created or reset_password:
        user.set_password(password)
    user.save()
    return user, created


class IsSuperAdmin(BasePermission):
    def has_permission(self, request, view):
        u = request.user
        return bool(u and u.is_authenticated and u.is_superuser and u.role == SUPERADMIN_ROLE)


def _row(u):
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "name": f"{u.first_name} {u.last_name}".strip(),
        "role": u.role,
        "company_id": u.company_id,
        "company_name": u.company.name if u.company_id else None,
        "is_active": u.is_active,
        "is_protected": u.is_superuser,
        "last_login": u.last_login,
        "date_joined": u.date_joined,
    }


class SuperAdminUsersView(APIView):
    """GET /api/auth/superadmin/users/?q=&company=&role=&status=active|blocked"""
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        qs = User.objects.select_related("company").order_by("company__name", "role", "username")
        p = request.query_params
        if p.get("q"):
            q = p["q"].strip()
            qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(first_name__icontains=q)
                           | Q(last_name__icontains=q) | Q(company__name__icontains=q))
        if p.get("company"):
            qs = qs.filter(company_id=p["company"])
        if p.get("role"):
            qs = qs.filter(role=p["role"])
        if p.get("status") == "active":
            qs = qs.filter(is_active=True)
        elif p.get("status") == "blocked":
            qs = qs.filter(is_active=False)
        everyone = User.objects.all()
        return Response({
            "summary": {
                "users": everyone.count(),
                "active": everyone.filter(is_active=True).count(),
                "blocked": everyone.filter(is_active=False).count(),
                "companies": Company.objects.count(),
            },
            "companies": list(Company.objects.order_by("name").values("id", "name")),
            "users": [_row(u) for u in qs[:1000]],
        })


class _UserAction(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def target(self, request, pk):
        user = User.objects.select_related("company").filter(pk=pk).first()
        if user is None:
            return None, Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
        if user.is_superuser or user.pk == request.user.pk:
            return None, Response({"error": "The super admin account cannot be changed here."},
                                  status=status.HTTP_400_BAD_REQUEST)
        return user, None


class SuperAdminBlockView(_UserAction):
    """POST .../users/<id>/block/ and .../users/<id>/unblock/."""
    block = True

    def post(self, request, pk):
        user, error = self.target(request, pk)
        if error:
            return error
        user.is_active = not self.block
        user.save(update_fields=["is_active"])
        log_activity(request.user, "Super Admin", "Block User" if self.block else "Unblock User", user.username)
        return Response(_row(user))


class SuperAdminUnblockView(SuperAdminBlockView):
    block = False


class SuperAdminUserDeleteView(_UserAction):
    """DELETE .../users/<id>/ — permanent. The company itself is left in place."""

    def delete(self, request, pk):
        user, error = self.target(request, pk)
        if error:
            return error
        username = user.username
        try:
            with transaction.atomic():
                user.delete()
        except ProtectedError:
            return Response(
                {"error": f"{username} has records that must be kept. Block the user instead."},
                status=status.HTTP_409_CONFLICT,
            )
        log_activity(request.user, "Super Admin", "Delete User", username)
        return Response(status=status.HTTP_204_NO_CONTENT)
