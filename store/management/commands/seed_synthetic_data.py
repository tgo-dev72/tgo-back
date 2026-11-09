"""Create an isolated, repeatable dataset for dashboard and storefront QA.

The command deliberately owns one clearly marked store. Re-running it replaces
only that store and its tenant-scoped records, leaving real merchant stores
untouched.

Usage:
    python manage.py seed_synthetic_data --merchant owner@example.com
    python manage.py seed_synthetic_data --merchant owner@example.com --orders 250 --days 180
    python manage.py seed_synthetic_data --remove
"""

from datetime import timedelta
from decimal import Decimal
from random import Random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from catalog.models import (
    CatalogSection,
    Product,
    ProductReview,
    RetailType,
    StockVariant,
)
from catalog.seeds import ensure_retail_types
from coupons.models import Coupon, CouponRedemption
from marketing.models import NewsletterCampaign, Subscriber
from orders.models import Order, OrderedProduct, OrderStatusHistory
from store.models import ClientProfile, Store, StoreCustomer, StoreMember


DEMO_SLUG = "synthetic-analytics-lab"
DEMO_NAME = "[TEST] Analytics Lab"
SYNTHETIC_EMAIL_DOMAIN = "analytics-demo.example.test"

CUSTOMERS = [
    ("Salma", "Benali", "Casablanca", "+212661100101"),
    ("Youssef", "Amrani", "Rabat", "+212661100102"),
    ("Imane", "Berrada", "Marrakech", "+212661100103"),
    ("Hamza", "Chraibi", "Fès", "+212661100104"),
    ("Lina", "Sekkat", "Tanger", "+212661100105"),
    ("Omar", "Kabbaj", "Agadir", "+212661100106"),
    ("Nora", "Bennis", "Casablanca", "+212661100107"),
    ("Reda", "Lahlou", "Rabat", "+212661100108"),
    ("Aya", "Alaoui", "Meknès", "+212661100109"),
    ("Adam", "Tazi", "Kénitra", "+212661100110"),
    ("Sara", "Fassi", "Oujda", "+212661100111"),
    ("Mehdi", "Idrissi", "El Jadida", "+212661100112"),
]

PRODUCTS = [
    ("apparel", "LAB-TEE", "Essential cotton tee", "Soft heavyweight cotton for everyday wear.", 189, 249),
    ("apparel", "LAB-HOOD", "Atlas everyday hoodie", "Brushed cotton hoodie with a relaxed silhouette.", 429, 499),
    ("apparel", "LAB-SHIRT", "Linen summer shirt", "Breathable linen shirt with a clean open collar.", 379, None),
    ("apparel", "LAB-JACKET", "Utility overshirt", "Structured overshirt with practical patch pockets.", 549, 649),
    ("footwear", "LAB-SNEAK", "Atlas street sneakers", "Cushioned low-profile sneakers for daily use.", 649, 749),
    ("footwear", "LAB-SANDAL", "Leather city sandals", "Comfortable leather sandals made for warm days.", 459, None),
    ("footwear", "LAB-RUN", "Motion running shoes", "Lightweight trainers with responsive cushioning.", 799, 899),
    ("accessories", "LAB-BAG", "Canvas commuter bag", "A roomy canvas bag with a padded inner sleeve.", 299, 349),
    ("accessories", "LAB-CAP", "Everyday embroidered cap", "A six-panel cotton cap with an adjustable strap.", 129, None),
    ("accessories", "LAB-BELT", "Classic leather belt", "Vegetable-tanned leather with a brushed buckle.", 239, 279),
    ("accessories", "LAB-BOTTLE", "Ceramic travel bottle", "Reusable insulated bottle with a ceramic finish.", 159, None),
    ("accessories", "LAB-WALLET", "Slim card wallet", "Compact leather wallet with six card slots.", 219, 259),
]

COLOR_VALUES = [
    {"value": "Black", "label": "Black", "swatch": "#16181d"},
    {"value": "Sand", "label": "Sand", "swatch": "#c6a77d"},
    {"value": "Ocean", "label": "Ocean", "swatch": "#256d85"},
    {"value": "Olive", "label": "Olive", "swatch": "#68724a"},
]


