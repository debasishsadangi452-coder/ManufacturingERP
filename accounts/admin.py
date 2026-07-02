from django.contrib import admin
from .models import User, Company, CompanySubscription


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "email", "role", "company", "is_staff", "is_active")
    list_filter = ("role", "company", "is_staff", "is_active")
    search_fields = ("username", "email")


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "created_at")
    search_fields = ("name", "slug")


@admin.register(CompanySubscription)
class CompanySubscriptionAdmin(admin.ModelAdmin):
    list_display = ("company", "plan", "status", "user_limit", "onboarding_completed", "current_period_end")
