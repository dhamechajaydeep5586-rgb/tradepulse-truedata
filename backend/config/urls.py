from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),

    # Auth
    path('api/auth/', include('users.urls')),

    # Data endpoints
    path('api/stocks/', include('stocks.urls')),
]
