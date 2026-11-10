import hmac
from datetime import timedelta
from decimal import Decimal, ROUND_DOWN

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from config.origins import is_allowed_return_origin, normalize_origin

from store.models import StoreConfig, StoreMember
from payments.gateways import (
    CheckoutRequest,
    GatewayEvent,
    GatewayConfigurationError,
    GatewayPaymentError,
    GatewayVerificationUnavailable,
    InvalidGatewayEvent,
    PaymentSubject,
    get_gateway,
)
from payments.gateways.availability import effective_platform_credentials, get_available_platform_gateway

from .entitlements import active_subscription_period_q, PREMIUM_DESIGNS_KEY, owner_has_store_entitlement, plan_entitlement_snapshot
from .models import (
    SubscriptionCreditAccount,
    SubscriptionCreditEntry,
    SubscriptionPayment,
    SubscriptionPlan,
    SubscriptionWebhookEvent,
    UserSubscription,
)


class SubscriptionError(RuntimeError):
    pass


class SubscriptionConfigError(SubscriptionError):
    pass


class PendingCheckoutConflict(SubscriptionError):
    """A payable provider session already exists for this subscription scope."""

    def __init__(self, payment, message=None):
        super().__init__(message or "A previous membership checkout is still active.")
        self.payment = payment


class InvalidSubscriptionWebhook(SubscriptionError):
    pass


class ProviderVerificationUnavailable(SubscriptionError):
    """The provider truth endpoint could not answer; webhook should retry."""

    pass


class PaymentCaptureError(SubscriptionError):
    def __init__(self, message, code="capture_failed"):
        super().__init__(message)
        self.code = code


@transaction.atomic
def ensure_free_subscription(user, now=None):
    """Idempotently grant the merchant's one expiring Free trial.

    A current paid account-wide membership always wins. Once Free expires it
    stays expired; status polling must never mint another trial.
    """
    now = now or timezone.now()
    user = get_user_model().objects.select_for_update().get(pk=user.pk)
    free_plan = SubscriptionPlan.objects.filter(slug="free", is_active=True).first()
    if free_plan is None:
        raise SubscriptionConfigError("The Free membership is temporarily unavailable.")

    scoped = UserSubscription.objects.select_for_update().filter(
        user=user,
        entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        selected_store__isnull=True,
    )
    paid = scoped.exclude(plan=free_plan).filter(
        status=UserSubscription.Status.ACTIVE,
        current_period_end__gt=now,
    ).order_by("-current_period_end", "-id").first()
    if paid is not None:
        return paid

    subscription = scoped.filter(plan=free_plan).order_by("-id").first()
    if subscription is not None and subscription.current_period_end is not None:
        if subscription.current_period_end <= now:
            if subscription.status == UserSubscription.Status.ACTIVE:
                subscription.status = UserSubscription.Status.EXPIRED
                subscription.save(update_fields=["status", "updated_at"])
                sync_user_entitlements(subscription)
            return subscription
        if subscription.status == UserSubscription.Status.ACTIVE:
            return subscription
        # Free is a one-time trial. Canceled/expired rows are not reactivated.
        return subscription

    # Invalid or elapsed paid rows must not conflict with the conditional
    # uniqueness constraint for the new active Free membership.
    scoped.exclude(plan=free_plan).filter(status=UserSubscription.Status.ACTIVE).update(
        status=UserSubscription.Status.EXPIRED,
        updated_at=now,
    )
    if subscription is None:
        subscription = UserSubscription(user=user, plan=free_plan)
    from platform_admin.models import PlatformCommerceSettings
    trial_days = PlatformCommerceSettings.load().free_trial_days
    scoped.exclude(pk=subscription.pk).filter(status=UserSubscription.Status.ACTIVE).update(
        status=UserSubscription.Status.CANCELED,
        updated_at=now,
    )
    subscription.plan = free_plan
    subscription.selected_store = None
    subscription.status = UserSubscription.Status.ACTIVE
    subscription.started_at = subscription.started_at or now
    subscription.current_period_end = now + timedelta(days=trial_days)
    subscription.cancel_at_period_end = False
    subscription.entitlement_scope = SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES
    subscription.grants_premium_designs = False
    subscription.plan_revision = free_plan.revision
    subscription.entitlements_snapshot = {}
    subscription.save()
    return subscription


def _schedule_membership_email(payment_id, event):
    """Send only after billing state commits; email failure stays non-fatal."""
    def deliver():
        payment = SubscriptionPayment.objects.select_related("user", "selected_store").filter(
            pk=payment_id,
        ).first()
        if payment is None:
            return
        from notifications.platform_email import (
            send_subscription_activated,
            send_subscription_payment_failed,
        )

        if event == "activated":
            send_subscription_activated(payment)
        else:
            send_subscription_payment_failed(payment)

    transaction.on_commit(deliver)


