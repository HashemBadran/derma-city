#!/usr/bin/env python3
"""Receivables & Collections — API.

Deployed on Vercel as a single Python Function (this file, `handler`, is the
entrypoint Vercel's Python runtime looks for) so every /api/* request is routed
here by vercel.json and dispatched below, the same way the original single-process
app.py did. The static frontend (public/) is served by Vercel directly, not by
this handler.

Locally: `python index.py` runs the same handler behind a plain ThreadingHTTPServer,
for development without a Vercel deploy.
"""

import json
import os
import re
import sys
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

# Vercel's Python runtime imports this file via importlib.util rather than
# running it as a script, so — unlike `python index.py` — this file's own
# directory is not automatically on sys.path. Without this, the sibling
# modules below (agency, aging, db, ...) fail to import in production even
# though they sit right next to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agency
import aging
import collections_data
import db
import export
import odoo_sync

# Run at import time so most cold starts pay this cost once, up front — but
# never let it take the whole function down. A Turso hiccup (bad token,
# paused database, wrong URL) used to raise here, which crashes the import
# itself and turns every route into an opaque FUNCTION_INVOCATION_FAILED
# with no diagnostic. Deferring the failure to request time means each
# request instead gets a real 503 explaining what's wrong, and the next
# cold start simply retries db.init() on its own.
DB_INIT_ERROR = None
try:
    db.init()
except Exception as exc:
    DB_INIT_ERROR = str(exc)
    traceback.print_exc()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')

with open(CONFIG_PATH, encoding='utf-8') as fh:
    CONFIG = json.load(fh)

# Env vars win over config.json, so the file can stay credential-free — required
# in production, since config.json ships in the repo.
for env_key, cfg_key in (('ODOO_URL', 'url'), ('ODOO_DB', 'db'),
                         ('ODOO_USER', 'username'), ('ODOO_PASSWORD', 'password')):
    if os.environ.get(env_key):
        CONFIG['odoo'][cfg_key] = os.environ[env_key]

# Guards the sync endpoint. The app itself is public with no login, but a sync
# hits the real Odoo server, so it must not be something anyone on the internet
# can hammer: the scheduled job authenticates with this secret, and the manual
# "Refresh from Odoo" button in the UI is throttled instead (see MIN_SYNC_GAP).
CRON_SECRET = os.environ.get('CRON_SECRET', '')
MIN_SYNC_GAP_SECONDS = 120

# Vercel Functions run in UTC. "Today" has to mean the business's calendar day
# (both companies are Saudi Arabia-registered in Odoo, and most Odoo users
# there are set to Asia/Riyadh), not the server's — otherwise "collected as of
# today" and every overdue-age calculation quietly disagree with Odoo for the
# few hours a day UTC's date has rolled over but Riyadh's hasn't yet, or vice
# versa. Every place that previously read datetime.now() for a calendar date
# reads business_today()/business_now() instead.
BUSINESS_TZ = ZoneInfo(CONFIG.get('timezone', 'Asia/Riyadh'))


def business_now():
    return datetime.now(BUSINESS_TZ)


def business_today():
    return business_now().date()


# --------------------------------------------------------------------------- helpers

def current_threshold(conn):
    raw = db.get_setting(conn, 'threshold', CONFIG.get('default_threshold_days', 270))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return CONFIG.get('default_threshold_days', 270)


def run_sync():
    """Runs the Odoo sync inline and returns the result. No background thread:
    a serverless function is torn down as soon as it responds, so the sync has to
    finish within the request instead of being handed off."""
    return odoo_sync.sync(CONFIG, progress=None)


def seconds_since_last_sync(conn):
    row = conn.execute(
        'SELECT synced_at FROM sync_log ORDER BY id DESC LIMIT 1'
    ).fetchone()
    if not row or not row['synced_at']:
        return None
    try:
        last = datetime.fromisoformat(row['synced_at'])
    except ValueError:
        return None
    return (datetime.now() - last).total_seconds()


