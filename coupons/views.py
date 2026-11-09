from django.db.models import Q
from rest_framework import generics
from rest_framework.permissions import IsAuthenticated

from access_control.services import permitted_store

from .models import Coupon
from .serializers import CouponSerializer


def _managed_store(user, store_id, permission):
    return permitted_store(user, store_id, permission)


class CouponListCreateView(generics.ListCreateAPIView):
    serializer_class = CouponSerializer
    permission_classes = [IsAuthenticated]
    pagination_class = None

    def get_store(self):
        if not hasattr(self, "store"):
            permission = "discounts.view" if self.request.method == "GET" else "discounts.manage"
            self.store = _managed_store(self.request.user, self.kwargs["store_id"], permission)
        return self.store

    def get_queryset(self):
        queryset = Coupon.objects.filter(store=self.get_store()).prefetch_related("categories", "products")
        query = self.request.query_params.get("q", "").strip()
        if query:
            queryset = queryset.filter(Q(code__icontains=query))
        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = self.get_store()
        return context

    def perform_create(self, serializer):
        serializer.save(store=self.get_store())


class CouponDetailView(generics.RetrieveUpdateDestroyAPIView):
    serializer_class = CouponSerializer
    permission_classes = [IsAuthenticated]

    def get_store(self):
        if not hasattr(self, "store"):
            permission = "discounts.view" if self.request.method == "GET" else "discounts.manage"
            self.store = _managed_store(self.request.user, self.kwargs["store_id"], permission)
        return self.store

    def get_queryset(self):
        return Coupon.objects.filter(store=self.get_store()).prefetch_related("categories", "products")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["store"] = self.get_store()
        return context

