"""Seed realistic demo data for the merchant dashboard.

Existing store owners are preserved. Stores without an owner are assigned to
an explicit or existing merchant account; platform staff identities are never
silently turned into merchants.

Usage:
    python manage.py seed_demo [--orders 12] [--merchant owner@example.com]

Idempotent: skips memberships that already exist; only creates customers
that are missing; orders are created fresh each run (previous demo orders
are left untouched).
"""
import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from orders.models import Order
from orders.services import confirm_order, create_order, fail_order
from store.models import ClientProfile, Store, StoreMember

NAMES = [
    ("Salma", "Benali", "Casablanca", "+212 661-123456"),
    ("Youssef", "El Amrani", "Rabat", "+212 662-987654"),
    ("Imane", "Berrada", "Marrakech", "+212 663-555444"),
    ("Hamza", "Chraibi", "Fès", "+212 664-333222"),
    ("Lina", "Sekkat", "Tanger", "+212 665-111000"),
    ("Omar", "Kabbaj", "Agadir", "+212 666-777888"),
    ("Nora", "Bennis", "Casablanca", "+212 667-999000"),
    ("Reda", "Lahlou", "Rabat", "+212 668-222111"),
]


class Command(BaseCommand):
    help = "Seed demo merchants/customers/orders for the dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--orders", type=int, default=12)
        parser.add_argument(
            "--merchant",
            help="Merchant email used only for stores that do not already have an owner.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        merchant_email = (options.get("merchant") or "").strip().lower()
        merchant = None
        if merchant_email:
            merchant = User.objects.filter(
                email__iexact=merchant_email,
                merchant_profile__isnull=False,
                is_staff=False,
            ).first()
            if merchant is None:
                self.stderr.write("The selected identity is not an eligible merchant account — aborting.")
                return
        else:
            merchant = User.objects.filter(
                merchant_profile__isnull=False,
                is_staff=False,
            ).order_by("id").first()

        stores = Store.objects.all()
        for store in stores:
            if store.members.filter(role=StoreMember.Roles.OWNER).exists():
                continue
            if merchant is None:
                self.stderr.write(
                    f"No merchant account is available to own {store.slug} — aborting."
                )
                return
            StoreMember.objects.get_or_create(
                user=merchant,
                store=store,
                defaults={"role": StoreMember.Roles.OWNER},
            )
            self.stdout.write(f"  {merchant.email} → owner of {store.slug}")

        rand = random.Random(20260811)
        profiles = []
        for first, last, city, phone in NAMES:
            email = f"{first.lower()}.{last.lower()}@demo.com"
            user, _ = User.objects.get_or_create(
                email=email,
                defaults={"first_name": first, "last_name": last},
            )
            profile, _ = ClientProfile.objects.get_or_create(
                user=user,
                defaults={"phone": phone, "city": city, "country": "Morocco"},
            )
            for store in stores:
                profile.stores.add(store)
            profiles.append(profile)

        for store in stores:
            variants = list(store.stock_variants.all())
            products = {v.product_id for v in variants}
            if not variants:
                self.stdout.write(f"  {store.slug}: no stock to order — skipped.")
                continue

            for i in range(options["orders"]):
                profile = rand.choice(profiles)
                n_items = rand.randint(1, 3)
                items = []
                for _ in range(n_items):
                    v = rand.choice(variants)
                    items.append({
                        "product_id": v.product_id,
                        "variant_id": v.id,
                        "quantity": rand.randint(1, 3),
                    })
                mode = rand.choice([Order.PaymentMode.COD, Order.PaymentMode.ONLINE, Order.PaymentMode.ONLINE])
                order = create_order(
                    store=store,
                    customer={
                        "name": profile.user.first_name,
                        "email": profile.user.email,
                        "phone": profile.phone,
                        "city": profile.city,
                        "address": f"{i + 1} Rue du Commerce",
                    },
                    items=items,
                    payment_mode=mode,
                )
                order.client = profile
                order.save(update_fields=["client"])

                roll = rand.random()
                # COD orders are already confirmed (and stock-deducted) by
                # create_order; only online orders need an explicit payment
                # outcome transition here.
                if mode == Order.PaymentMode.COD:
                    pass
                elif roll < 0.55:
                    confirm_order(order)
                elif roll < 0.8:
                    order.is_paid = Order.IsPaid.PENDING
                    order.save(update_fields=["is_paid"])
                else:
                    fail_order(order)

            self.stdout.write(f"  {store.slug}: seeded {options['orders']} orders")

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