class Command(BaseCommand):
    help = "Create an isolated synthetic commerce dataset for manual QA."

    def add_arguments(self, parser):
        parser.add_argument(
            "--merchant",
            help="Email of the merchant who should own the synthetic store.",
        )
        parser.add_argument("--orders", type=int, default=180)
        parser.add_argument("--days", type=int, default=120)
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Remove the command-owned synthetic store instead of seeding it.",
        )

    def handle(self, *args, **options):
        if options["orders"] < 1 or options["orders"] > 5000:
            raise CommandError("--orders must be between 1 and 5000.")
        if options["days"] < 14 or options["days"] > 730:
            raise CommandError("--days must be between 14 and 730.")

        existing = Store.objects.filter(slug=DEMO_SLUG).first()
        if existing is not None and existing.name != DEMO_NAME:
            raise CommandError(
                f"Refusing to replace '{DEMO_SLUG}' because it is not marked as synthetic data."
            )

        if options["remove"]:
            if existing is not None:
                self._delete_demo_store(existing)
                self.stdout.write(self.style.SUCCESS(f"Removed {DEMO_NAME}."))
            else:
                self.stdout.write("No synthetic store exists.")
            return

        merchant_email = (options.get("merchant") or "").strip().lower()
        if not merchant_email:
            raise CommandError("--merchant is required when creating synthetic data.")
        merchant = (
            get_user_model()
            .objects.filter(
                email__iexact=merchant_email,
                is_staff=False,
                merchant_profile__isnull=False,
            )
            .first()
        )
        if merchant is None:
            raise CommandError("The selected account is not an eligible merchant.")

        with transaction.atomic():
            if existing is not None:
                self._delete_demo_store(existing)
            store = self._create_store(merchant)
            products, variants = self._create_catalog(store)
            customers = self._create_customers(store)
            coupons = self._create_coupons(store, products)
            self._create_orders(
                store,
                customers,
                products,
                variants,
                coupons,
                count=options["orders"],
                days=options["days"],
            )
            self._create_marketing(store, customers)

        counts = {
            "products": store.products.count(),
            "variants": store.stock_variants.count(),
            "customers": store.store_customers.count(),
            "orders": store.orders.count(),
            "coupons": store.coupons.count(),
        }
        self.stdout.write(self.style.SUCCESS(f"Synthetic data ready in {DEMO_NAME} ({DEMO_SLUG})."))
        self.stdout.write(
            "  " + ", ".join(f"{key}: {value}" for key, value in counts.items())
        )
        self.stdout.write("  Re-run this command to reset only this synthetic store.")

    @staticmethod
    def _delete_demo_store(store):
        """Respect the catalog's protected category relationship during reset."""
        store.products.all().delete()
        store.delete()

    @staticmethod
    def _create_store(merchant):
        ensure_retail_types()
        retail_type = RetailType.objects.get(key="clothing")
        store = Store.objects.create(
            slug=DEMO_SLUG,
            name=DEMO_NAME,
            retail_type=retail_type,
        )
        StoreMember.objects.create(
            user=merchant,
            store=store,
            role=StoreMember.Roles.OWNER,
        )
        config = store.config
        config.currency = "MAD"
        config.shipping_fee = Decimal("30.00")
        config.homepage_product_source = config.HomepageProductSources.FEATURED
        config.homepage_product_limit = 8
        config.save(
            update_fields=[
                "currency",
                "shipping_fee",
                "homepage_product_source",
                "homepage_product_limit",
            ]
        )
        return store

    @staticmethod
    def _create_catalog(store):
        sections = {}
        for order, (key, label) in enumerate(
            (("apparel", "Apparel"), ("footwear", "Footwear"), ("accessories", "Accessories")),
            start=1,
        ):
            sections[key] = CatalogSection.objects.create(
                store=store,
                key=key,
                label=label,
                sort_order=order,
            )

        products = []
        variants = {}
        sizes_by_section = {
            "apparel": ["S", "M", "L", "XL"],
            "footwear": ["38", "39", "40", "41", "42"],
            "accessories": ["One size"],
        }
        now = timezone.now()
        for index, (section_key, ref, name, description, price, compare_at) in enumerate(PRODUCTS):
            product = Product.objects.create(
                store=store,
                section=sections[section_key],
                ref=ref,
                name=name,
                description=description,
                price=Decimal(price),
                compare_at_price=Decimal(compare_at) if compare_at else None,
                cost_price=(Decimal(price) * Decimal("0.48")).quantize(Decimal("0.01")),
                attributes={},
                variant_options=[
                    {"key": "color", "label": "Color", "type": "color", "values": COLOR_VALUES},
                    {"key": "size", "label": "Size", "type": "text", "values": sizes_by_section[section_key]},
                ],
                tags=[section_key, "synthetic", "qa"],
                seo_title=f"{name} | Analytics Lab",
                seo_description=description,
                uses_variants=True,
                show_related_products=True,
                track_inventory=True,
                is_featured=index < 8,
                merchandising_order=index,
                rating=round(3.9 + (index % 10) / 10, 1),
                reviews_count=8 + index * 3,
            )
            Product.objects.filter(pk=product.pk).update(created_at=now - timedelta(days=index * 4))
            product_variants = []
            for color_index, color in enumerate(COLOR_VALUES[: 2 + (index % 3)]):
                for size_index, size in enumerate(sizes_by_section[section_key]):
                    quantity = 0 if (index + color_index + size_index) % 13 == 0 else 18 + ((index * 11 + size_index * 7) % 70)
                    product_variants.append(
                        StockVariant.objects.create(
                            store=store,
                            product=product,
                            attributes={"color": color["value"], "size": size},
                            sku=f"{ref}-{color['value'][:3].upper()}-{size.replace(' ', '').upper()}",
                            price=Decimal(price),
                            quantity=quantity,
                        )
                    )
            variants[product.pk] = product_variants
            products.append(product)

        for index, product in enumerate(products):
            product.related_products.set(
                [products[(index + 1) % len(products)], products[(index + 2) % len(products)]]
            )
            for review_index in range(3):
                review = ProductReview.objects.create(
                    store=store,
                    product=product,
                    customer_name=CUSTOMERS[(index + review_index) % len(CUSTOMERS)][0],
                    customer_email=f"review-{index}-{review_index}@{SYNTHETIC_EMAIL_DOMAIN}",
                    rating=4 + ((index + review_index) % 2),
                    title="Verified demo review",
                    content="Synthetic review content used to validate the storefront review layout.",
                    status=ProductReview.Status.PUBLISHED,
                    verified_purchase=True,
                )
                ProductReview.objects.filter(pk=review.pk).update(
                    created_at=now - timedelta(days=index + review_index + 2)
                )
        return products, variants

    @staticmethod
    def _create_customers(store):
        User = get_user_model()
        now = timezone.now()
        profiles = []
        for index, (first, last, city, phone) in enumerate(CUSTOMERS, start=1):
            email = f"customer-{index}@{SYNTHETIC_EMAIL_DOMAIN}"
            user, _ = User.objects.get_or_create(
                email=email,
                defaults={"first_name": first, "last_name": last},
            )
            # These records exist only to exercise customer relationships and
            # must never become a second set of usable test credentials.
            user.set_unusable_password()
            user.save(update_fields=["password"])
            User.objects.filter(pk=user.pk).update(date_joined=now - timedelta(days=index * 11))
            profile, _ = ClientProfile.objects.update_or_create(
                user=user,
                defaults={
                    "phone": phone,
                    "address": f"{10 + index} Avenue Hassan II",
                    "city": city,
                    "country": "Morocco",
                    "preferred_language": ("ar", "fr", "en")[index % 3],
                },
            )
            profile.stores.add(store)
            StoreCustomer.objects.create(
                profile=profile,
                store=store,
                loyalty_points=index * 25,
            )
            profiles.append(profile)
        return profiles

    @staticmethod
    def _create_coupons(store, products):
        welcome = Coupon.objects.create(
            store=store,
            code="WELCOME10",
            discount_type=Coupon.DiscountType.PERCENTAGE,
            value=Decimal("10.00"),
            scope=Coupon.Scope.ALL_PRODUCTS,
            minimum_subtotal=Decimal("200.00"),
            maximum_discount=Decimal("100.00"),
            total_usage_limit=500,
            per_customer_usage_limit=1,
        )
        accessories = Coupon.objects.create(
            store=store,
            code="ACCESSORY50",
            discount_type=Coupon.DiscountType.FIXED,
            value=Decimal("50.00"),
            scope=Coupon.Scope.PRODUCTS,
            minimum_subtotal=Decimal("250.00"),
            total_usage_limit=100,
            per_customer_usage_limit=2,
        )
        accessories.products.set(products[-5:])
        return [welcome, accessories]

    @staticmethod
    def _create_orders(store, customers, products, variants, coupons, *, count, days):
        rand = Random(20260908)
        now = timezone.now()
        coupon_usage = {coupon.pk: 0 for coupon in coupons}
        reserved_by_variant = {}
        order_specs = []
        orders = []

        for index in range(count):
            age_days = index % days
            created_at = now - timedelta(days=age_days, hours=rand.randrange(0, 20), minutes=rand.randrange(0, 60))
            customer = customers[index % len(customers)]
            if age_days == 0:
                status = Order.Status.PENDING
            elif age_days < 3:
                status = rand.choice([Order.Status.PENDING, Order.Status.CONFIRMED])
            else:
                status = rand.choices(
                    [
                        Order.Status.DELIVERED,
                        Order.Status.SHIPPED,
                        Order.Status.CONFIRMED,
                        Order.Status.PENDING,
                        Order.Status.CANCELLED,
                    ],
                    weights=[48, 15, 12, 15, 10],
                    k=1,
                )[0]

            payment_mode = rand.choice([Order.PaymentMode.COD, Order.PaymentMode.COD, Order.PaymentMode.ONLINE])
            if payment_mode == Order.PaymentMode.COD:
                is_paid = Order.IsPaid.COD
            elif status == Order.Status.CANCELLED:
                is_paid = Order.IsPaid.FAILED
            elif status == Order.Status.PENDING:
                is_paid = rand.choice([Order.IsPaid.PENDING, Order.IsPaid.FAILED])
            else:
                is_paid = Order.IsPaid.CONFIRMED

            selected_products = rand.sample(products, k=rand.randint(1, 3))
            lines = []
            subtotal = Decimal("0.00")
            for product in selected_products:
                variant = rand.choice(variants[product.pk])
                quantity = rand.randint(1, 3)
                unit_price = variant.price or product.price
                subtotal += unit_price * quantity
                lines.append((product, variant, quantity, unit_price))

            coupon = coupons[index % len(coupons)] if index % 4 == 0 else None
            if coupon is None:
                discount = Decimal("0.00")
            elif coupon.discount_type == Coupon.DiscountType.PERCENTAGE:
                discount = min(
                    (subtotal * coupon.value / Decimal("100")).quantize(Decimal("0.01")),
                    coupon.maximum_discount,
                )
            else:
                discount = min(coupon.value, subtotal)
            shipping = Decimal("0.00") if subtotal >= Decimal("600.00") else Decimal("30.00")
            amount = subtotal - discount + shipping

            confirmed_at = created_at + timedelta(hours=2) if status in {
                Order.Status.CONFIRMED, Order.Status.SHIPPED, Order.Status.DELIVERED
            } else None
            shipped_at = created_at + timedelta(days=1) if status in {
                Order.Status.SHIPPED, Order.Status.DELIVERED
            } else None
            delivered_at = created_at + timedelta(days=3) if status == Order.Status.DELIVERED else None
            cancelled_at = created_at + timedelta(hours=4) if status == Order.Status.CANCELLED else None

            order = Order(
                store=store,
                client=customer,
                amount=amount,
                subtotal=subtotal,
                shipping_amount=shipping,
                discount_amount=discount,
                coupon_code=coupon.code if coupon else "",
                coupon_discount_type=coupon.discount_type if coupon else "",
                coupon_value=coupon.value if coupon else None,
                applied_coupon_id=coupon.pk if coupon else None,
                currency="MAD",
                payment_mode=payment_mode,
                is_paid=is_paid,
                status=status,
                transaction_id=f"SYNTH-{index + 1:05d}" if payment_mode == Order.PaymentMode.ONLINE else "",
                tracking_number=f"QA{index + 1:08d}" if status in {Order.Status.SHIPPED, Order.Status.DELIVERED} else "",
                carrier="Synthetic Express" if status in {Order.Status.SHIPPED, Order.Status.DELIVERED} else "",
                cancelled_by=Order.CancelledBy.CUSTOMER if status == Order.Status.CANCELLED else "",
                cancel_reason="Synthetic cancellation for QA." if status == Order.Status.CANCELLED else "",
                cancelled_at=cancelled_at,
                marketing_consent=index % 3 != 0,
                customer_name=f"{customer.user.first_name} {customer.user.last_name}",
                customer_email=customer.user.email,
                customer_phone=str(customer.phone),
                customer_city=customer.city,
                customer_address=customer.address,
                confirmed_at=confirmed_at,
                shipped_at=shipped_at,
                delivered_at=delivered_at,
            )
            orders.append(order)
            order_specs.append({
                "order": order,
                "created_at": created_at,
                "status": status,
                "is_paid": is_paid,
                "customer": customer,
                "coupon": coupon,
                "discount": discount,
                "confirmed_at": confirmed_at,
                "shipped_at": shipped_at,
                "delivered_at": delivered_at,
                "cancelled_at": cancelled_at,
                "lines": lines,
            })

        Order.objects.bulk_create(orders, batch_size=500)
        order_lines = []
        histories = []
        history_dates = []
        redemptions = []
        redemption_dates = []
        for spec in order_specs:
            order = spec["order"]
            order.created_at = spec["created_at"]
            order.updated_at = spec["created_at"]
            status = spec["status"]
            is_paid = spec["is_paid"]
            for product, variant, quantity, unit_price in spec["lines"]:
                is_pending = status == Order.Status.PENDING and is_paid != Order.IsPaid.FAILED
                order_lines.append(OrderedProduct(
                    order=order,
                    store=store,
                    product_id=product.pk,
                    variant_id=variant.pk,
                    section_key=product.section.key,
                    ref=product.ref,
                    name=product.name,
                    attributes=variant.attributes,
                    quantity=quantity,
                    fulfilled_quantity=quantity,
                    reserved_quantity=quantity if is_pending else 0,
                    price=unit_price,
                    available=True,
                    stock_deducted=status in {
                        Order.Status.CONFIRMED,
                        Order.Status.SHIPPED,
                        Order.Status.DELIVERED,
                    },
                ))
                if is_pending:
                    reserved_by_variant[variant.pk] = reserved_by_variant.get(variant.pk, 0) + quantity

            transitions = [("", Order.Status.PENDING, spec["created_at"])]
            if spec["confirmed_at"]:
                transitions.append((Order.Status.PENDING, Order.Status.CONFIRMED, spec["confirmed_at"]))
            if spec["shipped_at"]:
                transitions.append((Order.Status.CONFIRMED, Order.Status.SHIPPED, spec["shipped_at"]))
            if spec["delivered_at"]:
                transitions.append((Order.Status.SHIPPED, Order.Status.DELIVERED, spec["delivered_at"]))
            if spec["cancelled_at"]:
                transitions.append((Order.Status.PENDING, Order.Status.CANCELLED, spec["cancelled_at"]))
            for from_status, to_status, changed_at in transitions:
                histories.append(OrderStatusHistory(
                    order=order,
                    from_status=from_status,
                    to_status=to_status,
                    notes="Synthetic lifecycle event.",
                ))
                history_dates.append(changed_at)

            coupon = spec["coupon"]
            if coupon is not None:
                redemption_status = (
                    CouponRedemption.Status.RELEASED
                    if status == Order.Status.CANCELLED or is_paid == Order.IsPaid.FAILED
                    else CouponRedemption.Status.CONSUMED
                )
                redemptions.append(CouponRedemption(
                    coupon=coupon,
                    order=order,
                    customer_key=spec["customer"].user.email,
                    discount_amount=spec["discount"],
                    status=redemption_status,
                    consumed_at=spec["created_at"] + timedelta(hours=2)
                    if redemption_status == CouponRedemption.Status.CONSUMED else None,
                    released_at=spec["created_at"] + timedelta(hours=2)
                    if redemption_status == CouponRedemption.Status.RELEASED else None,
                ))
                redemption_dates.append(spec["created_at"])
                if redemption_status == CouponRedemption.Status.CONSUMED:
                    coupon_usage[coupon.pk] += 1

        Order.objects.bulk_update(orders, ["created_at", "updated_at"], batch_size=500)
        OrderedProduct.objects.bulk_create(order_lines, batch_size=500)
        OrderStatusHistory.objects.bulk_create(histories, batch_size=500)
        for history, created_at in zip(histories, history_dates):
            history.created_at = created_at
        OrderStatusHistory.objects.bulk_update(histories, ["created_at"], batch_size=500)
        CouponRedemption.objects.bulk_create(redemptions, batch_size=500)
        for redemption, created_at in zip(redemptions, redemption_dates):
            redemption.created_at = created_at
        CouponRedemption.objects.bulk_update(redemptions, ["created_at"], batch_size=500)

        all_variants = {variant.pk: variant for rows in variants.values() for variant in rows}
        changed_variants = []
        for variant_id, reserved in reserved_by_variant.items():
            variant = all_variants[variant_id]
            variant.quantity = max(variant.quantity, reserved + 12)
            variant.reserved = reserved
            changed_variants.append(variant)
        StockVariant.objects.bulk_update(changed_variants, ["quantity", "reserved"], batch_size=500)
        for coupon in coupons:
            coupon.usage_count = coupon_usage[coupon.pk]
        Coupon.objects.bulk_update(coupons, ["usage_count"])

    @staticmethod
    def _create_marketing(store, customers):
        now = timezone.now()
        for index, customer in enumerate(customers):
            subscriber = Subscriber.objects.create(
                store=store,
                email=customer.user.email,
                status=Subscriber.Status.BOUNCED if index == 10 else (
                    Subscriber.Status.UNSUBSCRIBED if index == 11 else Subscriber.Status.ACTIVE
                ),
                unsubscribed_at=now - timedelta(days=3) if index == 11 else None,
            )
            Subscriber.objects.filter(pk=subscriber.pk).update(
                subscribed_at=now - timedelta(days=(index + 1) * 5)
            )

        campaigns = [
            ("New arrivals are here", NewsletterCampaign.Status.SENT, 10, 10, 0, 21),
            ("Weekend collection", NewsletterCampaign.Status.SENT, 10, 9, 1, 7),
            ("September preview", NewsletterCampaign.Status.SCHEDULED, 10, 0, 0, 0),
            ("Loyal customer offer", NewsletterCampaign.Status.DRAFT, 10, 0, 0, 0),
        ]
        for index, (subject, status, recipients, sent, failed, age_days) in enumerate(campaigns):
            campaign = NewsletterCampaign.objects.create(
                store=store,
                subject=subject,
                preview_text="Synthetic campaign used for dashboard QA.",
                body_html="<p>This is synthetic content for interface testing.</p>",
                body_text="This is synthetic content for interface testing.",
                status=status,
                scheduled_at=now + timedelta(days=2) if status == NewsletterCampaign.Status.SCHEDULED else None,
                sent_at=now - timedelta(days=age_days) if status == NewsletterCampaign.Status.SENT else None,
                recipients_count=recipients,
                sent_count=sent,
                failed_count=failed,
            )
            NewsletterCampaign.objects.filter(pk=campaign.pk).update(
                created_at=now - timedelta(days=age_days + index + 1)
            )