def _scope_filter(queryset, entitlement_scope, selected_store_id):
    queryset = queryset.filter(entitlement_scope=entitlement_scope)
    if entitlement_scope == SubscriptionPlan.EntitlementScope.ONE_STORE:
        return queryset.filter(selected_store_id=selected_store_id)
    return queryset.filter(selected_store__isnull=True)


def _resume_checkout_url(payment, lang):
    gateway = get_gateway(payment.provider)
    credentials = effective_platform_credentials(gateway)
    if "sandbox" in credentials:
        credentials["sandbox"] = payment.sandbox
    checkout_url = gateway.resume_checkout_url(
        payment.provider_token,
        payment.checkout_url,
        lang,
        credentials,
    )
    if checkout_url != payment.checkout_url:
        payment.checkout_url = checkout_url
        payment.save(update_fields=["checkout_url", "updated_at"])
    return checkout_url


def _retire_unresumable_checkout(payment):
    """Release an obsolete checkout so it cannot block a fresh hosted one."""
    _release_reserved_credit(payment)
    payment.status = SubscriptionPayment.Status.FAILED
    payment.idempotency_key = None
    payment.save(update_fields=["status", "idempotency_key", "updated_at"])


def _scoped_subscriptions(user, entitlement_scope, selected_store=None):
    queryset = UserSubscription.objects.filter(user=user)
    if entitlement_scope == SubscriptionPlan.EntitlementScope.ONE_STORE:
        return queryset.filter(
            entitlement_scope=entitlement_scope,
            selected_store=selected_store,
        )
    return queryset.filter(
        entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
        selected_store__isnull=True,
    )


def _proration_quote(user, plan, selected_store, now):
    """Quote whole-unit credit for full calendar days after the change date."""
    subscription = _scoped_subscriptions(
        user, plan.entitlement_scope, selected_store,
    ).select_related("plan").filter(
        active_subscription_period_q(now),
    ).order_by("-current_period_end", "-id").first()
    if not subscription or not subscription.plan or subscription.plan.price <= 0:
        return Decimal("0.00"), {}
    if subscription.plan.currency != plan.currency:
        return Decimal("0.00"), {
            "source_subscription_id": subscription.pk,
            "source_plan_id": subscription.plan_id,
            "excluded_reason": "currency_mismatch",
        }
    period_end = subscription.current_period_end
    period_start = subscription.started_at
    if period_start is None or period_start >= period_end:
        period_start = period_end - timedelta(days=subscription.plan.duration_days)
    change_date = timezone.localtime(now).date()
    period_end_date = timezone.localtime(period_end).date()
    # Date subtraction excludes the change date itself. Only days beginning
    # tomorrow can create credit, regardless of the time checkout occurs today.
    remaining_days = min(
        # Count only complete calendar days strictly after the change date
        # and strictly before the timestamp-bearing expiration date.
        max((period_end_date - change_date).days - 1, 0),
        subscription.plan.duration_days,
    )
    credit = (
        subscription.plan.price
        * Decimal(remaining_days)
        / Decimal(max(subscription.plan.duration_days, 1))
    ).quantize(Decimal("1"), rounding=ROUND_DOWN)
    credit = min(max(credit, Decimal("0")), subscription.plan.price.quantize(Decimal("1"), rounding=ROUND_DOWN))
    return credit, {
        "source_subscription_id": subscription.pk,
        "source_plan_id": subscription.plan_id,
        "source_plan_name": subscription.plan.name,
        "source_price": str(subscription.plan.price),
        "currency": subscription.plan.currency,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "change_date": change_date.isoformat(),
        "remaining_days": remaining_days,
        "rounding": "whole_currency_unit_down",
        "quoted_at": now.isoformat(),
    }


def _credit_account_locked(user, currency):
    account, _ = SubscriptionCreditAccount.objects.select_for_update().get_or_create(
        user=user,
        currency=currency,
        defaults={"balance": Decimal("0.00"), "reserved": Decimal("0.00")},
    )
    return account


def _reserve_available_credit(user, currency, gross_amount, minimum_external_amount=Decimal("0.00")):
    account = _credit_account_locked(user, currency)
    # Legacy fractional credit, if any, is never rounded up or spent. All
    # usable balance follows the whole-unit policy.
    available = max(account.available.quantize(Decimal("1"), rounding=ROUND_DOWN), Decimal("0"))
    if available >= gross_amount:
        amount = gross_amount
    else:
        amount = min(available, max(gross_amount - minimum_external_amount, Decimal("0.00")))
    if amount:
        account.reserved += amount
        account.save(update_fields=["reserved", "updated_at"])
    return amount


def _release_reserved_credit(payment):
    if payment.credit_reserved <= 0 or payment.credit_released_at or payment.credit_consumed_at:
        return
    account = _credit_account_locked(payment.user, payment.currency)
    account.reserved = max(Decimal("0.00"), account.reserved - payment.credit_reserved)
    account.save(update_fields=["reserved", "updated_at"])
    payment.credit_released_at = timezone.now()
    payment.save(update_fields=["credit_released_at", "updated_at"])


