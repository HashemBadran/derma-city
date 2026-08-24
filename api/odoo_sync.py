"""Pulls open receivables for one company out of Odoo and into the local SQLite store.

Everything aged-related is deliberately left uncomputed here: we store the raw due
date and residual, and let the app age them at read time. That way the numbers keep
moving on their own between syncs instead of freezing at whatever the last sync saw.
"""

import re
import xmlrpc.client
from datetime import datetime

import collections_data
import db

UNASSIGNED_AREA = 'unassigned'

LINE_FIELDS = [
    'id', 'date', 'date_maturity', 'move_name', 'ref', 'name', 'journal_id',
    'debit', 'credit', 'balance', 'amount_residual', 'partner_id', 'move_id',
    'company_id',
]
PARTNER_FIELDS = ['id', 'name', 'phone', 'mobile', 'email', 'vat', 'street', 'city',
                  'property_payment_term_id', 'credit_limit', 'company_id',
                  'region_id', 'user_id']


def term_days(label):
    """Pull the credit-day count out of a payment term name, for sorting and filtering.

    Odoo stores the schedule as child records, not a single number, so the name is the
    practical source: "90 Days" -> 90, "Immediate Payment" -> 0. Terms that do not
    state a plain day count ("End of Following Month") return None and simply sort last.
    """
    if not label:
        return None
    if 'immediate' in label.lower():
        return 0
    match = re.search(r'(\d+)\s*days?', label, re.IGNORECASE)
    return int(match.group(1)) if match else None


class OdooError(RuntimeError):
    pass


class Odoo:
    def __init__(self, url, database, username, password):
        self.url = url.rstrip('/')
        self.db = database
        self.username = username
        self.password = password
        self.uid = None
        self._models = None

    def connect(self):
        try:
            common = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/common')
            version = common.version()
            self.uid = common.authenticate(self.db, self.username, self.password, {})
        except Exception as exc:
            raise OdooError(f'Could not reach {self.url}: {exc}') from exc
        if not self.uid:
            raise OdooError('Odoo rejected the credentials in config.json.')
        self._models = xmlrpc.client.ServerProxy(f'{self.url}/xmlrpc/2/object')
        return version

    def call(self, model, method, args, kwargs=None, context=None):
        payload = dict(kwargs or {})
        if context:
            payload['context'] = context
        return self._models.execute_kw(
            self.db, self.uid, self.password, model, method, args, payload
        )


def _read_or_bisect(odoo, model, ids, fields, context, say, failed):
    """Read a chunk; on fault, binary-search it to isolate the bad id(s).

    A single record the sync user can't see (typically a multi-company access
    rule) faults Odoo's *whole* batch response, with no way to tell which id
    was the culprit. Retrying one-at-a-time would isolate it but costs up to
    len(ids) extra round trips; bisecting costs only ~log2(len(ids)), which is
    the difference between a few seconds and blowing the serverless function's
    time budget when a 300-id batch has one bad apple in it.
    """
    if not ids:
        return []
    try:
        return odoo.call(model, 'read', [ids], {'fields': fields}, context=context)
    except xmlrpc.client.Fault as exc:
        if len(ids) == 1:
            failed.append(ids[0])
            say(f'  Skipped {model} {ids[0]}: no read access ({exc})')
            return []
        mid = len(ids) // 2
        return (_read_or_bisect(odoo, model, ids[:mid], fields, context, say, failed)
                + _read_or_bisect(odoo, model, ids[mid:], fields, context, say, failed))


def _fetch_all(odoo, model, domain, fields, context, page=500):
    """search_read in pages — the receivable ledger is well past a single-call limit."""
    total = odoo.call(model, 'search_count', [domain], context=context)
    rows, offset = [], 0
    while offset < total:
        batch = odoo.call(
            model, 'search_read', [domain],
            {'fields': fields, 'limit': page, 'offset': offset, 'order': 'id'},
            context=context,
        )
        if not batch:
            break
        rows.extend(batch)
        offset += len(batch)
    return rows


