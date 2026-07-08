from django.urls import path
from .views import ChatView, InsightsView, AgentListView, DigitalTwinView, QuickProcureView

urlpatterns = [
    path('chat/', ChatView.as_view(), name='chat'),
    path('insights/', InsightsView.as_view(), name='insights'),
    path('agents/', AgentListView.as_view(), name='ai-agents'),
    path('digital-twin/', DigitalTwinView.as_view(), name='digital-twin'),
    path('quick-procure/', QuickProcureView.as_view(), name='quick-procure'),
]
