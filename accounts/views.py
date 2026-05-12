from rest_framework import generics, permissions
from .models import User
from .serializers import UserSerializer
from rest_framework.permissions import AllowAny, IsAuthenticated
from accounts.permission import IsAdmin, IsFinanceOrAdmin
from rest_framework.views import APIView
from rest_framework.response import Response


class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAdmin]


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = UserSerializer(request.user)
        return Response(serializer.data)


class UsersListView(generics.ListAPIView):
    """Admin-only: list all users (for payroll, budget assignment, etc.)"""
    queryset = User.objects.all().order_by('username')
    serializer_class = UserSerializer
    permission_classes = [IsFinanceOrAdmin]

class UserUpdateView(generics.UpdateAPIView):
    """Admin-only: update user details (like auto_approve_limit)"""
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsFinanceOrAdmin]