@transaction.atomic
def expire_stale_subscription_checkouts(user, now=None):
    """Release balance held by abandoned provider or initialization sessions."""
    now = now or timezone.now()
    initialization_cutoff = now - timedelta(
        seconds=settings.SUBSCRIPTION_CHECKOUT_INITIALIZATION_TIMEOUT_SECONDS,
    )
    session_cutoff = now - timedelta(
        seconds=settings.SUBSCRIPTION_CHECKOUT_SESSION_TTL_SECONDS,
    )
    pending = SubscriptionPayment.objects.select_for_update().filter(
        user=user,
        status=SubscriptionPayment.Status.PENDING,
    )
    stale = list(pending.filter(
        provider_token="",
        updated_at__lt=initialization_cutoff,
    )) + list(pending.exclude(provider_token="").filter(
        updated_at__lt=session_cutoff,
    ))
    for payment in stale:
        _release_reserved_credit(payment)
        payment.status = SubscriptionPayment.Status.FAILED
        payment.save(update_fields=["status", "updated_at"])
    return len(stale)


def _post_payment_credit(payment):
    """Consume the reservation and grant proration exactly once."""
    account = _credit_account_locked(payment.user, payment.currency)
    if payment.credit_reserved > 0 and not SubscriptionCreditEntry.objects.filter(
        payment=payment,
        entry_type=SubscriptionCreditEntry.EntryType.CHECKOUT_APPLIED,
    ).exists():
        if account.reserved < payment.credit_reserved or account.balance < payment.credit_reserved:
            raise SubscriptionError("The account credit reservation is inconsistent.")
        account.reserved -= payment.credit_reserved
        account.balance -= payment.credit_reserved
        account.save(update_fields=["balance", "reserved", "updated_at"])
        SubscriptionCreditEntry.objects.create(
            account=account,
            payment=payment,
            entry_type=SubscriptionCreditEntry.EntryType.CHECKOUT_APPLIED,
            amount=-payment.credit_reserved,
            balance_after=account.balance,
            metadata={"plan_id": payment.plan_id, "gross_amount": str(payment.gross_amount)},
        )
        payment.credit_consumed_at = timezone.now()
        payment.save(update_fields=["credit_consumed_at", "updated_at"])
    if payment.proration_credit > 0 and not SubscriptionCreditEntry.objects.filter(
        payment=payment,
        entry_type=SubscriptionCreditEntry.EntryType.UNUSED_PERIOD,
    ).exists():
        account.balance += payment.proration_credit
        account.save(update_fields=["balance", "updated_at"])
        SubscriptionCreditEntry.objects.create(
            account=account,
            payment=payment,
            entry_type=SubscriptionCreditEntry.EntryType.UNUSED_PERIOD,
            amount=payment.proration_credit,
            balance_after=account.balance,
            metadata=payment.proration_snapshot,
        )


def _return_origin(candidate=None):
    """Use the initiating dashboard under the central browser-origin policy."""
    fallback = settings.MERCHANT_DASHBOARD_URL.rstrip("/")
    if not candidate:
        return fallback
    origin = normalize_origin(candidate)
    allowed = [
        fallback,
        *(item.rstrip("/") for item in settings.CORS_ALLOWED_ORIGINS),
        *(item.rstrip("/") for item in settings.CSRF_TRUSTED_ORIGINS),
    ]
    if not origin or not is_allowed_return_origin(
        origin,
        configured_origins=allowed,
        allow_local=settings.DEBUG,
    ):
        raise SubscriptionError("This dashboard origin is not trusted for payment returns.")
    return origin


def _owned_store(user, store_id):
    if not store_id:
        return None
    membership = StoreMember.objects.select_related("store").filter(
        user=user, store_id=store_id, role=StoreMember.Roles.OWNER,
    ).first()
    if membership is None:
        raise SubscriptionError("Choose a store that you own.")
    return membership.store


