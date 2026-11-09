# Testing report

Targeted automated coverage includes registry publication rules, unavailable direct routes, immutable demo tenants before and after publication, normal-tenant isolation, CSP differences and idempotent provisioning without URL loss. Storefront typechecks/builds cover the runtime demo contract, hidden mutation surfaces and checkout gate. Showroom checks cover trusted resolution and responsive controls.

Required release verification:

```bash
# platform-server
python manage.py makemigrations --check
python manage.py check
python manage.py test theme_previewer

# design-stock
npm run test:storefront-core
npm run test:showroom
npm --prefix apps/showroom run typecheck --workspaces=false
npm --prefix apps/showroom run build --workspaces=false
npm run typecheck
npm run build
```

Browser validation must cover desktop/tablet/mobile, EN/FR/AR and RTL, collection/search/product/variant/cart behavior, checkout blocking, direct POST rejection, new-tab preview and selected-theme continuation. Public browser validation cannot be completed until demo deployments, HTTPS hostnames and DNS exist. Record executed results and reviewed screenshot paths here during release review; do not treat this document as evidence for commands not yet run.

## Local implementation validation — 2026-10-25

- Django migration drift check: passed (`No changes detected`).
- Django system check: passed.
- `theme_previewer` tests: 8 passed, including mutation paths, CSP, URL validation, premium provisioning and idempotency.
- Showroom tests: 24 passed.
- Storefront-core tests: 44 passed.
- All five storefront TypeScript checks and production builds: passed.
- Showroom TypeScript check and production build: passed.
- Desktop 1440×1000 and narrow 430×900 browser proof: passed against a temporary SQLite demo tenant and the real Basic renderer. Reviewed files: `/tmp/tijarago-theme-previewer-desktop.png` and `/tmp/tijarago-theme-previewer-mobile.png`.

The local browser pass found and fixed nested Vite proxy asset routing and the provisioned variant-option schema before completion. Public DNS/Vercel iframe verification, production gallery captures, tablet mode, interactive click-through automation and explicit FR/AR/RTL browser passes remain release-review work.
