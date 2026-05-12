from django.urls import path
from .views import ChatView, InsightsView

urlpatterns = [
    path('chat/', ChatView.as_view(), name='chat'),
    path('insights/', InsightsView.as_view(), name='insights'),
]

