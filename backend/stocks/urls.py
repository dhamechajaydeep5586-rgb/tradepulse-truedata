from django.urls import path

from .views import (
    LiveSignalView,
    OptionChainView,
    FIIDIIView,
    LivePriceUpdateView,
    PerformanceReportView,
    SignalBacktestView,
    ProSystemView,
    ProPerformanceReportView,
    NotificationView,
    DeltaHedgeView,
    CronScannerTriggerView,
    DashboardSummaryView,
    OptionBuyingView,
)
app_name = 'stocks'

urlpatterns = [
    path('live-signals/', LiveSignalView.as_view(), name='live-signals'),
    path('option-chain/', OptionChainView.as_view(), name='option-chain'),
    path('fii-dii/', FIIDIIView.as_view(), name='fii-dii'),
    path('live-price-updates/', LivePriceUpdateView.as_view(), name='live-price-updates'),
    path('performance-report/', PerformanceReportView.as_view(), name='performance-report'),
    path('signal-backtest/', SignalBacktestView.as_view(), name='signal-backtest'),
    path('pro-system/', ProSystemView.as_view(), name='pro-system'),
    path('pro-performance-report/', ProPerformanceReportView.as_view(), name='pro-performance-report'),
    path('delta-hedge/', DeltaHedgeView.as_view(), name='delta-hedge'),
    path('dashboard-summary/', DashboardSummaryView.as_view(), name='dashboard-summary'),
    path('option-buying/', OptionBuyingView.as_view(), name='option-buying'),
    path('notifications/', NotificationView.as_view(), name='notifications'),
    path('cron-trigger/', CronScannerTriggerView.as_view(), name='cron-trigger'),
]