def sync(config, progress=None):
    """Replace the synced tables with a fresh pull. Follow-up state is untouched."""
    def say(msg):
        if progress:
            progress(msg)

    cfg = config['odoo']
    company_ids = config['company_ids']
    labels = config.get('company_labels', {})
    context = {'allowed_company_ids': company_ids}

    odoo = Odoo(cfg['url'], cfg['db'], cfg['username'], cfg['password'])
    say('Authenticating…')
    version = odoo.connect()

    say('Fetching open receivable lines…')
    domain = [
        ('parent_state', '=', 'posted'),
        ('account_id.account_type', '=', 'asset_receivable'),
        ('amount_residual', '!=', 0),
        ('company_id', 'in', company_ids),
    ]
    lines = _fetch_all(odoo, 'account.move.line', domain, LINE_FIELDS, context)
    say(f'Got {len(lines)} open lines. Fetching customer details…')

    partner_ids = sorted({l['partner_id'][0] for l in lines if l['partner_id']})
    partners = []
    restricted_partner_ids = []
    for i in range(0, len(partner_ids), 300):
        partners.extend(_read_or_bisect(
            odoo, 'res.partner', partner_ids[i:i + 300], PARTNER_FIELDS, context,
            say, restricted_partner_ids,
        ))
    if restricted_partner_ids:
        say(f'{len(restricted_partner_ids)} contact(s) skipped for access reasons: '
            f'{restricted_partner_ids} — their receivable lines are still synced under a '
            f'placeholder name, but grant the sync user read access to fix the name/details.')

    collection_rows = collections_data.sync(odoo, config, progress)

    say('Writing to local database…')
    now = datetime.now().isoformat(timespec='seconds')
    conn = db.connect()
    try:
        with conn:
            conn.execute('DELETE FROM documents')
            conn.execute('DELETE FROM customers')

            rows = []
            for p in partners:
                term = p.get('property_payment_term_id')
                term_label = term[1] if term else ''
                salesperson = p.get('user_id')
                rows.append((
                    p['id'], p.get('name') or '', p.get('phone') or '',
                    p.get('mobile') or '', p.get('email') or '', p.get('vat') or '',
                    p.get('city') or '',
                    labels.get(str(p['company_id'][0])) if p.get('company_id') else '',
                    # Odoo keeps the sales region on the customer as res.region:
                    # middle / north / south / west / east, plus export and samples.
                    (p['region_id'][1] if p.get('region_id') else UNASSIGNED_AREA),
                    term_label, term_days(term_label),
                    p.get('credit_limit') or 0.0,
                    salesperson[0] if salesperson else 0,
                    salesperson[1] if salesperson else '',
                ))
            # Placeholder rows for contacts the sync user can't read, so the
            # aging JOIN (customers JOIN documents) still picks up their open
            # balances instead of silently dropping that money from the totals.
            for pid in restricted_partner_ids:
                rows.append((
                    pid, f'(access restricted — contact #{pid})', '', '', '', '',
                    '', '', UNASSIGNED_AREA, '', None, 0.0, 0, '',
                ))
            conn.executemany(
                'INSERT INTO customers (partner_id, name, phone, mobile, email, vat,'
                ' city, company, area, payment_term, term_days, credit_limit,'
                ' salesperson_id, salesperson)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                rows,
            )
            # A receivable line with no partner cannot be chased, but it should not
            # silently vanish from the totals either.
            # Stored with an explicit area, not left blank: the display falls back
            # to "unassigned", and a filter on that value has to find this row too.
            conn.execute(
                'INSERT OR IGNORE INTO customers (partner_id, name, area)'
                ' VALUES (0, ?, ?)',
                ('(no customer assigned)', UNASSIGNED_AREA),
            )

            conn.executemany(
                'INSERT INTO documents (line_id, partner_id, company_id, company,'
                ' doc, ref, journal, inv_date, due_date, original, residual)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                [(l['id'],
                  l['partner_id'][0] if l['partner_id'] else 0,
                  l['company_id'][0] if l.get('company_id') else 0,
                  labels.get(str(l['company_id'][0]), '') if l.get('company_id') else '',
                  l.get('move_name') or '',
                  (l.get('ref') or l.get('name') or ''),
                  l['journal_id'][1] if l.get('journal_id') else '',
                  l['date'],
                  l.get('date_maturity') or l['date'],
                  round(l.get('balance') or 0.0, 2),
                  round(l.get('amount_residual') or 0.0, 2)) for l in lines],
            )

            residual = round(sum(l.get('amount_residual') or 0.0 for l in lines), 2)
            conn.execute(
                'INSERT INTO sync_log (synced_at, lines, customers, total_open)'
                ' VALUES (?,?,?,?)',
                (now, len(lines), len(partners), residual),
            )
            collections_data.write(conn, collection_rows)
            db.set_setting(conn, 'last_sync', now)
    finally:
        conn.close()

    say('Done.')
    return {
        'synced_at': now,
        'lines': len(lines),
        'customers': len(partners),
        'total_open': residual,
        'collection_rows': len(collection_rows),
        'collected': round(sum(r['amount'] for r in collection_rows), 2),
        'restricted_partners': len(restricted_partner_ids),
        'server_version': version.get('server_version'),
    }
