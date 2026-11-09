"""Model-level tenant boundaries for coupon targeting relations."""

from django.core.exceptions import ValidationError
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from .models import Coupon


def _reject_cross_store(instance, model, pk_set, label):
    if pk_set and model.objects.filter(pk__in=pk_set).exclude(store_id=instance.store_id).exists():
        raise ValidationError(f"Every targeted {label} must belong to the coupon's store.")


@receiver(m2m_changed, sender=Coupon.categories.through)
def validate_coupon_categories(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action != "pre_add":
        return
    _reject_cross_store(instance, model, pk_set, "category")


@receiver(m2m_changed, sender=Coupon.products.through)
def validate_coupon_products(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action != "pre_add":
        return
    _reject_cross_store(instance, model, pk_set, "product")
