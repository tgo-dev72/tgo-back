from django.apps import AppConfig


class CatalogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'catalog'

    def ready(self):
        # `flush` preserves migration history but removes data-migration rows.
        # Reinstall mandatory retail reference data after migrate/flush.
        from django.db.models.signals import post_migrate

        from .signals import restore_catalog_reference_data

        post_migrate.connect(
            restore_catalog_reference_data,
            sender=self,
            dispatch_uid="catalog.restore_reference_data",
            weak=False,
        )
