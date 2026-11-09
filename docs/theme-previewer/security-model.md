# Security model

Trust boundaries:

- Theme eligibility comes from the server-side design registry.
- Public URLs come only from administrator-managed `ThemeDemoStore` records and must be HTTPS.
- A demo tenant cannot be created through public APIs; the model is only registered in Django admin and the management command.
- All non-safe HTTP methods for a registered demo tenant return `403 theme_demo_read_only`, including direct API calls that bypass the UI.
- Demo payment availability is always false and checkout is never initiated.
- Demo stores have no merchant memberships or payment keys and use synthetic data only.
- Normal storefronts retain `frame-ancestors 'none'` and `X-Frame-Options: DENY`. Registered demos omit X-Frame-Options and allow only configured showroom origins through CSP.
- The showroom iframe target is server-resolved, HTTPS-only and sandboxed without forms, popups or top navigation.
- Production source maps remain disabled. Browser-delivered JavaScript, HTML and CSS are still inspectable; credentials and authorization never rely on obscurity.

The middleware uses the same trusted tenant resolution as storefront APIs, so subdomain and `X-Store-Slug` requests have identical policy. Safe reads remain public. Cart contents remain browser-session state and never create server records.
