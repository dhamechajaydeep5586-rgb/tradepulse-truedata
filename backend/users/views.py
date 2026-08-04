from django.utils import timezone
from rest_framework import generics, status
from rest_framework.permissions import IsAuthenticated, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .serializers import RegisterSerializer, UserSerializer


class RegisterView(generics.CreateAPIView):
    """
    POST /api/auth/register/
    Only the Main Owner (Admin) can create additional users.
    All users created here are marked as 'is_temporary=True'.
    """
    serializer_class = RegisterSerializer
    permission_classes = (IsAdminUser,)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # Mark all new users as temporary by default
        user = serializer.save(is_temporary=True)
        return Response(
            UserSerializer(user).data,
            status=status.HTTP_201_CREATED,
        )

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def validate(self, attrs):
        data = super().validate(attrs)
        
        # Safe check for first login timestamp
        user = self.user
        if user and getattr(user, 'is_temporary', False) and not user.first_login_at:
            from django.utils import timezone
            user.first_login_at = timezone.now()
            user.save(update_fields=['first_login_at'])
            
        return data

class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer

class ProfileView(APIView):
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        # Audit fix M3: the guest-session cutoff (and the row deletion that goes
        # with it) is now enforced centrally in
        # users.authentication.GuestSessionAwareJWTAuthentication, which runs
        # before this view — every DRF endpoint gets it, not just this one. Any
        # request that reaches this line already has an unexpired session.
        serializer = UserSerializer(request.user)
        return Response(serializer.data)
