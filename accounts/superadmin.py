"""Platform super admin: a fixed account above every company.

The super admin has no company and no tenant role, so none of the company
screens or APIs open to it. It can list every user on the platform, block
(deactivate) and unblock them, and delete them. A blocked user is signed out at
once (JWT auth rejects inactive users) and cannot log in until unblocked.

The login is configured through the environment — SUPERADMIN_EMAIL and
SUPERADMIN_PASSWORD — so no credential lives in source control. Without both
set, no super admin is created.

Visibility: only companies flagged `superadmin_visible` (the Smarties Foods demo
company, plus any company created from here) are shown; users, login activity and
actions for every other company are hidden until that is decided otherwise.
"""
import logging
import os
import threading
from datetime import timedelta

from django.db import IntegrityError, connections, transaction
from django.db.models import OuterRef, ProtectedError, Q, Subquery
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.utils import log_activity
from .models import Company, LoginEvent, User

logger = logging.getLogger(__name__)

SUPERADMIN_ROLE = "superadmin"
DEFAULT_DEMO_PASSWORD = "SmartiesDemo@2026"
# A company still "creating" after this long lost its worker; it is reported as failed.
CREATE_TIMEOUT = timedelta(minutes=10)


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


def visible_companies():
    return Company.objects.filter(superadmin_visible=True)


def visible_users():
    """Users of visible companies, plus the super admin account itself."""
    return User.objects.filter(
        Q(company__superadmin_visible=True) | Q(is_superuser=True, role=SUPERADMIN_ROLE)
    )


def _with_last_login_ip(qs):
    last_ok = LoginEvent.objects.filter(user=OuterRef("pk"), success=True)
    return qs.annotate(last_login_ip=Subquery(last_ok.values("ip_address")[:1]))


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
        "last_login_ip": getattr(u, "last_login_ip", None),
        "date_joined": u.date_joined,
    }


class SuperAdminUsersView(APIView):
    """GET /api/auth/superadmin/users/?q=&company=&role=&status=active|blocked"""
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        qs = _with_last_login_ip(visible_users().select_related("company")).order_by("company__name", "role", "username")
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
        everyone = visible_users()
        return Response({
            "summary": {
                "users": everyone.count(),
                "active": everyone.filter(is_active=True).count(),
                "blocked": everyone.filter(is_active=False).count(),
                "companies": visible_companies().count(),
            },
            "companies": list(visible_companies().order_by("name").values("id", "name")),
            "users": [_row(u) for u in qs[:1000]],
        })


class _UserAction(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def target(self, request, pk):
        user = visible_users().select_related("company").filter(pk=pk).first()
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


class SuperAdminLoginsView(APIView):
    """GET /api/auth/superadmin/logins/?q=&result=success|failed&limit=

    Recent sign-in attempts, newest first, with the IP address each came from.
    Only attempts for visible users are returned."""
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        p = request.query_params
        qs = LoginEvent.objects.filter(user__in=visible_users()).select_related("user", "user__company")
        if p.get("q"):
            q = p["q"].strip()
            qs = qs.filter(Q(username__icontains=q) | Q(ip_address__icontains=q) | Q(user__company__name__icontains=q))
        if p.get("result") == "success":
            qs = qs.filter(success=True)
        elif p.get("result") == "failed":
            qs = qs.filter(success=False)
        try:
            limit = max(1, min(int(p.get("limit", 200)), 1000))
        except ValueError:
            limit = 200
        return Response({"events": [{
            "id": e.id,
            "username": e.username,
            "name": f"{e.user.first_name} {e.user.last_name}".strip() if e.user_id else "",
            "company_name": e.user.company.name if e.user_id and e.user.company_id else None,
            "success": e.success,
            "ip_address": e.ip_address,
            "forwarded_for": e.forwarded_for,
            "user_agent": e.user_agent,
            "created_at": e.created_at,
        } for e in qs[:limit]]})


# --- Company creation (clone of the Smarties Foods demo company) -----------------

DEMO_ROLES = ("admin", "production", "quality", "store")


def _company_row(c):
    status_now, error = c.demo_status, c.demo_error
    if status_now == "creating" and timezone.now() - c.created_at > CREATE_TIMEOUT:
        status_now, error = "failed", "Timed out while creating the demo data. Create it again."
        Company.objects.filter(pk=c.pk, demo_status="creating").update(demo_status="failed", demo_error=error)
    return {
        "id": c.id,
        "name": c.name,
        "slug": c.slug,
        "demo_status": status_now,
        "demo_error": error,
        "created_at": c.created_at,
        "users": c.users.count(),
        "logins": [f"{role}@{c.slug}" for role in DEMO_ROLES] if status_now == "ready" else [],
    }


def _build_demo_company(company_id, name, slug, password):
    """Fill a freshly created company with the Smarties Foods demo data set.
    Runs off the request thread: seeding over a remote database can outlast a request."""
    try:
        import seed_smarties_foods  # project-root script; also usable from the shell
        seed_smarties_foods.seed(name, slug, password)
        Company.objects.filter(pk=company_id).update(demo_status="ready", demo_error="")
    except Exception as exc:
        logger.exception("Creating demo company %s failed", name)
        Company.objects.filter(pk=company_id).update(demo_status="failed", demo_error=str(exc)[:1000])


def _spawn(target, *args):
    def run():
        try:
            target(*args)
        finally:
            connections.close_all()  # this thread's own database connection

    threading.Thread(target=run, daemon=True).start()


class SuperAdminCompaniesView(APIView):
    """GET lists visible companies. POST {name, password} creates a new company with
    the same dummy data as Smarties Foods (separate rows, own logins) in the background;
    poll GET until its demo_status is "ready"."""
    permission_classes = [IsAuthenticated, IsSuperAdmin]

    def get(self, request):
        return Response({"companies": [_company_row(c) for c in visible_companies().order_by("-created_at")]})

    def post(self, request):
        name = " ".join(str(request.data.get("name", "")).split())
        password = request.data.get("password") or DEFAULT_DEMO_PASSWORD
        slug = slugify(name).replace("-", "")
        if len(name) < 2 or len(name) > 100 or not slug:
            return Response({"error": "Enter a company name (2-100 characters, with letters or numbers)."},
                            status=status.HTTP_400_BAD_REQUEST)
        if len(password) < 8:
            return Response({"error": "Password must be at least 8 characters."}, status=status.HTTP_400_BAD_REQUEST)

        existing = Company.objects.filter(Q(name__iexact=name) | Q(slug=slug)).first()
        if existing is not None:
            if existing.demo_status == "failed":
                existing.delete()  # a half-made shell from an earlier failed attempt
            else:
                return Response({"error": "A company with this name already exists."}, status=status.HTTP_409_CONFLICT)
        try:
            company = Company.objects.create(name=name, slug=slug, superadmin_visible=True, demo_status="creating")
        except IntegrityError:
            return Response({"error": "A company with this name already exists."}, status=status.HTTP_409_CONFLICT)

        log_activity(request.user, "Super Admin", "Create Company", name)
        _spawn(_build_demo_company, company.id, name, slug, password)
        body = _company_row(company)
        body["credentials"] = {
            "logins": [f"{role}@{slug}" for role in DEMO_ROLES],
            "password": password,
        }
        return Response(body, status=status.HTTP_202_ACCEPTED)
