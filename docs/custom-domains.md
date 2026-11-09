# Custom storefront domains

Custom domains are disabled by default. Merchants keep the permanent
`<slug>.tijarago.com` fallback, and connect hostnames they already own from
Dashboard → Settings → Storefront.

## Lifecycle

1. Submission normalizes IDNA and creates an exact, globally unique hostname.
2. The merchant publishes the displayed `_tijarago.<hostname>` TXT challenge.
3. A queue worker verifies ownership, then attaches only that hostname to the
   store's existing Vercel project.
4. DNS instructions come from Vercel's domain configuration API. TijaraGO
   never requests registrar credentials and never edits external DNS.
5. Activation requires provider verification, public routing, a valid HTTPS
   request, and a health marker matching both store slug and deployed release.
6. A healthy hostname can become primary transactionally. Secondary verified
   aliases redirect GET navigation to it while preserving path/query/fragment.
7. Disconnect removes only the Vercel alias, immediately revokes origin trust,
   preserves merchant DNS, and restores the platform fallback when necessary.

Platform deployments always use the platform-owned fallback. Redesigns and
code deployments therefore do not need to reattach merchant domains.

## Browser/API architecture

This release uses direct `https://api.tijarago.com` access. Dynamic CORS and
CSRF trust require an exact active HTTPS hostname and are limited to customer
storefront routes. Actual requests must also carry the matching store slug;
preflight is allowed without a slug value. Admin and merchant settings routes
are never granted by a custom storefront origin. CSRF tokens remain mandatory.

Customer cookies are cross-site (`Secure; SameSite=None`). Browsers that block
third-party cookies can still prevent customer sessions. For that reason the
feature flag must remain off until the release browser matrix passes. A future
same-origin proxy must use a narrow route allowlist and a server-authenticated
tenant binding; a broad static rewrite is not an acceptable substitute.

Switching hostname does not migrate cookies or local cart storage. Existing
sessions remain on the old hostname until expiry; customers authenticate again
on the new primary. No unsafe cross-domain cookie copying is attempted.

## Configuration

```env
STOREFRONT_CUSTOM_DOMAINS_ENABLED=false
STOREFRONT_DNS_RESOLVER_URL=https://cloudflare-dns.com/dns-query
VERCEL_API_TOKEN=...
VERCEL_TEAM_ID=...
STOREFRONT_API_URL=https://api.tijarago.com
```

Apply migrations normally, then optionally queue resumable reconciliation:

```bash
python manage.py migrate
python manage.py reconcile_storefront_domains --limit 50
python manage.py reconcile_storefront_domains --store STORE_ID
```

The schema/data migration performs no network calls. Legacy custom values are
backfilled as unverified; only the dedicated lifecycle can activate them.

## Staged rollout

1. Deploy schema and code with the feature flag off.
2. Verify platform fallback deployments and run the reconciliation command in
   staging with mocked/provider sandbox data.
3. Enable for one internal canary store. Test apex and subdomain DNS, certificate
   issuance, login/signup/password reset, cart, checkout and payment returns on
   Safari, Firefox, Chrome and mobile with third-party cookies restricted.
4. Confirm unknown/pending/disconnected origins fail CORS and CSRF, admin APIs
   remain inaccessible, rapid retries are idempotent, and store redeploys retain
   aliases.
5. Enable progressively by environment only after those checks pass.

Rollback is setting `STOREFRONT_CUSTOM_DOMAINS_ENABLED=false`. Existing healthy
aliases may continue serving, while no new connection is accepted. If an alias
must be withdrawn, disconnect it in the dashboard; this does not alter merchant
DNS. The stable platform hostname remains available throughout.
