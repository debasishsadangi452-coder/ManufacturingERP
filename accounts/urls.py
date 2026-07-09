from django.urls import path
from .views import (
    RegisterView,
    CompanyRegisterView,
    ProfileView,
    UsersListView,
    UserUpdateView,
    SubscriptionPlansView,
    SubscriptionStatusView,
    SelectPlanView,
    CompleteOnboardingView,
    ImportTemplateView,
    CompanyDataImportView,
)

urlpatterns = [
    path("register/", RegisterView.as_view()),
    path("company/register/", CompanyRegisterView.as_view()),
    path("company/import-template/", ImportTemplateView.as_view()),
    path("company/import-data/", CompanyDataImportView.as_view()),
    path("profile/", ProfileView.as_view()),
    path("users/", UsersListView.as_view()),
    path("users/<int:pk>/", UserUpdateView.as_view()),
    path("subscription/", SubscriptionStatusView.as_view()),
    path("subscription/plans/", SubscriptionPlansView.as_view()),
    path("subscription/select/", SelectPlanView.as_view()),
    path("subscription/complete-onboarding/", CompleteOnboardingView.as_view()),
]
