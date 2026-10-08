from django.http import HttpResponse
from django.template.loader import render_to_string
from django.utils import timezone
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .builders import DOCUMENTS, get_object


class DocumentTypesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response([{"type": key, "label": value[0]} for key, value in DOCUMENTS.items()])


class DocumentView(APIView):
    """GET /api/documents/<type>/<id>/ → printable HTML (print / save as PDF in the browser)."""
    permission_classes = [IsAuthenticated]

    def get(self, request, doc_type, pk):
        if doc_type not in DOCUMENTS:
            return Response({"error": f"Unknown document type '{doc_type}'."}, status=404)
        obj = get_object(doc_type, pk, request.user.company)
        if obj is None:
            return Response({"error": "Document source not found."}, status=404)
        try:
            doc = DOCUMENTS[doc_type][3](obj)
        except (ValueError, DjangoValidationError) as e:
            return Response({"error": str(e)}, status=400)
        html = render_to_string("documents/document.html", {"doc": doc, "printed_at": timezone.localtime()})
        return HttpResponse(html, content_type="text/html; charset=utf-8")