def create_subscription_checkout(
    user,
    plan,
    store_id=None,
    lang="ar",
    return_origin=None,
    idempotency_key=None,
    use_balance=False,
    provider=None,
):
    if not plan.is_active or plan.price == Decimal("0.00"):
        raise SubscriptionError("This plan is not available for paid checkout.")
    origin = _return_origin(return_origin)
    selected_store = _owned_store(user, store_id)
    if plan.entitlement_scope == SubscriptionPlan.EntitlementScope.ONE_STORE and selected_store is None:
        raise SubscriptionError("Select the store this plan should upgrade.")
    if plan.entitlement_scope == SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES:
        has_merchant_account = hasattr(user, "merchant_profile")
        has_owned_store = StoreMember.objects.filter(
            user=user,
            role=StoreMember.Roles.OWNER,
        ).exists()
        if not (has_merchant_account or has_owned_store):
            raise SubscriptionError("Only a merchant account can purchase an account-wide plan.")
        selected_store = None

    lang = lang if lang in {"en", "fr", "ar"} else "ar"
    try:
        gateway = get_available_platform_gateway(provider)
    except ValueError as exc:
        raise SubscriptionConfigError(str(exc)) from exc
    credentials = effective_platform_credentials(gateway)
    checkout_template = CheckoutRequest(
        order_id="pending",
        amount=plan.price,
        currency=plan.currency,
        success_url=origin,
        error_url=origin,
        language=lang,
        provider_hosted=True,
    )
    entitlements_snapshot = plan_entitlement_snapshot(plan)
    grants_premium_designs = bool(entitlements_snapshot.get(PREMIUM_DESIGNS_KEY, plan.grants_premium_designs))
    with transaction.atomic():
        # The user row is the stable serialization point even before a payment or
        # subscription row exists. This prevents two workers creating two tokens.
        get_user_model().objects.select_for_update().get(pk=user.pk)
        now = timezone.now()
        initialization_cutoff = now - timedelta(
            seconds=settings.SUBSCRIPTION_CHECKOUT_INITIALIZATION_TIMEOUT_SECONDS,
        )
        session_cutoff = now - timedelta(
            seconds=settings.SUBSCRIPTION_CHECKOUT_SESSION_TTL_SECONDS,
        )
        if idempotency_key:
            keyed_payment = SubscriptionPayment.objects.select_for_update().filter(
                idempotency_key=idempotency_key,
            ).first()
            if keyed_payment:
                same_request = (
                    keyed_payment.user_id == user.pk
                    and keyed_payment.plan_id == plan.pk
                    and keyed_payment.plan_revision == plan.revision
                    and keyed_payment.selected_store_id == (selected_store.pk if selected_store else None)
                    and keyed_payment.entitlement_scope == plan.entitlement_scope
                    and keyed_payment.balance_authorized == bool(use_balance)
                    and keyed_payment.provider == gateway.code
                )
                if not same_request:
                    raise SubscriptionError("This checkout idempotency key was already used for another purchase.")
                if keyed_payment.status == SubscriptionPayment.Status.PENDING and keyed_payment.provider_token:
                    if keyed_payment.updated_at < session_cutoff:
                        _release_reserved_credit(keyed_payment)
                        keyed_payment.status = SubscriptionPayment.Status.FAILED
                        # Free the request key so the exact client retry can
                        # atomically replace an expired provider session.
                        keyed_payment.idempotency_key = None
                        keyed_payment.save(update_fields=["status", "idempotency_key", "updated_at"])
                    else:
                        try:
                            return keyed_payment, _resume_checkout_url(keyed_payment, lang)
                        except GatewayConfigurationError:
                            _retire_unresumable_checkout(keyed_payment)
                elif keyed_payment.status == SubscriptionPayment.Status.PENDING:
                    raise SubscriptionError("Your secure checkout is still being prepared. Please try again shortly.")
                else:
                    raise SubscriptionError("This checkout request has already reached a final state.")

        active_subscriptions = UserSubscription.objects.filter(
            user=user,
            plan=plan,
        ).filter(active_subscription_period_q())
        if plan.entitlement_scope == SubscriptionPlan.EntitlementScope.ONE_STORE:
            active_subscriptions = active_subscriptions.filter(selected_store=selected_store)
        else:
            active_subscriptions = active_subscriptions.filter(
                entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            )
        if active_subscriptions.exists():
            raise SubscriptionError("This plan is already active for the selected subscription scope.")

        current_subscription = _scoped_subscriptions(
            user, plan.entitlement_scope, selected_store,
        ).select_related("plan").filter(
            active_subscription_period_q(now),
        ).order_by("-current_period_end", "-id").first()
        if (
            current_subscription
            and current_subscription.plan
            and plan.price <= current_subscription.plan.price
        ):
            raise SubscriptionError(
                "A lower or equivalent plan cannot replace the current plan before its period ends."
            )

        pending_payments = _scope_filter(SubscriptionPayment.objects.filter(
            user=user,
            status=SubscriptionPayment.Status.PENDING,
        ), plan.entitlement_scope, selected_store.pk if selected_store else None)
        # A worker can stop after reserving the checkout but before persisting
        # the provider token. Only abandon reservations old enough that no
        # healthy request should still be tokenizing them.
        stale_payments = list(pending_payments.select_for_update().filter(
            provider_token="",
            updated_at__lt=initialization_cutoff,
        )) + list(pending_payments.select_for_update().exclude(provider_token="").filter(
            updated_at__lt=session_cutoff,
        ))
        for stale_payment in stale_payments:
            _release_reserved_credit(stale_payment)
            stale_payment.status = SubscriptionPayment.Status.FAILED
            stale_payment.save(update_fields=["status", "updated_at"])
        existing_payment = pending_payments.filter(
            plan=plan,
            plan_revision=plan.revision,
            balance_authorized=bool(use_balance),
            provider=gateway.code,
        ).exclude(provider_token="").first()
        if existing_payment:
            try:
                return existing_payment, _resume_checkout_url(existing_payment, lang)
            except GatewayConfigurationError:
                _retire_unresumable_checkout(existing_payment)

        conflicting_payment = pending_payments.exclude(provider_token="").first()
        if conflicting_payment:
            # Keep the resumable URL aligned with the current public/browser
            # origin even when the original checkout was created behind a
            # development proxy or an older deployment hostname.
            try:
                _resume_checkout_url(conflicting_payment, lang)
            except GatewayConfigurationError:
                _retire_unresumable_checkout(conflicting_payment)
            else:
                raise PendingCheckoutConflict(
                    conflicting_payment,
                    "Another membership checkout is already in progress for this account or store. "
                    "Complete it before choosing another plan."
                )

        if pending_payments.filter(provider_token="").exists():
            raise SubscriptionError("Your secure checkout is still being prepared. Please try again shortly.")

        try:
            gateway.validate_checkout(checkout_template, credentials)
        except Exception as exc:
            raise SubscriptionConfigError(str(exc)) from exc
        quoted_at = timezone.now()
        proration_credit, proration_snapshot = _proration_quote(
            user, plan, selected_store, quoted_at,
        )
        credit_reserved = Decimal("0.00")
        if use_balance:
            credit_reserved = _reserve_available_credit(
                user,
                plan.currency,
                plan.price,
                gateway.minimum_checkout_amount(credentials),
            )
        payment = SubscriptionPayment.objects.create(
            idempotency_key=idempotency_key,
            user=user,
            plan=plan,
            selected_store=selected_store,
            plan_name=plan.name,
            gross_amount=plan.price,
            amount=plan.price - credit_reserved,
            balance_authorized=bool(use_balance),
            credit_reserved=credit_reserved,
            proration_credit=proration_credit,
            proration_snapshot=proration_snapshot,
            currency=plan.currency,
            duration_days=plan.duration_days,
            entitlement_scope=plan.entitlement_scope,
            grants_premium_designs=grants_premium_designs,
            plan_revision=plan.revision,
            entitlements_snapshot=entitlements_snapshot,
            provider=gateway.code,
            sandbox=bool(credentials.get("sandbox", False)),
        )
        if payment.amount == Decimal("0.00"):
            payment.checkout_url = f"{origin}/subscription?payment=success&payment_id={payment.id}"
            _activate_payment_locked(payment, transaction_id=f"credit_{payment.id}")
            return payment, payment.checkout_url
    # Never hold the merchant/account database lock during a remote provider
    # request. The pending tokenless row above is the short-lived reservation
    # that prevents a second checkout from being created concurrently.
    success_url = f"{origin}/subscription?payment=success&payment_id={payment.id}"
    error_url = f"{origin}/subscription?payment=failed&payment_id={payment.id}"

    try:
        session = gateway.create_checkout(CheckoutRequest(
            order_id=str(payment.id),
            amount=payment.amount,
            currency=payment.currency,
            success_url=success_url,
            error_url=error_url,
            language=lang,
            customer_name=(f"{user.first_name} {user.last_name}".strip() or user.email),
            customer_email=user.email,
            metadata={"purpose": "platform_subscription", "payment_id": str(payment.id), "plan_slug": plan.slug},
            provider_hosted=True,
        ), credentials)
    except Exception:
        with transaction.atomic():
            failed_payment = SubscriptionPayment.objects.select_for_update().get(pk=payment.pk)
            if failed_payment.status == SubscriptionPayment.Status.PENDING and not failed_payment.provider_token:
                _release_reserved_credit(failed_payment)
                failed_payment.status = SubscriptionPayment.Status.FAILED
                failed_payment.save(update_fields=["status", "updated_at"])
        raise

    token_id = session.provider_token
    if not token_id or not session.payment_url:
        with transaction.atomic():
            failed_payment = SubscriptionPayment.objects.select_for_update().filter(
                pk=payment.pk,
                status=SubscriptionPayment.Status.PENDING,
                provider_token="",
            ).first()
            if failed_payment:
                _release_reserved_credit(failed_payment)
                failed_payment.status = SubscriptionPayment.Status.FAILED
                failed_payment.save(update_fields=["status", "updated_at"])
        raise SubscriptionError("The secure checkout could not be prepared. Please try again.")

    with transaction.atomic():
        payment = SubscriptionPayment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != SubscriptionPayment.Status.PENDING:
            raise SubscriptionError("This checkout is no longer available.")
        if not payment.provider_token:
            payment.provider_token = token_id
            payment.checkout_url = session.payment_url
            payment.save(update_fields=["provider_token", "checkout_url", "updated_at"])
    return payment, payment.checkout_url


