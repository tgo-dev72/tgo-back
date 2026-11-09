from collections import Counter
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, Exists, F, Max, OuterRef, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone

from catalog.models import Product, StockVariant
from orders.models import Order, OrderedProduct
from store.models import StoreCustomer, StoreMember
from subscriptions.entitlements import resolve_entitlement
from subscriptions.policies import ANALYTICS_LEVEL_KEY

from .models import StorefrontEvent


ANALYTICS_RANGES = {
    "essential": (7, 30),
    "standard": (7, 30, 90),
    "advanced": (7, 30, 90, 365),
}


def store_analytics_level(store):
    owner = StoreMember.objects.filter(
        store=store, role=StoreMember.Roles.OWNER,
    ).select_related("user").first()
    if owner is None:
        return "essential"
    level = resolve_entitlement(owner.user, ANALYTICS_LEVEL_KEY)
    return level if level in ANALYTICS_RANGES else "essential"


def is_collected_revenue_order(order):
    """Canonical revenue recognition for merchant reporting.

    Online money is collected when the gateway confirms it. COD is collected
    only when delivery is completed; the ``cod`` payment marker alone merely
    describes the payment method.
    """
    if order.status == Order.Status.CANCELLED:
        return False
    if order.payment_mode == Order.PaymentMode.ONLINE:
        return order.is_paid == Order.IsPaid.CONFIRMED
    return order.payment_mode == Order.PaymentMode.COD and order.status == Order.Status.DELIVERED


def _money(value):
    return str((value or Decimal("0.00")).quantize(Decimal("0.01")))


def _percent(current, previous):
    if previous == 0:
        return None
    return float(((current - previous) / previous) * Decimal("100"))


def _rate(numerator, denominator):
    return round((numerator / denominator) * 100, 2) if denominator else 0


def _change(current, previous):
    return _percent(Decimal(str(current)), Decimal(str(previous)))


def _session_progression_rate(events, from_event, to_event):
    """Measure progression only among sessions observed at the prior stage.

    Independent event totals may be incomplete because analytics delivery is
    intentionally best effort. A later-stage event must therefore never make
    a rate exceed 100% or abandonment become negative.
    """
    from_sessions = (
        events.filter(event_type=from_event)
        .exclude(session_hash="")
        .values("session_hash")
        .distinct()
    )
    denominator = from_sessions.count()
    if not denominator:
        return 0
    later_session = events.filter(
        event_type=to_event,
        session_hash=OuterRef("session_hash"),
    )
    progressed = from_sessions.annotate(
        reached_next=Exists(later_session),
    ).filter(reached_next=True).count()
    return _rate(progressed, denominator)


FUNNEL_EVENT_TYPES = {
    "visits": StorefrontEvent.EventType.PAGE_VIEW,
    "product_view_sessions": StorefrontEvent.EventType.PRODUCT_VIEW,
    "cart_sessions": StorefrontEvent.EventType.ADD_TO_CART,
    "checkout_sessions": StorefrontEvent.EventType.CHECKOUT_STARTED,
    "order_submission_sessions": StorefrontEvent.EventType.ORDER_SUBMITTED,
}


def _funnel_summary(events):
    session_filter = ~Q(session_hash="")
    session_counts = events.aggregate(**{
        key: Count(
            "session_hash",
            distinct=True,
            filter=Q(event_type=event_type) & session_filter,
        )
        for key, event_type in FUNNEL_EVENT_TYPES.items()
    })
    event_counts = events.aggregate(
        product_views=Count("id", filter=Q(event_type=StorefrontEvent.EventType.PRODUCT_VIEW)),
        cart_additions=Count("id", filter=Q(event_type=StorefrontEvent.EventType.ADD_TO_CART)),
        checkout_starts=Count("id", filter=Q(event_type=StorefrontEvent.EventType.CHECKOUT_STARTED)),
        order_submissions=Count("id", filter=Q(event_type=StorefrontEvent.EventType.ORDER_SUBMITTED)),
    )
    product_sessions = session_counts["product_view_sessions"]
    cart_sessions = session_counts["cart_sessions"]
    checkout_sessions = session_counts["checkout_sessions"]
    submitted_sessions = session_counts["order_submission_sessions"]
    add_to_cart_rate = _session_progression_rate(
        events, StorefrontEvent.EventType.PRODUCT_VIEW, StorefrontEvent.EventType.ADD_TO_CART,
    )
    checkout_start_rate = _session_progression_rate(
        events, StorefrontEvent.EventType.ADD_TO_CART, StorefrontEvent.EventType.CHECKOUT_STARTED,
    )
    order_submission_rate = _session_progression_rate(
        events, StorefrontEvent.EventType.CHECKOUT_STARTED, StorefrontEvent.EventType.ORDER_SUBMITTED,
    )
    cart_to_order_rate = _session_progression_rate(
        events, StorefrontEvent.EventType.ADD_TO_CART, StorefrontEvent.EventType.ORDER_SUBMITTED,
    )
    return {
        "store_visits": session_counts["visits"],
        **event_counts,
        "unique_sessions": {
            "product_views": product_sessions,
            "cart_additions": cart_sessions,
            "checkout_starts": checkout_sessions,
            "order_submissions": submitted_sessions,
        },
        "rates": {
            "add_to_cart": add_to_cart_rate,
            "checkout_start": checkout_start_rate,
            "order_submission": order_submission_rate,
            "cart_abandonment": round(100 - cart_to_order_rate, 2) if cart_sessions else 0,
            "checkout_abandonment": round(100 - order_submission_rate, 2) if checkout_sessions else 0,
        },
    }


