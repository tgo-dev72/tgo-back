# TijaraGO store mailbox Worker

This Email Worker parses mail addressed to `<store-slug>@mail.tijarago.com` and
posts it to the signed Django ingestion endpoint. Unknown store aliases are
rejected; transient API failures are retried by Cloudflare.

## Configure

1. Set the same strong random value in Django `MAILBOX_INBOUND_SECRET` and the
   Worker secret:

   ```sh
   npx wrangler secret put INBOUND_SECRET
   ```

2. Confirm `API_URL` in `wrangler.jsonc`, then deploy with `npm run deploy`.
3. Set Django `CLOUDFLARE_EMAIL_WORKER_NAME=tijarago-store-mail`.
4. In Resend, register
   `https://api.tijarago.com/api/internal/mail/resend/events/` for `email.sent`,
   `email.delivered`, `email.delivery_delayed`, `email.bounced`,
   `email.complained`, and `email.failed`; save its `whsec_...` value as
   `RESEND_WEBHOOK_SECRET`.
5. Activate each store from Dashboard → Mailbox. The backend creates a literal
   Cloudflare routing rule targeting this Worker.

Inbound attachments are limited by the backend to 10 MB each and 20 MB per
message. Executable attachment MIME types are discarded. Stored conversations
default to a 90-day retention period and can be purged with:

```sh
python manage.py purge_store_mail
```