def verify_provider_transaction(payment, event_or_payload):
    """Ask the payment's own adapter for provider truth before granting value."""
    gateway = get_gateway(payment.provider)
    event = (
        event_or_payload
        if isinstance(event_or_payload, GatewayEvent)
        else gateway.parse_webhook(event_or_payload)
    )
    subject = PaymentSubject(
        order_id=str(payment.pk),
        amount=payment.amount,
        currency=payment.currency,
        provider_token=payment.provider_token,
        metadata={"payment_id": str(payment.pk)},
    )
    credentials = effective_platform_credentials(gateway)
    if "sandbox" in credentials:
        credentials["sandbox"] = payment.sandbox
    try:
        return gateway.verify_transaction(event, subject, credentials)
    except GatewayVerificationUnavailable as exc:
        raise ProviderVerificationUnavailable(str(exc)) from exc
    except InvalidGatewayEvent as exc:
        raise InvalidSubscriptionWebhook(str(exc)) from exc


def _captured_event(payment):
    """Rebuild the normalized settlement event persisted before activation."""
    return GatewayEvent(
        provider=payment.provider,
        event_id=f"capture:{payment.provider_transaction_id}",
        event_name="PAYMENT.CAPTURE.COMPLETED",
        outcome="paid",
        order_id=str(payment.pk),
        provider_transaction_id=payment.provider_transaction_id,
        provider_token=payment.provider_token,
        amount=payment.amount,
        currency=payment.currency,
        sandbox=payment.sandbox,
        metadata={"payment_id": str(payment.pk), "purpose": "platform_subscription"},
        raw_payload=dict(payment.webhook_payload),
    )


