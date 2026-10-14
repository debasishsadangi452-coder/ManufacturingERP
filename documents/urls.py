from django.urls import path

from .views import DocumentRegisterView, DocumentTypesView, DocumentView

urlpatterns = [
    path("", DocumentTypesView.as_view(), name="document-types"),
    path("register/", DocumentRegisterView.as_view(), name="document-register"),
    path("<str:doc_type>/<int:pk>/", DocumentView.as_view(), name="document"),
]
