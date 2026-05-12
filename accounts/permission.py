from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "admin"


class IsHR(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "hr"


class IsStore(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "store"


class IsProduction(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "production"


class IsQuality(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "quality"


class IsSales(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "sales"


class IsFinance(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role == "finance"
class IsFinanceOrAdmin(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in ["admin", "finance"]
class IsMaintenanceAllowed(BasePermission):
    def has_permission(self, request, view):
        return request.user.is_authenticated and request.user.role in ["admin", "production", "quality"]