def capture_subscription_payment(user, provider_order_id):
    """Capture an approved provider order and converge on one activation."""
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        payment = SubscriptionPayment.objects.select_for_update().filter(
            provider="paypal", provider_token=provider_order_id,
        ).first()
        if payment is None:
            raise SubscriptionError("PayPal order was not found.")
        if payment.user_id != user.pk:
            raise SubscriptionError("PayPal order was not found.")
        if payment.status in {SubscriptionPayment.Status.CONFIRMED, SubscriptionPayment.Status.DUPLICATE}:
            return payment
        if payment.status == SubscriptionPayment.Status.CAPTURED:
            event = _captured_event(payment)
        elif payment.status != SubscriptionPayment.Status.PENDING:
            raise SubscriptionError("This PayPal order can no longer be captured.")
        else:
            event = None

    gateway = get_gateway("paypal")
    credentials = effective_platform_credentials(gateway)
    credentials["sandbox"] = payment.sandbox
    if event is None:
        subject = PaymentSubject(
            order_id=str(payment.pk), amount=payment.amount,
            currency=payment.currency, provider_token=payment.provider_token,
            metadata={"payment_id": str(payment.pk)},
        )
        try:
            event = gateway.capture_payment(
                payment.provider_token, subject, credentials, str(payment.pk),
            )
            gateway.verify_transaction(event, subject, credentials)
        except GatewayPaymentError as exc:
            raise PaymentCaptureError(str(exc), exc.code) from exc
        except GatewayConfigurationError as exc:
            raise PaymentCaptureError(str(exc), "configuration_missing") from exc
        except GatewayVerificationUnavailable as exc:
            raise ProviderVerificationUnavailable(str(exc)) from exc
        except InvalidGatewayEvent as exc:
            raise InvalidSubscriptionWebhook(str(exc)) from exc

        # Persist provider settlement before local activation. If activation
        # fails, retries reconcile this capture and never charge a second time.
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=user.pk)
            payment = SubscriptionPayment.objects.select_for_update().get(pk=payment.pk)
            if payment.status in {SubscriptionPayment.Status.CONFIRMED, SubscriptionPayment.Status.DUPLICATE}:
                return payment
            if payment.status == SubscriptionPayment.Status.PENDING:
                payment.status = SubscriptionPayment.Status.CAPTURED
                payment.provider_transaction_id = event.provider_transaction_id
                payment.webhook_payload = dict(event.raw_payload)
                payment.processed_at = timezone.now()
                payment.save(update_fields=[
                    "status", "provider_transaction_id", "webhook_payload", "processed_at", "updated_at",
                ])
            event = _captured_event(payment)

    return handle_subscription_webhook(payment, event)


def sync_store_entitlement(store):
    """Derive a store's design entitlement from every current owner subscription."""
    covered = owner_has_store_entitlement(store, PREMIUM_DESIGNS_KEY)
    StoreConfig.objects.filter(store=store).update(
        plan=StoreConfig.Plans.PREMIUM if covered else StoreConfig.Plans.FREE,
    )
    return covered


def sync_user_entitlements(subscription):
    owned_store_ids = StoreMember.objects.filter(
        user_id=subscription.user_id,
        role=StoreMember.Roles.OWNER,
    ).values_list("store_id", flat=True)
    for store_id in owned_store_ids:
        sync_store_entitlement(store_id)


