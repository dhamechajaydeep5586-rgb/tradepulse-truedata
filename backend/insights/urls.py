from django.urls import path

from .views import DailyInsightView

app_name = 'insights'

urlpatterns = [
    path('daily/', DailyInsightView.as_view(), name='daily'),
]
