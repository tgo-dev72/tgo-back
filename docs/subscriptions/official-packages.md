# Official TijaraGO subscription packages

The authoritative runtime configuration is `subscriptions/official_plans.py`. The public catalog is served by `GET /api/subscriptions/plans`; clients must use its prices, translations and entitlement values rather than maintaining price constants. `GET /api/subscriptions/status` includes authoritative account and per-store usage for store, team, product and marketing-recipient limits.

## Packages

| Capability | Free | Starter | Growth | Business |
|---|---:|---:|---:|---:|
| Monthly price (MAD) | 0 | 79 | 149 | 249 |
| `stores.max_count` | 1 | 1 | 3 | 5 |
| `designs.premium` | false | false | true | true |
| `team.max_members` | 1 | 2 | 5 | 10 |
| `catalog.max_products` | 50 | 500 | 2,000 | 10,000 |
| `domains.custom` | false | true | true | true |
| `branding.remove_platform` | false | false | true | true |
| `invoicing.issue` | false | true | true | true |
| `analytics.level` | essential | standard | advanced | advanced |
| `marketing.email_monthly_sends` | 0 | 200 | 1,000 | 3,000 |
| `catalog.import` | false | true | true | true |

Growth is the recommended offer. All plans cover all stores owned by the subscriber, subject to `stores.max_count`.

## Free period

Free is a one-time 30-day period. `PlatformCommerceSettings.free_trial_days` remains editable by authorized platform administrators and defaults to 30. An expired or canceled Free membership is never reactivated automatically.

## Billing cycles

The current checkout contract charges a plan for its `duration_days`; the official paid records use 30 days. Annual checkout is not exposed because a single plan record cannot safely represent selectable monthly and yearly billing cycles throughout checkout, renewal, cancellation and reconciliation. The intended annual totals are Starter 790 MAD, Growth 1,490 MAD and Business 2,490 MAD, but they must remain unavailable until billing-cycle selection is represented and verified end-to-end.

## Compatibility and migration

Migration `subscriptions.0017_official_subscription_packs` creates or updates the four stable slugs and their ten entitlements. Existing `UserSubscription` and `SubscriptionPayment` rows are not rewritten. Their entitlement and price snapshots remain historical truth. The legacy `premium` offer is retained for historical foreign keys and made unavailable for new checkout. No merchant resources are deleted during downgrade.

## Enforcement

Runtime enforcement is registered in `subscriptions/policies.py` and invoked by store creation, design selection, team invitation, product creation/import, domain connection, storefront branding, invoicing, analytics and marketing campaign endpoints. Products count once regardless of variants. Team usage includes owners, members and live pending invitations. Marketing usage counts recipients over the existing rolling 30-day window and excludes transactional messages.

## Payments

Checkout accepts a server-side plan identifier, snapshots the authoritative price and entitlements, and activates only after verified provider confirmation. Webhook processing is idempotent. Free requires no payment. Failed or canceled payments do not grant access.

## Testing

Run:

```bash
venv/bin/python manage.py test subscriptions store catalog marketing orders commerce_analytics
cd ../design-stock && npm run release:verify
```

Before release, run migrations, inspect `/api/subscriptions/plans`, complete sandbox checkout canaries for Starter/Growth/Business, and confirm an existing legacy subscriber still resolves its stored snapshot.
