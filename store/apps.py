from django.apps import AppConfig


class StoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'store'

    def ready(self):
        from . import signals  # noqa: F401 — registers the post_save handler
        from corsheaders.signals import check_request_enabled
        from config.origins import custom_origin_matches_request

        def allow_verified_storefront_origin(sender, request, **kwargs):
            return custom_origin_matches_request(request)

        check_request_enabled.connect(
            allow_verified_storefront_origin,
            dispatch_uid="store.allow_verified_storefront_origin",
            weak=False,
        )
