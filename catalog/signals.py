"""Lifecycle hooks for mandatory catalog reference data."""

from django.core.exceptions import ValidationError
from django.db.models.signals import m2m_changed
from django.dispatch import receiver

from .models import CatalogSection, Product, ProductVendor
from .seeds import ensure_retail_types


def restore_catalog_reference_data(*, using="default", **kwargs):
    """Restore idempotent system choices after migrate and Django flush."""
    ensure_retail_types(using=using)


def _reject_cross_store(instance, reverse, model, pk_set, label):
    if not pk_set:
        return
    expected_store_id = instance.store_id
    if model.objects.filter(pk__in=pk_set).exclude(store_id=expected_store_id).exists():
        raise ValidationError(f"Every {label} must belong to the same store.")


@receiver(m2m_changed, sender=Product.categories.through)
def validate_product_categories(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action != "pre_add":
        return
    _reject_cross_store(instance, reverse, model, pk_set, "category or product")


@receiver(m2m_changed, sender=Product.vendors.through)
def validate_product_vendors(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action != "pre_add":
        return
    _reject_cross_store(instance, reverse, model, pk_set, "vendor or product")


@receiver(m2m_changed, sender=Product.related_products.through)
def validate_related_products(sender, instance, action, reverse, model, pk_set, **kwargs):
    if action != "pre_add":
        return
    _reject_cross_store(instance, reverse, model, pk_set, "related product")
    if instance.pk in (pk_set or set()):
        raise ValidationError("A product cannot recommend itself.")