def company_of(params, conn=None):
    """Selected company, or None for both. Remembered between sessions."""
    raw = params.get('company_id')
    if raw is None and conn is not None:
        raw = db.get_setting(conn, 'company_id', '')
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        return None
    return cid if cid in CONFIG['company_ids'] else None


def company_label(company_id, config):
    if company_id:
        return config['company_labels'].get(str(company_id), 'Company')
    return ' + '.join(config['company_labels'][str(c)] for c in config['company_ids'])


def scope_of(params, conn=None):
    scope = params.get('scope')
    if scope in ('all', 'aged'):
        return scope
    if conn is not None:
        stored = db.get_setting(conn, 'scope', '')
        if stored in ('all', 'aged'):
            return stored
    return CONFIG.get('default_scope', 'all')


def scheme_of(params, conn=None):
    scheme = params.get('scheme')
    if scheme in aging.SCHEMES:
        return scheme
    if conn is not None:
        stored = db.get_setting(conn, 'scheme', '')
        if stored in aging.SCHEMES:
            return stored
    return CONFIG.get('default_scheme', aging.DEFAULT_SCHEME)


def filter_customers(customers, params):
    """Apply the screen's filters. Kept server-side so the export matches the view."""
    q = (params.get('q') or '').strip().lower()
    status = params.get('status') or ''
    band = params.get('band') or ''
    term = (params.get('term') or '').strip()
    owner = (params.get('owner') or '').strip().lower()
    try:
        minimum = float(params.get('min') or 0)
    except ValueError:
        minimum = 0.0
    hide_credits = params.get('hide_credits') == '1'
    hide_settled = params.get('hide_settled') == '1'
    agency_mode = params.get('agency') or ''
    due_only = params.get('due_only') == '1'
    overdue_only = params.get('overdue_only') == '1'
    over_limit = params.get('over_limit') == '1'
    today = business_today().isoformat()

    out = []
    for c in customers:
        if q and q not in c['name'].lower() and q not in (c['phone'] or '').lower():
            continue
        if status and c['status'] != status:
            continue
        if owner and owner not in (c['owner'] or '').lower():
            continue
        if minimum and c['aged_total'] < minimum:
            continue
        if hide_credits and c['aged_total'] <= 0:
            continue
        if hide_settled and c.get('settled'):
            continue
        if agency_mode == 'hide' and c.get('agency'):
            continue
        if agency_mode == 'only' and not c.get('agency'):
            continue
        if band and not any(d['band'] == band for d in c['documents']):
            continue
        if term and (c['payment_term'] or '(none)') != term:
            continue
        if overdue_only and c['overdue_total'] <= 0:
            continue
        if over_limit and not c.get('over_limit'):
            continue
        if due_only:
            nxt = c['next_action_date'] or ''
            promise = c['promise_date'] or ''
            hit = (nxt and nxt <= today) or (promise and promise < today)
            if not hit:
                continue
        out.append(c)
    return out


def area_summary(customers):
    """Open and overdue per sales region, straight from the customer's res.region."""
    out = {}
    for c in customers:
        key = c.get('area') or 'unassigned'
        e = out.setdefault(key, {'area': key, 'count': 0, 'total': 0.0, 'overdue': 0.0})
        e['count'] += 1
        e['total'] += c['total_open']
        e['overdue'] += c['overdue_total']
    for e in out.values():
        e['total'] = round(e['total'], 2)
        e['overdue'] = round(e['overdue'], 2)
    return sorted(out.values(), key=lambda e: -e['total'])


def term_summary(customers):
    """Credit terms in play, with the exposure sitting behind each."""
    out = {}
    for c in customers:
        key = c['payment_term'] or '(none)'
        entry = out.setdefault(key, {
            'term': key, 'days': c['term_days'], 'count': 0, 'amount': 0.0, 'overdue': 0.0,
        })
        entry['count'] += 1
        entry['amount'] += c['total_open']
        entry['overdue'] += c['overdue_total']
    for entry in out.values():
        entry['amount'] = round(entry['amount'], 2)
        entry['overdue'] = round(entry['overdue'], 2)
    # Shortest terms first; anything without a plain day count goes last.
    return sorted(out.values(), key=lambda e: (e['days'] is None, e['days'] or 0, e['term']))


