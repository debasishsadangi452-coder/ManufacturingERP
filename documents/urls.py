from django.urls import path

from .views import DocumentTypesView, DocumentView

urlpatterns = [
    path("", DocumentTypesView.as_view(), name="document-types"),
    path("<str:doc_type>/<int:pk>/", DocumentView.as_view(), name="document"),
]
