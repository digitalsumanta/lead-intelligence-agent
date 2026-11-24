# Sending domain setup (Resend)

Cold outreach lands in spam, or gets your main domain blocklisted, unless the sending domain is authenticated and warmed up. Do this before real campaign volume. Most of it happens in your DNS provider and the Resend dashboard, not in this application.

## 1. Use a separate sending subdomain

Send from a subdomain such as `outreach.mindartdigital.com` rather than `mindartdigital.com` itself. If outreach reputation suffers, your normal company e-mail (proposals, invoices, replies to clients) is not affected.

## 2. Add and verify the domain in Resend

1. In Resend, go to **Domains → Add domain** and enter the subdomain.
2. Resend shows the exact DNS records to create, typically:
   - an **MX** record and an **SPF TXT** record on a `send.` host, which handle bounces and authorize Resend to send
   - a **DKIM TXT** record (`resend._domainkey`), which signs every message
3. Create those records at your DNS provider **exactly as shown**, then click **Verify**. Propagation usually takes minutes, occasionally a few hours.

Copy values from the Resend dashboard rather than from any guide: they are specific to your account.

## 3. Add a DMARC policy

Create a TXT record on `_dmarc.mindartdigital.com` (it covers the subdomain too). Start in monitoring mode:

```
v=DMARC1; p=none; rua=mailto:dmarc@mindartdigital.com; adkim=r; aspf=r
```

After two to four weeks of clean reports, tighten it to `p=quarantine`, and later `p=reject`. Gmail and Yahoo require SPF, DKIM and DMARC for bulk senders.

## 4. Point the application at the verified domain

In `.env`:

```
OUTREACH_RESEND_FROM=Your Name <you@outreach.mindartdigital.com>
```

Then restart the backend and worker (`docker compose up -d --force-recreate backend outbox_worker`).

Make sure replies reach a mailbox that someone reads, and that `unsubscribe@mindartdigital.com` exists: every e-mail tells recipients to write there.

## 5. Delivery webhooks (when the backend is publicly reachable)

So that bounces, complaints and unsubscribes are added to the suppression list automatically:

1. In Resend, go to **Webhooks → Add endpoint** with the URL `https://<your-public-api>/api/sales-supervisor/webhooks/delivery/resend`. Select the bounced, complained and delivered events.
2. Copy the signing secret into `.env` as `OUTREACH_RESEND_WEBHOOK_SECRET=…` and restart.

Until then, check bounces and complaints in the Resend dashboard and add those addresses under **Settings → Suppression list**.

## 6. Warm up gradually

A new domain has no reputation. Increase volume slowly and only to well-researched, relevant prospects:

| Week | Approx. e-mails per day |
| --- | --- |
| 1 | 10–20 |
| 2 | 20–40 |
| 3 | 40–80 |
| 4+ | grow about 20–30% a week, if bounce and complaint rates stay low |

Pause and investigate if bounces exceed about 2% or spam complaints exceed 0.1% (Gmail's hard limit is 0.3%). Keep **canary mode** on (`OUTREACH_CANARY_MODE=true`) while you test with your own addresses. Turn it off only when the domain is verified and you are ready for real recipients.

## Checklist

- [ ] Subdomain added and verified in Resend (SPF, DKIM, MX)
- [ ] DMARC TXT record on `_dmarc.mindartdigital.com`
- [ ] `OUTREACH_RESEND_FROM` uses the verified subdomain
- [ ] Reply mailbox and `unsubscribe@mindartdigital.com` exist and are monitored
- [ ] Test e-mails to your own Gmail and Outlook accounts land in the inbox and show "signed by" and "mailed-by" for your domain
- [ ] Warm-up plan agreed; canary mode stays on until then
