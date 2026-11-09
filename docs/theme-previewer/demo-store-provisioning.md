# Demo store provisioning

Run migrations, then provision all ready registry themes:

```bash
python manage.py migrate
python manage.py provision_theme_demos
```

The command creates or updates `demo-<theme>` tenants, storefront configuration, pages, theme-specific collections, products, variants and generated synthetic product art. It is transactional and idempotent. It never creates merchant memberships, customers, orders, payment credentials or subscriptions.

To provision one theme:

```bash
python manage.py provision_theme_demos --theme fashion
```

After deploying and validating every storefront, record and enable URLs explicitly:

```bash
python manage.py provision_theme_demos \
  --public-url-template 'https://{theme}.preview.tijarago.com' \
  --enable
```

Do not use `--enable` before the production build, tenant routing, iframe headers and DNS are verified. Rerunning without a URL preserves an existing URL. Synthetic catalog definitions live in `theme_previewer/catalogs.json`; a new ready theme must have an entry there before provisioning succeeds.