@transaction.atomic
def apply_plan_revision_to_active_subscriptions(plan):
    """Move effective subscribers to the plan's newly published promises.

    Subscription payment rows remain immutable commercial/audit snapshots. Only
    the currently effective access projection is advanced to the new revision.
    """
    subscriptions = list(
        UserSubscription.objects.select_for_update()
        .filter(plan=plan)
        .filter(active_subscription_period_q())
        .only("id", "user_id")
    )
    if not subscriptions:
        return 0

    snapshot = plan_entitlement_snapshot(plan)
    UserSubscription.objects.filter(pk__in=[item.pk for item in subscriptions]).update(
        plan_revision=plan.revision,
        entitlements_snapshot=snapshot,
        grants_premium_designs=bool(
            snapshot.get(PREMIUM_DESIGNS_KEY, plan.grants_premium_designs)
        ),
        updated_at=timezone.now(),
    )

    # StoreConfig.plan is a compatibility cache used by storefront deployment.
    # Refresh each affected owner's stores after the subscription projection is
    # updated so access checks and deployment configuration agree immediately.
    synced_users = set()
    for subscription in subscriptions:
        if subscription.user_id in synced_users:
            continue
        synced_users.add(subscription.user_id)
        sync_user_entitlements(subscription)
    return len(subscriptions)


def _activate_payment_locked(payment, transaction_id, now=None):
    """Activate a paid quote while its merchant row is locked.

    This single settlement path is shared by provider-confirmed payments and
    checkouts fully covered by previously earned account credit.
    """
    now = now or timezone.now()
    subscriptions = UserSubscription.objects.select_for_update().filter(user=payment.user)
    if payment.entitlement_scope == SubscriptionPlan.EntitlementScope.ONE_STORE:
        scope_subscriptions = subscriptions.filter(
            entitlement_scope=SubscriptionPlan.EntitlementScope.ONE_STORE,
            selected_store=payment.selected_store,
        )
    else:
        scope_subscriptions = subscriptions.filter(
            entitlement_scope=SubscriptionPlan.EntitlementScope.ALL_OWNED_STORES,
            selected_store__isnull=True,
        )
    newer_confirmed = _scope_filter(
        SubscriptionPayment.objects.filter(
            user=payment.user,
            status=SubscriptionPayment.Status.CONFIRMED,
            created_at__gt=payment.created_at,
        ),
        payment.entitlement_scope,
        payment.selected_store_id,
    ).exists()
    same_active_plan = scope_subscriptions.filter(
        plan_id=payment.plan_id,
    ).filter(active_subscription_period_q(now)).exists()
    if newer_confirmed or same_active_plan:
        _release_reserved_credit(payment)
        payment.status = SubscriptionPayment.Status.DUPLICATE
        payment.provider_transaction_id = transaction_id
        payment.processed_at = now
        payment.save()
        return payment

    subscription = scope_subscriptions.filter(
        status=UserSubscription.Status.ACTIVE,
    ).order_by("-current_period_end", "-id").first()
    if subscription is None:
        subscription = scope_subscriptions.order_by("-current_period_end", "-id").first()
    if subscription is None:
        subscription = UserSubscription(user=payment.user)
    scope_subscriptions.exclude(pk=subscription.pk).filter(status=UserSubscription.Status.ACTIVE).update(
        status=UserSubscription.Status.CANCELED,
    )
    subscription.plan = payment.plan
    subscription.selected_store = payment.selected_store
    subscription.status = UserSubscription.Status.ACTIVE
    # Every paid plan change starts a new, independently proratable period.
    subscription.started_at = now
    subscription.current_period_end = now + timedelta(days=payment.duration_days)
    subscription.cancel_at_period_end = False
    subscription.entitlement_scope = payment.entitlement_scope
    subscription.grants_premium_designs = payment.grants_premium_designs
    subscription.plan_revision = payment.plan_revision
    subscription.entitlements_snapshot = payment.entitlements_snapshot
    subscription.save()
    sync_user_entitlements(subscription)

    _post_payment_credit(payment)
    payment.status = SubscriptionPayment.Status.CONFIRMED
    payment.provider_transaction_id = transaction_id
    payment.processed_at = now
    payment.save()
    _schedule_membership_email(payment.pk, "activated")
    return payment