def recompute_totals(customers, totals):
    bands = totals['bands']
    return {
        **totals,
        'band_totals': [round(sum(c['buckets'][i] for c in customers), 2)
                        for i in range(len(bands))],
        'aged_total': round(sum(c['aged_total'] for c in customers), 2),
        'overdue_total': round(sum(c['overdue_total'] for c in customers), 2),
        'not_due_total': round(sum(c['not_due_total'] for c in customers), 2),
        'total_open': round(sum(c['total_open'] for c in customers), 2),
        'customers': len(customers),
        'documents': sum(c['aged_docs'] for c in customers),
    }


def status_summary(customers):
    counts = {key: {'label': label, 'count': 0, 'amount': 0.0}
              for key, label in db.STATUSES}
    for c in customers:
        entry = counts.setdefault(c['status'], {'label': c['status'], 'count': 0, 'amount': 0.0})
        entry['count'] += 1
        entry['amount'] += c['aged_total']
    for entry in counts.values():
        entry['amount'] = round(entry['amount'], 2)
    return counts


def attention_items(customers):
    """Promises that have come and gone, and follow-ups that are due."""
    today = business_today().isoformat()
    broken, due = [], []
    for c in customers:
        if c['promise_date'] and c['promise_date'] < today and c['status'] == 'promised':
            broken.append({'partner_id': c['partner_id'], 'name': c['name'],
                           'date': c['promise_date'], 'amount': c['aged_total']})
        if c['next_action_date'] and c['next_action_date'] <= today:
            due.append({'partner_id': c['partner_id'], 'name': c['name'],
                        'date': c['next_action_date'], 'amount': c['aged_total']})
    broken.sort(key=lambda x: x['date'])
    due.sort(key=lambda x: x['date'])
    return {'broken_promises': broken, 'due_actions': due}


# --------------------------------------------------------------------------- handler