def _event_product_rows(events):
    return {
        row["product_id"]: row
        for row in events.filter(product_id__isnull=False)
        .values("product_id", "product__name")
        .annotate(
            views=Count("id", filter=Q(event_type=StorefrontEvent.EventType.PRODUCT_VIEW)),
            cart_additions=Count("id", filter=Q(event_type=StorefrontEvent.EventType.ADD_TO_CART)),
            view_sessions=Count(
                "session_hash", distinct=True,
                filter=Q(event_type=StorefrontEvent.EventType.PRODUCT_VIEW) & ~Q(session_hash=""),
            ),
            cart_sessions=Count(
                "session_hash", distinct=True,
                filter=Q(event_type=StorefrontEvent.EventType.ADD_TO_CART) & ~Q(session_hash=""),
            ),
        )
    }


def _product_performance(events, current_orders, collected_orders, level):
    event_rows = _event_product_rows(events)
    active_order_ids = [order.pk for order in current_orders if order.status != Order.Status.CANCELLED]
    collected_ids = [order.pk for order in collected_orders]
    order_rows = {
        row["product_id"]: row
        for row in OrderedProduct.objects.filter(order_id__in=active_order_ids)
        .values("product_id")
        .annotate(
            name=Max("name"),
            orders=Count("order_id", distinct=True),
            quantity_sold=Sum("quantity"),
            collected_orders=Count("order_id", distinct=True, filter=Q(order_id__in=collected_ids)),
            revenue=Sum(
                F("price") * F("quantity"),
                filter=Q(order_id__in=collected_ids),
                output_field=DecimalField(max_digits=18, decimal_places=2),
            ),
        )
    }
    product_ids = set(event_rows) | set(order_rows)
    rows = []
    for product_id in product_ids:
        event_row = event_rows.get(product_id, {})
        order_row = order_rows.get(product_id, {})
        views = event_row.get("views", 0)
        view_sessions = event_row.get("view_sessions", 0)
        orders = order_row.get("orders", 0)
        revenue = order_row.get("revenue") or Decimal("0.00")
        row = {
            "product_id": product_id,
            "name": order_row.get("name") or event_row.get("product__name") or "",
            "views": views,
            "view_sessions": view_sessions,
            "cart_additions": event_row.get("cart_additions", 0),
            "cart_sessions": event_row.get("cart_sessions", 0),
            "orders": orders,
            "quantity": order_row.get("quantity_sold") or 0,
            "collected_orders": order_row.get("collected_orders", 0),
            "conversion_rate": _rate(orders, view_sessions or views),
            "revenue": _money(revenue),
        }
        if level == "advanced":
            row["revenue_per_view"] = _money(revenue / views if views else Decimal("0.00"))
        rows.append(row)
    return sorted(rows, key=lambda item: (-item["views"], -item["cart_additions"], item["name"]))[:50]


def _zero_result_searches(events):
    return [
        {"query": row["properties__query"], "searches": row["searches"]}
        for row in events.filter(
            event_type=StorefrontEvent.EventType.SEARCH,
            properties__results_count=0,
        ).exclude(properties__query="")
        .values("properties__query")
        .annotate(searches=Count("id"))
        .order_by("-searches", "properties__query")[:20]
    ]


