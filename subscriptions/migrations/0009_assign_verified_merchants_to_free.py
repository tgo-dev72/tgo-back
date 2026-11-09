from django.db import migrations
from django.utils import timezone


def assign_verified_merchants_to_free(apps, schema_editor):
    MerchantProfile = apps.get_model("accounts", "MerchantProfile")
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")

    free_plan = SubscriptionPlan.objects.filter(slug="free", is_active=True).first()
    if free_plan is None:
        return
    now = timezone.now()
    user_ids = MerchantProfile.objects.filter(
        user__is_active=True,
        user__email_verified_at__isnull=False,
    ).values_list("user_id", flat=True)
    for user_id in user_ids.iterator():
        scoped = UserSubscription.objects.filter(
            user_id=user_id,
            entitlement_scope="all_owned_stores",
            selected_store_id__isnull=True,
        )
        if scoped.exclude(plan_id=free_plan.pk).filter(
            status="active",
            current_period_end__gt=now,
        ).exists():
            continue
        scoped.exclude(plan_id=free_plan.pk).filter(status="active").update(
            status="expired",
            updated_at=now,
        )
        subscription = scoped.filter(plan_id=free_plan.pk).order_by("-id").first()
        values = {
            "plan_id": free_plan.pk,
            "selected_store_id": None,
            "status": "active",
            "started_at": subscription.started_at if subscription and subscription.started_at else now,
            "current_period_end": None,
            "cancel_at_period_end": False,
            "entitlement_scope": "all_owned_stores",
            "grants_premium_designs": False,
            "plan_revision": free_plan.revision,
            "entitlements_snapshot": {},
            "updated_at": now,
        }
        if subscription is None:
            UserSubscription.objects.create(user_id=user_id, **values)
        else:
            scoped.exclude(pk=subscription.pk).filter(status="active").update(
                status="canceled",
                updated_at=now,
            )
            scoped.filter(pk=subscription.pk).update(**values)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_alter_merchantprofile_phone_number"),
        ("subscriptions", "0008_multilingual_commercial_capabilities"),
    ]

    operations = [
        migrations.RunPython(assign_verified_merchants_to_free, migrations.RunPython.noop),
    ]