class handler(BaseHTTPRequestHandler):
    server_version = 'ReceivablesTracker/2.0'

    def log_message(self, fmt, *args):
        if os.environ.get('TRACKER_VERBOSE'):
            super().log_message(fmt, *args)

    # -- plumbing ---------------------------------------------------------------
    def _send(self, code, body=b'', content_type='application/json; charset=utf-8',
              extra_headers=None):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode('utf-8'))

    def _error(self, code, message):
        self._json({'error': message}, code)

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode('utf-8'))
        except json.JSONDecodeError:
            return {}

    # Everything under /api is handled by this one function; vercel.json rewrites
    # /api/* here. Vercel's Python runtime hands the function the *rewritten*
    # destination path, not the path the browser actually requested, so the real
    # path is smuggled through as a query param (__path) by the rewrite itself —
    # see vercel.json. Locally (no rewrite involved) self.path is already correct.
    def _path(self):
        qs = parse_qs(urlparse(self.path).query)
        smuggled = qs.get('__path')
        if smuggled:
            return urlparse(smuggled[0]).path
        return urlparse(self.path).path

    # -- routes -----------------------------------------------------------------
    def do_GET(self):
        path = self._path()
        params = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        if DB_INIT_ERROR:
            return self._error(503, f'Database unavailable: {DB_INIT_ERROR}')
        try:
            if path == '/api/bootstrap':
                return self.api_bootstrap()
            if path == '/api/customers':
                return self.api_customers(params)
            if path == '/api/sync':
                return self.api_sync_status()
            if path == '/api/export.xlsx':
                return self.api_export(params)
            if path == '/api/agency':
                conn = db.connect()
                try:
                    return self._json({'customers': agency.listing(conn)})
                finally:
                    conn.close()
            if path == '/api/collections':
                return self.api_collections(params)
            if path == '/api/collections/breakdown':
                return self.api_collections_breakdown(params)
            if path == '/api/collections/receipts':
                return self.api_collections_receipts(params)
            if path == '/api/collections/export.xlsx':
                return self.api_collections_export(params)
            match = re.fullmatch(r'/api/customers/(\d+)', path)
            if match:
                return self.api_customer_detail(int(match.group(1)), params)
            return self._error(404, 'Not found')
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self._error(500, str(exc))

    def do_POST(self):
        path = self._path()
        if DB_INIT_ERROR:
            return self._error(503, f'Database unavailable: {DB_INIT_ERROR}')
        try:
            if path == '/api/sync':
                return self.api_sync_trigger()
            if path == '/api/settings':
                return self.api_settings()
            match = re.fullmatch(r'/api/agency/(\d+)', path)
            if match:
                payload = self._body()
                conn = db.connect()
                try:
                    on = agency.set_flag(conn, int(match.group(1)),
                                         bool(payload.get('agency')))
                finally:
                    conn.close()
                return self._json({'partner_id': int(match.group(1)), 'agency': on})
            match = re.fullmatch(r'/api/customers/(\d+)/followup', path)
            if match:
                return self.api_save_followup(int(match.group(1)))
            match = re.fullmatch(r'/api/customers/(\d+)/salesperson', path)
            if match:
                return self.api_set_salesperson(int(match.group(1)))
            match = re.fullmatch(r'/api/customers/(\d+)/notes', path)
            if match:
                return self.api_add_note(int(match.group(1)))
            match = re.fullmatch(r'/api/notes/(\d+)/delete', path)
            if match:
                return self.api_delete_note(int(match.group(1)))
            return self._error(404, 'Not found')
        except BrokenPipeError:
            pass
        except Exception as exc:
            traceback.print_exc()
            self._error(500, str(exc))

    do_HEAD = do_GET

    # -- api --------------------------------------------------------------------
    def api_bootstrap(self):
        conn = db.connect()
        try:
            last = conn.execute(
                'SELECT synced_at, lines, customers, total_open FROM sync_log'
                ' ORDER BY id DESC LIMIT 1'
            ).fetchone()
            payload = {
                'company': CONFIG.get('app_label', 'Receivables & Collections'),
                'companies': [{'id': cid, 'label': CONFIG['company_labels'][str(cid)]}
                              for cid in CONFIG['company_ids']],
                'company_id': db.get_setting(conn, 'company_id', '') or '',
                'currency': CONFIG.get('currency', 'SAR'),
                'threshold': current_threshold(conn),
                'scope': scope_of({}, conn),
                'scheme': scheme_of({}, conn),
                'schemes': [{'key': k, 'label': v['label']} for k, v in aging.SCHEMES.items()],
                'statuses': [{'key': k, 'label': v} for k, v in db.STATUSES],
                'has_data': db.has_data(conn),
                'last_sync': dict(last) if last else None,
                'owner': db.get_setting(conn, 'owner', ''),
                'today': business_today().isoformat(),
                'has_collections': collections_data.has_data(conn),
            }
        finally:
            conn.close()
        self._json(payload)

    def api_customers(self, params):
        conn = db.connect()
        try:
            threshold = int(params.get('threshold') or current_threshold(conn))
            scope = scope_of(params, conn)
            scheme = scheme_of(params, conn)
            company = company_of(params, conn)
            everything, totals = aging.build(conn, threshold, as_of=business_today(), scope=scope,
                                             company_id=company,
                                             area=params.get('area') or None, scheme=scheme)
        finally:
            conn.close()

        filtered = filter_customers(everything, params)
        settled = [c for c in everything if c.get('settled')]
        self._json({
            'customers': [{k: v for k, v in c.items() if k != 'documents'} for c in filtered],
            'settled': {'count': len(settled),
                        'gross': round(sum(d['residual'] for c in settled
                                           for d in c['documents']
                                           if d['residual'] > 0), 2)},
            'totals': recompute_totals(filtered, totals),
            'grand_totals': totals,
            'status_summary': status_summary(everything),
            'attention': attention_items(everything),
            'terms': term_summary(everything),
            'areas': area_summary(everything),
            'agency': {
                'count': sum(1 for c in everything if c.get('agency')),
                'balance': round(sum(c['total_open'] for c in everything
                                     if c.get('agency')), 2),
                'overdue': round(sum(c['overdue_total'] for c in everything
                                     if c.get('agency')), 2),
            },
            'band_labels': {b: aging.band_label(b, scheme) for b in totals['bands']},
        })

    def api_customer_detail(self, partner_id, params):
        conn = db.connect()
        try:
            threshold = int(params.get('threshold') or current_threshold(conn))
            scope = scope_of(params, conn)
            scheme = scheme_of(params, conn)
            everything, _ = aging.build(conn, threshold, as_of=business_today(), scope=scope,
                                        company_id=company_of(params, conn),
                                        area=params.get('area') or None, scheme=scheme)
            customer = next((c for c in everything if c['partner_id'] == partner_id), None)
            if customer is None:
                return self._error(404, 'Customer has no items in the current view')
            notes = [dict(r) for r in conn.execute(
                'SELECT id, body, author, created_at FROM notes'
                ' WHERE partner_id = ? ORDER BY created_at DESC, id DESC',
                [partner_id],
            ).fetchall()]
            contact = conn.execute(
                'SELECT phone, mobile, email, vat, city, payment_term, term_days,'
                ' credit_limit FROM customers WHERE partner_id = ?',
                [partner_id],
            ).fetchone()
            customer['agency'] = bool(conn.execute(
                'SELECT 1 FROM agency WHERE partner_id = ?', [partner_id]).fetchone())
        finally:
            conn.close()
        self._json({
            'customer': customer,
            'contact': dict(contact) if contact else {},
            'notes': notes,
            'odoo_url': f"{CONFIG['odoo']['url']}/odoo/res.partner/{partner_id}",
        })

    # ---------------------------------------------------------- collections
    def _collection_filters(self, params):
        keys = ('date_from', 'date_to', 'user_id', 'partner_id', 'journal',
                'applied', 'basis', 'q', 'company_id', 'area', 'agency')
        out = {k: params[k] for k in keys if params.get(k)}
        if 'company_id' not in out:
            conn = db.connect()
            try:
                stored = company_of({}, conn)
            finally:
                conn.close()
            if stored:
                out['company_id'] = str(stored)
        return out

    def api_collections(self, params):
        f = self._collection_filters(params)
        conn = db.connect()
        try:
            self._json({
                'totals': collections_data.totals(conn, f),
                'daily': collections_data.daily(conn, f),
                'monthly': collections_data.breakdown(conn, f, 'month'),
                'by_salesperson': collections_data.breakdown(conn, f, 'salesperson'),
                'by_customer': collections_data.breakdown(conn, f, 'customer', limit=25),
                'by_journal': collections_data.breakdown(conn, f, 'journal'),
                'by_area': collections_data.breakdown(conn, f, 'area'),
                'facets': collections_data.facets(conn),
            })
        finally:
            conn.close()

    def api_collections_breakdown(self, params):
        dim = params.get('dim', 'salesperson')
        if dim not in collections_data.DIMENSIONS:
            return self._error(400, f'Unknown dimension: {dim}')
        f = self._collection_filters(params)
        conn = db.connect()
        try:
            self._json({'dim': dim,
                        'rows': collections_data.breakdown(conn, f, dim),
                        'totals': collections_data.totals(conn, f)})
        finally:
            conn.close()

    def api_collections_receipts(self, params):
        f = self._collection_filters(params)
        conn = db.connect()
        try:
            self._json(collections_data.receipts(
                conn, f, limit=int(params.get('limit', 400)),
                offset=int(params.get('offset', 0))))
        finally:
            conn.close()

    def api_collections_export(self, params):
        f = self._collection_filters(params)
        conn = db.connect()
        try:
            wb = export.collections_workbook(
                conn, f, company_label(f.get('company_id'), CONFIG),
                CONFIG.get('currency', 'SAR'))
        finally:
            conn.close()
        import io
        buf = io.BytesIO()
        wb.save(buf)
        stamp = datetime.now().strftime('%Y-%m-%d')
        self._send(
            200, buf.getvalue(),
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            {'Content-Disposition':
             f'attachment; filename="Collections_{stamp}.xlsx"'},
        )

    def api_save_followup(self, partner_id):
        payload = self._body()
        status = payload.get('status', 'new')
        if status not in db.STATUS_KEYS:
            return self._error(400, f'Unknown status: {status}')
        try:
            promise_amount = float(payload.get('promise_amount') or 0)
        except (TypeError, ValueError):
            promise_amount = 0.0

        conn = db.connect()
        try:
            db.ensure_followup(conn, partner_id)
            conn.execute(
                'UPDATE followups SET status = ?, owner = ?, promise_date = ?,'
                ' promise_amount = ?, next_action_date = ?, updated_at = ?'
                ' WHERE partner_id = ?',
                [status,
                 (payload.get('owner') or '').strip(),
                 (payload.get('promise_date') or '').strip(),
                 promise_amount,
                 (payload.get('next_action_date') or '').strip(),
                 datetime.now().isoformat(timespec='seconds'),
                 partner_id],
            )
            if payload.get('owner'):
                db.set_setting(conn, 'owner', payload['owner'].strip())
            row = conn.execute(
                'SELECT * FROM followups WHERE partner_id = ?', [partner_id]
            ).fetchone()
        finally:
            conn.close()
        self._json({'followup': dict(row)})

    def api_set_salesperson(self, partner_id):
        """Local override of the Odoo-synced salesperson. An empty string clears
        the override and reverts the customer to whatever Odoo says on the next
        sync — Odoo itself is never written to.
        """
        payload = self._body()
        override = (payload.get('salesperson') or '').strip()
        conn = db.connect()
        try:
            db.ensure_followup(conn, partner_id)
            conn.execute(
                'UPDATE followups SET salesperson_override = ?, updated_at = ?'
                ' WHERE partner_id = ?',
                [override, datetime.now().isoformat(timespec='seconds'), partner_id],
            )
            synced = conn.execute(
                'SELECT salesperson FROM customers WHERE partner_id = ?', [partner_id]
            ).fetchone()
        finally:
            conn.close()
        self._json({
            'partner_id': partner_id,
            'salesperson_override': override,
            'salesperson_synced': (synced['salesperson'] if synced else '') or '',
            'salesperson': override or (synced['salesperson'] if synced else '') or '',
        })

    def api_add_note(self, partner_id):
        payload = self._body()
        body = (payload.get('body') or '').strip()
        if not body:
            return self._error(400, 'Note is empty')
        conn = db.connect()
        try:
            db.ensure_followup(conn, partner_id)
            cur = conn.execute(
                'INSERT INTO notes (partner_id, body, author, created_at)'
                ' VALUES (?,?,?,?)',
                [partner_id, body, (payload.get('author') or '').strip(),
                 datetime.now().isoformat(timespec='seconds')],
            )
            note_id = cur.lastrowid
            if payload.get('author'):
                db.set_setting(conn, 'owner', payload['author'].strip())
            row = conn.execute(
                'SELECT id, body, author, created_at FROM notes WHERE id = ?', [note_id]
            ).fetchone()
        finally:
            conn.close()
        self._json({'note': dict(row)})

    def api_delete_note(self, note_id):
        conn = db.connect()
        try:
            conn.execute('DELETE FROM notes WHERE id = ?', [note_id])
        finally:
            conn.close()
        self._json({'ok': True})

    def api_settings(self):
        payload = self._body()
        conn = db.connect()
        try:
            if 'threshold' in payload:
                try:
                    db.set_setting(conn, 'threshold', max(0, int(payload['threshold'])))
                except (TypeError, ValueError):
                    return self._error(400, 'Threshold must be a whole number of days')
            if 'owner' in payload:
                db.set_setting(conn, 'owner', str(payload['owner']).strip())
            if 'company_id' in payload:
                db.set_setting(conn, 'company_id', str(payload['company_id'] or ''))
            if 'scope' in payload:
                if payload['scope'] not in ('all', 'aged'):
                    return self._error(400, 'Scope must be "all" or "aged"')
                db.set_setting(conn, 'scope', payload['scope'])
            if 'scheme' in payload:
                if payload['scheme'] not in aging.SCHEMES:
                    return self._error(400, 'Unknown aging scheme')
                db.set_setting(conn, 'scheme', payload['scheme'])
            threshold = current_threshold(conn)
            scope = scope_of({}, conn)
            scheme = scheme_of({}, conn)
            company = company_of({}, conn)
        finally:
            conn.close()
        self._json({'threshold': threshold, 'scope': scope, 'scheme': scheme,
                     'company_id': company or ''})

    def api_export(self, params):
        conn = db.connect()
        try:
            threshold = int(params.get('threshold') or current_threshold(conn))
            scope = scope_of(params, conn)
            scheme = scheme_of(params, conn)
            company = company_of(params, conn)
            everything, totals = aging.build(conn, threshold, as_of=business_today(), scope=scope,
                                             company_id=company,
                                             area=params.get('area') or None, scheme=scheme)
            filtered = filter_customers(everything, params)
            wb = export.build(filtered, recompute_totals(filtered, totals),
                              company_label(company, CONFIG),
                              CONFIG.get('currency', 'SAR'))
            export.notes_sheet(wb, conn)
        finally:
            conn.close()

        import io
        buf = io.BytesIO()
        wb.save(buf)
        stamp = datetime.now().strftime('%Y-%m-%d')
        label = 'AllOpen' if scope == 'all' else f'Overdue{threshold}plus'
        if scheme != aging.DEFAULT_SCHEME:
            label += f'_{scheme}'
        name = f'Receivables_{label}_{stamp}.xlsx'
        self._send(
            200, buf.getvalue(),
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            {'Content-Disposition': f'attachment; filename="{name}"'},
        )

    # ---------------------------------------------------------- sync
    def api_sync_status(self):
        conn = db.connect()
        try:
            last = conn.execute(
                'SELECT synced_at, lines, customers, total_open FROM sync_log'
                ' ORDER BY id DESC LIMIT 1'
            ).fetchone()
        finally:
            conn.close()
        self._json({'last_sync': dict(last) if last else None})

    def api_sync_trigger(self):
        """Runs the sync and waits for it — there is no background process left to
        poll once this response is sent. The scheduled job (GitHub Actions, calling
        this with the cron secret) is the "always fresh" mechanism; this endpoint
        also backs the manual "Refresh from Odoo" button, throttled below so the
        public UI can't be used to hammer the real Odoo server."""
        is_cron = CRON_SECRET and self.headers.get('X-Cron-Secret') == CRON_SECRET
        if not is_cron:
            conn = db.connect()
            try:
                gap = seconds_since_last_sync(conn)
            finally:
                conn.close()
            if gap is not None and gap < MIN_SYNC_GAP_SECONDS:
                return self._error(
                    429, f'Synced {int(gap)}s ago — try again in '
                         f'{MIN_SYNC_GAP_SECONDS - int(gap)}s.')
        try:
            result = run_sync()
        except Exception as exc:
            traceback.print_exc()
            return self._error(502, f'Sync failed: {exc}')
        self._json({'result': result})


# --------------------------------------------------------------------------- local dev

def main():
    """Local development only — Vercel never calls this. Vercel imports `handler`
    directly and drives it per-request; there is no listening socket in production."""
    import argparse
    import webbrowser
    import threading

    parser = argparse.ArgumentParser(description='Receivables & Collections API (dev server)')
    parser.add_argument('--port', type=int, default=CONFIG.get('port', 5050))
    parser.add_argument('--sync', action='store_true', help='sync from Odoo and exit')
    parser.add_argument('--no-open', action='store_true', help='do not open a browser')
    args = parser.parse_args()

    db.init()

    if args.sync:
        result = odoo_sync.sync(CONFIG, lambda m: print('  ' + m))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler)
    url = f'http://localhost:{args.port}'
    print(f'\n  {CONFIG.get("app_label", "Receivables")} API (dev)', flush=True)
    print(f'  {url}  (frontend is served separately from public/ — see README)', flush=True)
    print('  Ctrl+C to stop\n', flush=True)
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
        server.shutdown()


if __name__ == '__main__':
    main()
