# Theme previewer architecture

The previewer renders the same production storefront bundle and APIs used by merchants. A `ThemeDemoStore` row binds one ready manifest theme to one platform-owned `Store`, its public HTTPS URL, catalog revision and release state. The public API never accepts an iframe URL from a browser: `/api/theme-previews/` derives eligible themes from the authoritative design manifest and adds a URL only when the database mapping is enabled, ready and internally consistent.

The showroom route `/design/:theme/preview` resolves that API record, validates HTTPS, and embeds one storefront. Device controls resize the iframe to 1440, 768 or 390 pixels without remounting it. The storefront receives a `demo` runtime config, retains the existing session-local cart, omits write-oriented newsletter/review surfaces and replaces checkout with a registration CTA.

The demo policy is enforced independently by Django middleware. Every unsafe tenant-scoped request for any registered demo store is rejected before reaching orders, payments, customer auth, reviews, marketing, inventory or settings code. Normal merchant tenants are unchanged.

Adding a theme requires a ready registry entry, a catalog definition, provisioning, deployment, validation, screenshot capture and finally explicit enablement. No frontend theme list changes are needed.
