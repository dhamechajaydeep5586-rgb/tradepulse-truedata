from django.urls import path
from .views import RegisterView, CustomTokenObtainPairView, ProfileView
from rest_framework_simplejwt.views import TokenRefreshView

app_name = 'users'

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('login/', CustomTokenObtainPairView.as_view(), name='login'),
    path('profile/', ProfileView.as_view(), name='profile'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
]