def _funnel_trend(events, period_start_date, days, collected_orders):
    event_rows = {
        row["day"]: row
        for row in events.annotate(day=TruncDate("occurred_at"))
        .values("day")
        .annotate(
            visits=Count("session_hash", distinct=True, filter=Q(event_type=StorefrontEvent.EventType.PAGE_VIEW) & ~Q(session_hash="")),
            product_views=Count("id", filter=Q(event_type=StorefrontEvent.EventType.PRODUCT_VIEW)),
            cart_additions=Count("id", filter=Q(event_type=StorefrontEvent.EventType.ADD_TO_CART)),
            checkout_starts=Count("id", filter=Q(event_type=StorefrontEvent.EventType.CHECKOUT_STARTED)),
            order_submissions=Count("id", filter=Q(event_type=StorefrontEvent.EventType.ORDER_SUBMITTED)),
        )
    }
    paid_by_day = Counter(timezone.localtime(order.created_at).date() for order in collected_orders)
    return [
        {
            "date": day.isoformat(),
            "visits": event_rows.get(day, {}).get("visits", 0),
            "product_views": event_rows.get(day, {}).get("product_views", 0),
            "cart_additions": event_rows.get(day, {}).get("cart_additions", 0),
            "checkout_starts": event_rows.get(day, {}).get("checkout_starts", 0),
            "order_submissions": event_rows.get(day, {}).get("order_submissions", 0),
            "collected_orders": paid_by_day.get(day, 0),
        }
        for day in (period_start_date + timedelta(days=offset) for offset in range(days))
    ]


def _source_performance(events):
    visits = {
        (row["properties__source"] or "direct", row["properties__campaign"] or ""): row["sessions"]
        for row in events.filter(event_type=StorefrontEvent.EventType.PAGE_VIEW)
        .values("properties__source", "properties__campaign")
        .annotate(sessions=Count("session_hash", distinct=True, filter=~Q(session_hash="")))
    }
    submissions = {
        (row["properties__source"] or "direct", row["properties__campaign"] or ""): row["orders"]
        for row in events.filter(event_type=StorefrontEvent.EventType.ORDER_SUBMITTED)
        .values("properties__source", "properties__campaign")
        .annotate(orders=Count("order_id", distinct=True))
    }
    collected_by_source = {}
    seen_orders = set()
    submission_rows = events.filter(
        event_type=StorefrontEvent.EventType.ORDER_SUBMITTED,
        order_id__isnull=False,
    ).select_related("order").order_by("occurred_at", "id")
    for event in submission_rows:
        if event.order_id in seen_orders or not is_collected_revenue_order(event.order):
            continue
        seen_orders.add(event.order_id)
        key = (event.properties.get("source") or "direct", event.properties.get("campaign") or "")
        entry = collected_by_source.setdefault(key, {"orders": 0, "revenue": Decimal("0.00")})
        entry["orders"] += 1
        entry["revenue"] += event.order.amount
    keys = set(visits) | set(submissions) | set(collected_by_source)
    return sorted([
        {
            "source": source,
            "campaign": campaign,
            "visits": visits.get((source, campaign), 0),
            "order_submissions": submissions.get((source, campaign), 0),
            "collected_orders": collected_by_source.get((source, campaign), {}).get("orders", 0),
            "revenue": _money(collected_by_source.get((source, campaign), {}).get("revenue")),
            "conversion_rate": _rate(submissions.get((source, campaign), 0), visits.get((source, campaign), 0)),
        }
        for source, campaign in keys
    ], key=lambda item: (-item["order_submissions"], -item["visits"], item["source"]))[:30]


def _opportunity_cards(product_rows, funnel):
    cards = []
    for product in product_rows:
        if product["views"] >= 10 and product["conversion_rate"] < 2:
            cards.append({
                "kind": "high_traffic_low_conversion",
                "severity": "warning",
                "product_id": product["product_id"],
                "product_name": product["name"],
                "metrics": {key: product[key] for key in ("views", "cart_additions", "orders", "conversion_rate")},
            })
        elif product["cart_additions"] >= 5 and product["orders"] == 0:
            cards.append({
                "kind": "carted_not_ordered",
                "severity": "warning",
                "product_id": product["product_id"],
                "product_name": product["name"],
                "metrics": {key: product[key] for key in ("views", "cart_additions", "orders", "conversion_rate")},
            })
        if len(cards) >= 8:
            break
    if funnel["unique_sessions"]["cart_additions"] >= 5 and funnel["rates"]["cart_abandonment"] >= 70:
        cards.append({
            "kind": "high_cart_abandonment",
            "severity": "warning",
            "product_id": None,
            "product_name": "",
            "metrics": {"cart_abandonment": funnel["rates"]["cart_abandonment"]},
        })
    return cards[:8]


def _identity(order):
    if order.client_id:
        return f"client:{order.client_id}"
    email = (order.customer_email or "").strip().lower()
    phone = (order.customer_phone or "").strip()
    return f"email:{email}" if email else (f"phone:{phone}" if phone else "")


