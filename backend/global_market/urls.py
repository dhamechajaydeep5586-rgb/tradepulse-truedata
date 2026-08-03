from django.urls import path

from .views import LatestGlobalMarketView

app_name = 'global_market'

urlpatterns = [
    path('latest/', LatestGlobalMarketView.as_view(), name='latest'),
]
