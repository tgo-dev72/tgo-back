from django.apps import AppConfig


class StoreMailConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "store_mail"
    verbose_name = "Store mailbox"

    def ready(self):
        from . import signals  # noqa: F401