def build_store_report(store, days, level=None, now=None):
    """Return a bounded, API-ready report without transferring raw datasets."""
    now = now or timezone.now()
    level = level or store_analytics_level(store)
    today = timezone.localdate(now)
    period_start_date = today - timedelta(days=days - 1)
    previous_start_date = period_start_date - timedelta(days=days)
    tz = timezone.get_current_timezone()
    period_start = timezone.make_aware(datetime.combine(period_start_date, time.min), tz)
    previous_start = timezone.make_aware(datetime.combine(previous_start_date, time.min), tz)

    query_start = previous_start if level == "advanced" else period_start
    orders = list(
        Order.objects.filter(store=store, created_at__gte=query_start, created_at__lte=now)
        .select_related("client")
    )
    current = [order for order in orders if order.created_at >= period_start]
    previous = [order for order in orders if previous_start <= order.created_at < period_start]
    collected = [order for order in current if is_collected_revenue_order(order)]
    previous_collected = [order for order in previous if is_collected_revenue_order(order)]
    event_query = StorefrontEvent.objects.filter(
        store=store,
        occurred_at__gte=query_start,
        occurred_at__lte=now,
    )
    current_events = event_query.filter(occurred_at__gte=period_start)
    previous_events = event_query.filter(occurred_at__lt=period_start)

    revenue = sum((order.amount for order in collected), Decimal("0.00"))
    previous_revenue = sum((order.amount for order in previous_collected), Decimal("0.00"))
    current_ids = [order.pk for order in current]
    item_count = (
        OrderedProduct.objects.filter(order_id__in=current_ids).aggregate(total=Sum("quantity"))["total"] or 0
    )
    status_counts = Counter(order.status for order in current)

    daily = {
        period_start_date + timedelta(days=offset): {"revenue": Decimal("0.00"), "orders": 0}
        for offset in range(days)
    }
    weekdays = {day: {"revenue": Decimal("0.00"), "orders": 0} for day in range(7)}
    hours = {hour: {"revenue": Decimal("0.00"), "orders": 0} for hour in range(24)}
    for order in current:
        local_created = timezone.localtime(order.created_at)
        order_day = local_created.date()
        if order_day in daily:
            daily[order_day]["orders"] += 1
        js_weekday = (local_created.weekday() + 1) % 7
        weekdays[js_weekday]["orders"] += 1
        hours[local_created.hour]["orders"] += 1
    for order in collected:
        local_created = timezone.localtime(order.created_at)
        order_day = local_created.date()
        if order_day in daily:
            daily[order_day]["revenue"] += order.amount
        js_weekday = (local_created.weekday() + 1) % 7
        weekdays[js_weekday]["revenue"] += order.amount
        hours[local_created.hour]["revenue"] += order.amount

    stock_totals = list(
        Product.objects.filter(store=store).annotate(
            stock_quantity=Sum("variants__quantity"),
            stock_reserved=Sum("variants__reserved"),
        ).values("track_inventory", "stock_quantity", "stock_reserved")
    )
    stock = {"in_stock": 0, "low_stock": 0, "out_of_stock": 0}
    for product in stock_totals:
        if not product["track_inventory"]:
            stock["in_stock"] += 1
            continue
        available = (product["stock_quantity"] or 0) - (product["stock_reserved"] or 0)
        if available <= 0:
            stock["out_of_stock"] += 1
        elif available <= 5:
            stock["low_stock"] += 1
        else:
            stock["in_stock"] += 1

    collected_ids = [order.pk for order in collected]
    top_products_payload = []
    if collected_ids and level in {"standard", "advanced"}:
        rows = (
            OrderedProduct.objects.filter(order_id__in=collected_ids)
            .values("product_id")
            .annotate(
                name=Max("name"),
                quantity_sold=Sum("quantity"),
                revenue=Sum(
                    F("price") * F("quantity"),
                    output_field=DecimalField(max_digits=18, decimal_places=2),
                ),
            )
            .order_by("-revenue")[:8]
        )
        top_products_payload = [
            {
                "product_id": row["product_id"],
                "name": row["name"],
                "quantity": row["quantity_sold"],
                "revenue": _money(row["revenue"]),
            }
            for row in rows
        ]

    payment = {"online": Decimal("0.00"), "cod": Decimal("0.00")}
    for order in collected:
        payment[order.payment_mode] += order.amount

    identities = Counter(filter(None, (_identity(order) for order in collected)))
    fulfilled_hours = [
        (order.delivered_at - order.created_at).total_seconds() / 3600
        for order in current if order.delivered_at and order.delivered_at >= order.created_at
    ]
    best_weekday = max(weekdays.items(), key=lambda item: item[1]["revenue"])[0] if revenue else None
    best_hour = max(hours.items(), key=lambda item: item[1]["revenue"])[0] if revenue else None
    currency = getattr(getattr(store, "config", None), "currency", "MAD")
    customer_count = StoreCustomer.objects.filter(store=store).count()
    product_count = len(stock_totals)
    funnel = _funnel_summary(current_events) if level in {"standard", "advanced"} else None
    product_performance = (
        _product_performance(current_events, current, collected, level)
        if level in {"standard", "advanced"} else []
    )
    previous_funnel = _funnel_summary(previous_events) if level == "advanced" else None

    return {
        "level": level,
        "range_days": days,
        "available_ranges": list(ANALYTICS_RANGES[level]),
        "currency": currency,
        "generated_at": now.isoformat(),
        "has_data": bool(current) or customer_count > 0 or product_count > 0 or current_events.exists(),
        "kpis": {
            "collected_revenue": _money(revenue),
            "average_order_value": _money(revenue / len(collected) if collected else Decimal("0.00")),
            "customers": customer_count,
            "products": product_count,
            "average_items_per_order": round(item_count / len(current), 2) if current else 0,
            "out_of_stock_products": stock["out_of_stock"],
        },
        "daily": [
            {"date": day.isoformat(), "revenue": _money(values["revenue"]), "orders": values["orders"]}
            for day, values in daily.items()
        ],
        "order_status": {
            status: status_counts.get(status, 0)
            for status in ("pending", "confirmed", "shipped", "delivered", "cancelled")
        },
        "standard": ({
            "weekdays": [
                {"day": day, "revenue": _money(values["revenue"]), "orders": values["orders"]}
                for day, values in weekdays.items()
            ],
            "hours": [
                {"hour": hour, "revenue": _money(values["revenue"]), "orders": values["orders"]}
                for hour, values in hours.items()
            ],
            "top_products": top_products_payload,
            "payment": {"online": _money(payment["online"]), "cod": _money(payment["cod"])},
            "stock": stock,
            "funnel": funnel,
            "product_performance": product_performance,
            "zero_result_searches": _zero_result_searches(current_events),
        } if level in {"standard", "advanced"} else None),
        "advanced": ({
            "revenue_change_percent": _percent(revenue, previous_revenue),
            "repeat_customer_rate": round(
                (sum(1 for count in identities.values() if count > 1) / len(identities)) * 100, 2,
            ) if identities else 0,
            "cancellation_rate": round(
                (status_counts.get(Order.Status.CANCELLED, 0) / len(current)) * 100, 2,
            ) if current else 0,
            "average_fulfilment_hours": round(sum(fulfilled_hours) / len(fulfilled_hours), 2) if fulfilled_hours else 0,
            "best_weekday": best_weekday,
            "best_hour": best_hour,
            "funnel_comparison": {
                "previous": previous_funnel,
                "change_percent": {
                    key: _change(funnel[key], previous_funnel[key])
                    for key in ("store_visits", "product_views", "cart_additions", "checkout_starts", "order_submissions")
                },
            },
            "funnel_trend": _funnel_trend(current_events, period_start_date, days, collected),
            "product_intelligence": product_performance,
            "sources": _source_performance(current_events),
            "opportunities": _opportunity_cards(product_performance, funnel),
        } if level == "advanced" else None),
    }


def capture_inventory_snapshot(store, snapshot_date=None):
    from .models import InventorySnapshot

    snapshot_date = snapshot_date or timezone.localdate()
    count = 0
    variants = StockVariant.objects.filter(store=store).select_related("product")
    for variant in variants.iterator():
        unit_price = variant.price if variant.price is not None else variant.product.price
        InventorySnapshot.objects.update_or_create(
            store=store,
            variant=variant,
            snapshot_date=snapshot_date,
            defaults={
                "product": variant.product,
                "quantity": variant.quantity,
                "reserved": variant.reserved,
                "available": variant.available_quantity(),
                "unit_price": unit_price,
                "unit_cost": variant.product.cost_price,
            },
        )
        count += 1
    return count


def purge_expired_storefront_events(retention_days, now=None):
    """Apply behavioural-event retention without touching commerce truth."""
    from .models import StorefrontEvent

    if retention_days < 30:
        raise ValueError("Event retention cannot be shorter than 30 days.")
    cutoff = (now or timezone.now()) - timedelta(days=retention_days)
    deleted, _details = StorefrontEvent.objects.filter(occurred_at__lt=cutoff).delete()
    return deleted