@transaction.atomic
def handle_subscription_webhook(payment, event_or_payload):
    gateway = get_gateway(payment.provider)
    try:
        event = (
            event_or_payload
            if isinstance(event_or_payload, GatewayEvent)
            else gateway.parse_webhook(event_or_payload)
        )
    except InvalidGatewayEvent as exc:
        raise InvalidSubscriptionWebhook(str(exc)) from exc
    event_id = event.event_id
    event_name = event.event_name
    if event.outcome not in {"paid", "failed", "ignored"}:
        raise InvalidSubscriptionWebhook("Invalid payment outcome.")
    if not event_id:
        raise InvalidSubscriptionWebhook("Webhook event ID is required.")
    if len(event_id) > SubscriptionWebhookEvent._meta.get_field("event_id").max_length:
        raise InvalidSubscriptionWebhook("Webhook event ID is too long.")
    if len(event_name) > SubscriptionWebhookEvent._meta.get_field("event_name").max_length:
        raise InvalidSubscriptionWebhook("Webhook event name is too long.")

    # Always lock the stable account row first. Locking only subscriptions is
    # ineffective for the first activation because there is no row yet.
    get_user_model().objects.select_for_update().get(pk=payment.user_id)
    # Do not join the nullable plan FK here: PostgreSQL rejects FOR UPDATE on
    # the nullable side of that outer join.
    payment = SubscriptionPayment.objects.select_for_update().get(pk=payment.pk)
    if event.provider != payment.provider:
        raise InvalidSubscriptionWebhook("Payment provider does not match.")

    webhook_event, created = SubscriptionWebhookEvent.objects.get_or_create(
        provider=event.provider,
        event_id=event_id,
        defaults={
            "event_name": event_name,
            "payment": payment,
            "status": SubscriptionWebhookEvent.Status.PROCESSED,
            "payload": dict(event.raw_payload),
        },
    )
    if not created:
        if webhook_event.payment_id and webhook_event.payment_id != payment.pk:
            raise InvalidSubscriptionWebhook("Webhook event ID belongs to another payment.")
        if webhook_event.event_name != event_name or webhook_event.payload != dict(event.raw_payload):
            raise InvalidSubscriptionWebhook("Webhook event ID was reused with different data.")
        return payment

    if event.outcome == "ignored":
        webhook_event.status = SubscriptionWebhookEvent.Status.IGNORED
        webhook_event.save(update_fields=["status"])
        return payment

    # A payment keeps the environment in which its provider order was created.
    # Compare settlement against that immutable quote, not the gateway's current
    # default: live payments must remain reconcilable even if admin mode changes.
    if event.sandbox is None or event.sandbox != payment.sandbox:
        raise InvalidSubscriptionWebhook("Payment environment does not match.")

    if payment.status == SubscriptionPayment.Status.CONFIRMED:
        return payment
    if payment.status == SubscriptionPayment.Status.CANCELED:
        _release_reserved_credit(payment)
        webhook_event.status = SubscriptionWebhookEvent.Status.IGNORED
        webhook_event.save(update_fields=["status"])
        return payment

    if event.order_id not in {str(payment.pk), payment.provider_token}:
        raise InvalidSubscriptionWebhook("Payment order does not match.")
    metadata = event.metadata
    if metadata.get("purpose") not in {None, "platform_subscription"}:
        raise InvalidSubscriptionWebhook("Payment purpose does not match.")
    if metadata.get("payment_id") not in {None, str(payment.pk)}:
        raise InvalidSubscriptionWebhook("Payment metadata does not match.")

    if not event.provider_token or not hmac.compare_digest(event.provider_token, payment.provider_token):
        raise InvalidSubscriptionWebhook("Payment token does not match.")
    if event.amount != payment.amount or event.currency != payment.currency:
        raise InvalidSubscriptionWebhook("Payment amount or currency does not match.")

    transaction_id = event.provider_transaction_id
    if not transaction_id:
        raise InvalidSubscriptionWebhook("Provider transaction ID is required.")
    if SubscriptionPayment.objects.exclude(pk=payment.pk).filter(
        provider=payment.provider,
        provider_transaction_id=transaction_id,
    ).exists():
        raise InvalidSubscriptionWebhook("Provider transaction ID belongs to another payment.")

    paid = event.outcome == "paid"
    if payment.status == SubscriptionPayment.Status.FAILED and not paid:
        webhook_event.status = SubscriptionWebhookEvent.Status.IGNORED
        webhook_event.save(update_fields=["status"])
        return payment
    payment.webhook_payload = dict(event.raw_payload)
    payment.processed_at = timezone.now()
    payment.provider_transaction_id = transaction_id
    if not paid:
        _release_reserved_credit(payment)
        payment.status = SubscriptionPayment.Status.FAILED
        payment.save()
        _schedule_membership_email(payment.pk, "failed")
        return payment

    return _activate_payment_locked(payment, transaction_id)


def refresh_expired_subscription(subscription):
    """Expire from freshly locked state so status reads cannot undo a renewal."""
    if subscription is None:
        return None
    expired_now = False
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=subscription.user_id)
        # Lock only the subscription row. Both related foreign keys are
        # nullable, so select_related() would produce LEFT OUTER JOINs and
        # PostgreSQL rejects FOR UPDATE on the nullable side of those joins.
        # Relations can be loaded lazily after this row has been locked.
        current = UserSubscription.objects.select_for_update().get(pk=subscription.pk)
        if (
            current.status == UserSubscription.Status.ACTIVE
            and current.current_period_end
            and current.current_period_end <= timezone.now()
        ):
            current.status = UserSubscription.Status.EXPIRED
            current.save(update_fields=["status", "updated_at"])
            sync_user_entitlements(current)
            expired_now = True
        # Preserve the historical in-place contract for callers that keep the
        # object they passed, while returning the freshly locked instance.
        subscription.status = current.status
        subscription.current_period_end = current.current_period_end
        if expired_now:
            subscription_id = current.pk

            def notify_expiry():
                from .lifecycle import dispatch_expired_subscription_notice

                dispatch_expired_subscription_notice(subscription_id)

            transaction.on_commit(notify_expiry)
        return current
