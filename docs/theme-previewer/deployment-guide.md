# Deployment guide

Required backend variables:

```text
THEME_PREVIEW_SIGNUP_URL=https://www.tijarago.com/signup
THEME_PREVIEW_ALLOWED_ORIGINS=https://www.tijarago.com
```

Release sequence:

1. Deploy backend code and apply the `theme_previewer` migration.
2. Run `provision_theme_demos` without `--enable`.
3. Use the existing storefront/Vercel pipeline to create one production build per demo tenant. Do not expose development servers or source maps.
4. Configure wildcard DNS for `*.preview.tijarago.com` to the approved storefront deployment target and attach each hostname in Vercel. DNS and Vercel changes are manual release actions and are not performed by this repository change.
5. Verify each HTTPS storefront uses the correct `demo-<theme>` tenant and returns demo CSP headers.
6. Run backend, frontend and browser security checks.
7. Run provisioning with the public URL template and `--enable`.
8. Run `THEME_PREVIEW_CATALOG_URL=https://api.tijarago.com/api/theme-previews/ npm run gallery:capture:previews` in `design-stock`, optimize/review captures, deploy the showroom, then smoke-test public routes.

The showroom CSP permits frames only from `https://*.preview.tijarago.com`; keep demo deployments inside that dedicated namespace. Roll back exposure by setting `is_enabled=false`. This immediately removes the URL from list/detail APIs while keeping the tenant read-only.
