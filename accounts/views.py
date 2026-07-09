from datetime import timedelta

from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from .models import User, CompanySubscription
from .plans import PLAN_CONFIG, PLAN_ORDER
from .serializers import (
    UserSerializer,
    RegisterUserSerializer,
    CompanyRegisterSerializer,
    CompanySubscriptionSerializer,
    EmailOrUsernameTokenObtainPairSerializer,
)
from rest_framework.permissions import AllowAny, IsAuthenticated
from accounts.permission import IsAdmin, IsFinanceOrAdmin
from rest_framework.views import APIView
from rest_framework.response import Response


class EmailOrUsernameTokenObtainPairView(TokenObtainPairView):
    """JWT login that accepts a username or an email address."""
    serializer_class = EmailOrUsernameTokenObtainPairSerializer


class CompanyRegisterView(APIView):
    """Public sign-up: register a company and its (single) admin account.
    Returns JWT tokens so the new admin lands directly in onboarding."""
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CompanyRegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        admin = serializer.save()
        refresh = RefreshToken.for_user(admin)
        return Response({
            "username": admin.username,
            "email": admin.email,
            "company": admin.company.name,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }, status=status.HTTP_201_CREATED)


class RegisterView(generics.CreateAPIView):
    """Admin-only: register a team member for the admin's company.
    The username is generated as firstname.role@companyslug."""
    queryset = User.objects.all()
    serializer_class = RegisterUserSerializer
    permission_classes = [IsAdmin]


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)


class UsersListView(generics.ListAPIView):
    """Admin/finance: list the users of the caller's company."""
    serializer_class = UserSerializer
    permission_classes = [IsFinanceOrAdmin]

    def get_queryset(self):
        return User.objects.filter(company=self.request.user.company).order_by('username')

class UserUpdateView(generics.UpdateAPIView):
    """Admin/finance: update user details (like auto_approve_limit) within their company."""
    serializer_class = UserSerializer
    permission_classes = [IsFinanceOrAdmin]

    def get_queryset(self):
        return User.objects.filter(company=self.request.user.company)


class SubscriptionPlansView(APIView):
    """Catalog of the 4 commercial plans (source of truth: accounts/plans.py)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        plans = []
        for key in PLAN_ORDER:
            cfg = PLAN_CONFIG[key]
            plans.append({
                "id": key,
                "name": cfg["name"],
                "tagline": cfg["tagline"],
                "user_limit": cfg["user_limit"],
                "warehouse_limit": cfg["warehouse_limit"],
                "production_line_limit": cfg["production_line_limit"],
                "ai_monthly_message_limit": cfg["ai_monthly_message_limit"],
                "features": cfg["features"],
                "excluded": cfg["excluded"],
            })
        return Response({"plans": plans})


class SubscriptionStatusView(APIView):
    """The caller's company subscription plus live usage counts.
    `configured: false` means the onboarding wizard should run."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        company = request.user.company
        users = User.objects.filter(company=company) if company else User.objects.none()
        usage = {
            "users": users.count(),
            "admin_exists": users.filter(role="admin").exists(),
        }
        subscription = CompanySubscription.for_company(company)
        if subscription is None:
            return Response({"configured": False, "subscription": None, "usage": usage})
        return Response({
            "configured": True,
            "subscription": CompanySubscriptionSerializer(subscription).data,
            "usage": usage,
        })


class SelectPlanView(APIView):
    """Admin-only: select (or change) their company's subscription plan.
    Limits are copied from the plan capability map."""
    permission_classes = [IsAdmin]

    def post(self, request):
        company = request.user.company
        if company is None:
            return Response(
                {"detail": "Your account is not linked to a company."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        plan = request.data.get("plan")
        if plan not in PLAN_CONFIG:
            return Response(
                {"detail": f"Invalid plan. Choose one of: {', '.join(PLAN_ORDER)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cfg = PLAN_CONFIG[plan]

        # Block downgrades below the current user count.
        user_count = User.objects.filter(company=company).count()
        if cfg["user_limit"] is not None and user_count > cfg["user_limit"]:
            return Response(
                {"detail": (
                    f"Cannot select {cfg['name']}: you have {user_count} users but the plan "
                    f"allows only {cfg['user_limit']}. Remove users or pick a higher plan."
                )},
                status=status.HTTP_400_BAD_REQUEST,
            )

        now = timezone.now()
        subscription = CompanySubscription.for_company(company) or CompanySubscription(company=company, plan=plan)
        subscription.plan = plan
        subscription.status = "active"
        subscription.user_limit = cfg["user_limit"]
        subscription.warehouse_limit = cfg["warehouse_limit"]
        subscription.production_line_limit = cfg["production_line_limit"]
        subscription.ai_monthly_message_limit = cfg["ai_monthly_message_limit"]
        subscription.current_period_start = now
        subscription.current_period_end = now + timedelta(days=30)
        subscription.save()

        return Response({
            "configured": True,
            "subscription": CompanySubscriptionSerializer(subscription).data,
        })


class CompleteOnboardingView(APIView):
    """Admin-only: mark the plan-selection + member-registration wizard as finished."""
    permission_classes = [IsAdmin]

    def post(self, request):
        subscription = CompanySubscription.for_company(request.user.company)
        if subscription is None:
            return Response(
                {"detail": "Select a plan before completing onboarding."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        subscription.onboarding_completed = True
        subscription.save()
        return Response({"configured": True, "onboarding_completed": True})


# ── Company data import (Excel) at registration ──────────────────────────────
from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from accounts.permission import IsAdmin


class ImportTemplateView(APIView):
    """Download the 5-sheet .xlsx template a company fills in at registration."""
    permission_classes = [AllowAny]

    def get(self, request):
        from .company_import import build_template
        data = build_template()
        resp = HttpResponse(
            data,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        resp["Content-Disposition"] = 'attachment; filename="company_data_template.xlsx"'
        return resp


class CompanyDataImportView(APIView):
    """Upload the filled workbook. Validates everything; on any error returns
    the list so the user can fix and re-upload. On success, imports and returns
    a summary of what was created."""
    permission_classes = [IsAuthenticated, IsAdmin]

    def post(self, request):
        f = request.FILES.get("file")
        if not f:
            return Response({"ok": False, "errors": ["No file uploaded."]},
                            status=status.HTTP_400_BAD_REQUEST)
        if not f.name.lower().endswith((".xlsx", ".xlsm")):
            return Response({"ok": False, "errors": ["Please upload an .xlsx file."]},
                            status=status.HTTP_400_BAD_REQUEST)
        company = request.user.company
        if company is None:
            return Response({"ok": False, "errors": ["Your account is not linked to a company."]},
                            status=status.HTTP_400_BAD_REQUEST)

        from .company_import import import_workbook
        ok, result = import_workbook(f, company, request.user)
        if not ok:
            return Response({"ok": False, "errors": result["errors"]},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True, "summary": result["summary"]}, status=status.HTTP_200_OK)
