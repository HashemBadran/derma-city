# Derma City — Receivables & Collections Tracker

A web app for **Derma City only**. It tracks what customers owe, chases what is
overdue, and reports the cash actually collected. It reads live from Odoo and
keeps your follow-up notes locally.

This is a fork of the combined [Derma City + Mastery Avenue tracker](https://github.com/HashemBadran/Collections)
scoped down to a single company — `company_ids` in `api/config.json` lists only
Derma City's Odoo company id, so Mastery Avenue's data is never pulled from Odoo
in the first place, not merely filtered out of the view.

## Deployed on Vercel

This app runs as a Vercel Function (Python) with a static frontend, backed by a
[Turso](https://turso.tech) (libSQL/SQLite-compatible) database instead of a local
file — Vercel functions have no persistent disk, so `tracker.db` can't live on the
server the way it does for a local run. It is public with no login, so anyone with
the URL can view it; the **sync endpoint** is the one thing kept from being wide
open (see below), since it talks to the real Odoo server.

```
api/          Python backend — one Vercel Function (api/index.py) handling all /api/*
public/       Frontend — served directly by Vercel, no server involved
scripts/      One-time local-to-Turso migration for your existing tracker.db
vercel.json   Routes /api/* to the function; 60s max duration
requirements.txt
```

### First-time setup

1. **Turso** — create a database and grab its URL + token. Location is set on the
   **group**, not the database — Turso's default group for a new account is
   wherever their infra happens to put it (Ireland, in practice), and every
   database created into that group inherits it regardless of what you pick in
   the create-database dialog. Vercel Functions on this project run in
   Washington D.C. (`iad1`), so create a *group* in a US East location first
   (`turso group create us-east --location iad` or the dashboard's "Create
   Group"), then create the database inside it — every query is a network round
   trip, and matching regions cut page-load time roughly 3x versus leaving the
   database on the other side of the Atlantic from the function that queries it.
   ```bash
   turso group create us-east --location iad
   turso db create derma-city-tracker --group us-east
   turso db show derma-city-tracker --url
   turso db tokens create derma-city-tracker
   ```
2. **Migrate your existing notes/statuses** (skip this for a brand-new install):
   ```bash
   TURSO_DATABASE_URL=<url> TURSO_AUTH_TOKEN=<token> \
     python scripts/migrate_to_turso.py tracker.db
   ```
3. **Vercel** — link the project and set environment variables (Project Settings →
   Environment Variables, or `vercel env add`):
   - `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` — from step 1
   - `ODOO_URL`, `ODOO_DB`, `ODOO_USER`, `ODOO_PASSWORD` — from your Odoo instance;
     an [API key](https://www.odoo.com/documentation/latest/developer/reference/external_api.html#api-keys)
     is safer than your account password here
   - `CRON_SECRET` — any random string; protects the sync endpoint (see below)

   Then deploy:
   ```bash
   vercel --prod
   ```
4. **Scheduled sync** — Vercel's free plan only runs cron jobs once a day, so the
   recurring refresh lives in `.github/workflows/sync.yml` instead (every 20
   minutes, GitHub Actions is free for this). In your GitHub repo, add two
   **Actions secrets**: `APP_URL` (your deployed URL, no trailing slash) and
   `CRON_SECRET` (the same value you set in Vercel). Once pushed, it runs on its
   own — check the Actions tab to confirm it's green.

### Refreshing from Odoo

Two mechanisms, same endpoint:

- **Automatic** — the GitHub Actions workflow above hits `POST /api/sync` with the
  cron secret every 20 minutes, so the data on screen is never more than about
  that far behind Odoo.
- **Manual** — the "Refresh from Odoo" button in the header calls the same
  endpoint. It runs the sync inline and waits for it (there is no background
  process left to poll once a serverless function responds), so it can take up to
  a minute; it is throttled to once every two minutes so the public page can't be
  used to hammer the real Odoo server.

### Local development

```bash
vercel dev
```

runs the whole thing — frontend and API — exactly as it behaves in production. Set
`TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN` in a `.env.local` to point at your Turso
database, or leave them unset to fall back to a local `tracker.db` file, same as
before.

`python api/index.py --sync` still works for a one-off sync from the command line,
and `python api/index.py --port 5050` runs just the API standalone (without the
frontend) for backend debugging.

## What it shows

Two views, switched with the toggle in the header:

- **All open** (the default) — every customer with an open receivable, aged across the
  full ladder from *Within terms* through *Over 1.5 years*. This is the whole book:
  675 customers, and it shows what is still inside its credit period alongside what
  has gone past due.
- **Overdue only** — just the customers with items past due by the number of days in
  the box beside the toggle. The age bands adjust to match, so at 365 days you get
  "1–1.5 years" and "over 1.5 years" only.

The choice is remembered between sessions.

The top row gives the headline numbers: total receivable, how much is within terms,
how much is overdue and what share of the book that is. The **aging distribution** bar
below breaks the same total across every band — click any segment or legend entry to
filter the table to it, click again to clear.

Under that, two alert cards appear when they have something to report: **broken
promises** (customers marked "Promised to Pay" whose date has passed) and
**follow-ups due** (anyone whose next-action date has arrived).

The table lists one row per customer, largest first. Click any column heading to
re-sort, click any row to open that customer. The **Terms** column shows that
customer's credit period from Odoo, and **Oldest** shows a green "85d left" when
everything they owe is still within terms. A customer over their Odoo credit limit has
their name in red.

Filters include credit terms, age band, status, owner, a minimum amount, and three
switches: **Has overdue**, **Hide credits & zero**, **Needs action**.

## Collections

The **Collections** tab tracks cash actually received: daily totals, per
salesperson and per customer.

A collection is a credit posted to a receivable account from a bank or cash
journal. Two things had to be handled for the numbers to mean anything:

- **The "Customers Opening Balance" journal is typed as a bank journal in Odoo**
  but carries migrated balances, not money received. It is excluded — counting it
  would report 163.8M collected instead of 58.1M.
- **A receipt has no salesperson of its own.** Credit comes from the invoices the
  receipt is reconciled against: `account.partial.reconcile` gives the exact
  amount applied to each invoice, and each invoice carries its salesperson. A
  payment split across three invoices from two salespeople is split the same way
  here, so nothing is double-counted or rounded to a single name.

Anything not reconciled to an invoice stays visible as **(unapplied)** rather than
being dropped, so the rows always add up to the cash banked.

### Total cash vs what a salesperson earned

**The headline "Collected" is always the real cash figure — every receipt banked.**
For Derma City that is 29,100,211.76 in 2026, which ties to Odoo exactly.

The selector splits that same total by what the money settled, without ever
changing what "collected" means:

| View | Collected | Receipts |
|---|---:|---:|
| **All receipts — total cash** (default) | **29,100,211.76** | 1,220 |
| Only settling 2026 invoices | 8,738,551.69 | 549 |
| Only settling opening balances | 20,361,660.07 | 776 |

The split matters for the salesperson ranking, not for the total. Balances
migrated from the old system were booked as ordinary invoices in the Customer
Invoices journal, so the journal cannot identify them — only the invoice date
can. Settling one is real cash but not a salesperson's selling, and at 20.4M it
dominates the table: "Administrator" alone holds 70% of collections when they are
left in. Switch to **Only settling 2026 invoices** to rank the team on collection
they actually earned.

The cutoff is `collections_from_invoice_date` in `config.json` — change it there
if the boundary ever moves.

### Using it

Date presets run from today through 7 days, this month, year to date, or
everything, with a custom range beside them. Filter by salesperson, bank or cash
account, and whether receipts are applied to invoices. Click a salesperson to
filter the whole view to them.

**Export Excel** gives seven sheets: Summary, Daily, By Salesperson, By Customer,
By Journal, Monthly, and full receipt detail with the invoice each amount settled.

## Tracking a customer

Clicking a row opens a panel with their phone number, a link through to their Odoo
record, every overdue document, and the follow-up controls:

- **Status** — New, Contacted, Promised to Pay, Partial Payment, Disputed, Escalated,
  Legal / Write-off, Resolved
- **Owner** — who is chasing it
- **Promised payment date and amount** — if the date passes while the status is still
  "Promised to Pay", they show up under broken promises
- **Next action date** — drives the follow-ups-due list
- **Notes** — a timestamped log; Arabic and English both work

Everything saves immediately. The panel has its own link (`#c=3251`), so you can
bookmark a customer or paste the URL to a colleague.

A sync (automatic or manual — see **Deployed on Vercel** above) re-reads every open
receivable for Derma City. It replaces the figures only — statuses, owners,
promises and notes are never touched by a sync, so your follow-up history survives.

Ages are recalculated every time you load the page, not at sync time. A customer at
269 days yesterday is at 270 today and appears on their own.

## Excel export

**Export Excel** downloads whatever the screen is currently showing — the same filters,
the same threshold. Three sheets: customer summary with follow-up columns, document
detail, and the full contact log.

## Files

| File | What it does |
|---|---|
| `api/index.py` | The Vercel Function — API routing, request handling |
| `api/odoo_sync.py` | Pulls receivables out of Odoo over XML-RPC |
| `api/db.py` | Turso/libSQL schema and connections |
| `api/aging.py` | Age bands and per-customer aggregation |
| `api/export.py` | Excel generation |
| `api/config.json` | Non-secret settings (companies, currency, thresholds) |
| `public/` | The browser front end, served directly by Vercel |
| `scripts/migrate_to_turso.py` | One-time import of an existing local tracker.db |
| `.github/workflows/sync.yml` | The scheduled Odoo sync (every 20 min) |

## Configuration

`api/config.json` holds the companies to include (`company_ids` and
`company_labels`), the default threshold, and the currency. It ships in the repo
with no credentials in it — the Odoo connection and Turso credentials come from
environment variables instead (`ODOO_URL`, `ODOO_DB`, `ODOO_USER`,
`ODOO_PASSWORD`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`), set in Vercel's
Project Settings → Environment Variables.

An Odoo API key is safer than your account password: in Odoo, **Preferences → Account
Security → New API Key**, then use that value as `ODOO_PASSWORD`.

## Notes on the numbers

Amounts are unreconciled residuals on posted journal items in receivable accounts —
what is genuinely still outstanding, not the original invoice value.

Age is measured from the invoice **due date**, matching Odoo's own aged-receivable
report. Almost every customer here is on 90-day terms, so an invoice raised 12 months
ago is around 270 days overdue, not 365. *Within terms* means the due date has not
arrived yet — those invoices are not late, they are simply unpaid.

Credit terms come from the customer's payment term in Odoo (**Sales → Payment
Terms**). The day count is read from the term's name, so "90 Days" gives 90. Terms
without a plain day count, like "End of Following Month", show their full name and
sort last. 85 customers have no term set at all and appear under "(none)".

Negative amounts are credit notes or unapplied customer credits. Customers whose
overdue items net to zero or less are still listed rather than hidden, so those credits
stay visible — tick **Hide credits & zero** to drop them from the view.

Customers appear only if they have at least one document past the threshold, so the
count on screen is smaller than the full customer list.

## Backing up

Your notes and statuses live in Turso now, not a local file. Turso keeps its own
backups, but you can also export a full copy any time:

```bash
turso db shell derma-city-tracker ".dump" > backup.sql
```

## Who can see this

The app has no login — anyone with the deployed URL can view every customer's
balance, phone number, and follow-up notes. The only thing that isn't wide open is
the sync endpoint, which requires `CRON_SECRET` (used by the scheduled GitHub
Actions job) or is otherwise throttled to once every two minutes, so the public
page can't be used to hammer the real Odoo server. If that level of exposure ever
stops being acceptable, put Vercel's password protection or a login in front of
the deployment.
