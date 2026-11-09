"""Every store gets a StoreConfig on creation — the per-store settings
(currency, payment keys, …) must never be missing when code reads them."""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from copy import deepcopy

from .models import Store, StoreConfig, StorefrontAsset, ensure_default_storefront_pages


@receiver(post_save, sender=Store)
def ensure_store_config(sender, instance, created, **kwargs):
    if created:
        from catalog.models import RetailType

        preset = instance.retail_type or RetailType.objects.filter(key="general").first()
        if not instance.catalog_config:
            instance.catalog_config = deepcopy(preset.config) if preset else {}
            Store.objects.filter(pk=instance.pk).update(catalog_config=instance.catalog_config)
        # `store_id` (not `store=instance`): assigning the full instance would
        # populate the parent's reverse OneToOne cache with this *default*
        # config, so later `store.config` reads would return stale values even
        # after the config row is updated.
        StoreConfig.objects.create(store_id=instance.pk)
        ensure_default_storefront_pages(instance)
        from .domain_services import ensure_platform_domain
        ensure_platform_domain(instance)


@receiver(post_delete, sender=StorefrontAsset)
def delete_storefront_asset_file(sender, instance, **kwargs):
    if instance.image:
        instance.image.delete(save=False)
