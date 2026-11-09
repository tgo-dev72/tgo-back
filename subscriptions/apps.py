from django.apps import AppConfig


class SubscriptionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "subscriptions"
    verbose_name = "Platform subscriptions"

    def ready(self):
        from django.db.models.signals import post_migrate

        from .signals import restore_subscription_reference_data

        post_migrate.connect(
            restore_subscription_reference_data,
            sender=self,
            dispatch_uid="subscriptions.restore_commercial_reference_data",
            weak=False,
        )
