from django.urls import path
from .views import RegisterView, ProfileView, UsersListView, UserUpdateView

urlpatterns = [
    path("register/", RegisterView.as_view()),
    path("profile/", ProfileView.as_view()),
    path("users/", UsersListView.as_view()),
    path("users/<int:pk>/", UserUpdateView.as_view()),
]
