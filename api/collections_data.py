"""Cash actually collected from customers, and who it belongs to.

A collection is a credit posted to a receivable account from a bank or cash
journal. Two traps in this database:

  * The "Customers Opening Balance" journal is typed as a *bank* journal in Odoo,
    but it carries migrated balances rather than money received. It is excluded —
    including it would report 163.8M collected instead of 58.1M.
  * A receipt carries no salesperson of its own. Attribution comes from the
    invoices the receipt is reconciled against (`account.partial.reconcile` gives
    the exact amount applied to each one), and from there the invoice's
    salesperson. A payment split across three invoices from two salespeople is
    split the same way here.

Whatever is not reconciled to any invoice is kept as "(unapplied)" rather than
dropped, so the rows always add up to the cash banked.
"""

import re
import xmlrpc.client
from collections import defaultdict
from datetime import datetime

import db

UNAPPLIED = '(unapplied)'
UNASSIGNED_AREA = 'unassigned'
NO_SALESPERSON = '(no salesperson)'


def clean(value):
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', str(value or '')).strip()


def _safe_read(odoo, model, ids, fields, context, page=300, say=None):
    """Batched `read` that degrades gracefully. A single record the sync user
    can't see (typically a multi-company access rule) faults Odoo's *whole*
    batch response — retry that batch one id at a time and skip only the
    specific record(s) that still fail, instead of losing everything and
    failing the entire sync over one inaccessible row.
    """
    out = []
    for i in range(0, len(ids), page):
        chunk = ids[i:i + page]
        try:
            out.extend(odoo.call(model, 'read', [chunk], {'fields': fields}, context=context))
        except xmlrpc.client.Fault:
            for rid in chunk:
                try:
                    out.extend(odoo.call(model, 'read', [[rid]], {'fields': fields}, context=context))
                except xmlrpc.client.Fault as exc:
                    if say:
                        say(f'  Skipped {model} {rid}: no read access ({exc})')
    return out


def collection_journal_ids(odoo, company_ids, context):
    """Bank and cash journals, minus the opening-balance ones."""
    journals = odoo.call('account.journal', 'search_read',
                         [[('type', 'in', ['bank', 'cash']),
                           ('company_id', 'in', company_ids)]],
                         {'fields': ['id', 'name']}, context=context)
    return [j['id'] for j in journals if 'opening' not in (j['name'] or '').lower()]


def sync(odoo, config, progress=None):
    """Pull receipts and their allocations. Returns rows ready for the table."""
    def say(msg):
        if progress:
            progress(msg)

    company_ids = config['company_ids']
    labels = config.get('company_labels', {})
    context = {'allowed_company_ids': company_ids}
    # Balances migrated into Odoo were booked as ordinary invoices in the Customer
    # Invoices journal, so the journal cannot identify them — only the date can.
    # Everything invoiced before this date is a carried-forward balance, and
    # settling one is not a salesperson's collection.
    cutoff = config.get('collections_from_invoice_date', '2026-01-01')

    say('Fetching collections…')
    journal_ids = collection_journal_ids(odoo, company_ids, context)
    domain = [('parent_state', '=', 'posted'),
              ('account_id.account_type', '=', 'asset_receivable'),
              ('credit', '>', 0),
              ('journal_id', 'in', journal_ids),
              ('company_id', 'in', company_ids)]

    total = odoo.call('account.move.line', 'search_count', [domain], context=context)
    lines, offset = [], 0
    while offset < total:
        batch = odoo.call('account.move.line', 'search_read', [domain],
                          {'fields': ['id', 'date', 'credit', 'partner_id', 'journal_id',
                                      'move_name', 'ref', 'name', 'company_id'],
                           'limit': 500, 'offset': offset, 'order': 'id'},
                          context=context)
        if not batch:
            break
        lines.extend(batch)
        offset += len(batch)
        say(f'Fetched {len(lines)} of {total} receipts…')

    if not lines:
        return []

    say('Reading customer regions…')
    # Collections can come from customers who no longer have an open balance, so
    # the receivables partner list is not enough — look them up directly.
    payer_ids = sorted({l['partner_id'][0] for l in lines if l['partner_id']})
    areas = {}
    for pt in _safe_read(odoo, 'res.partner', payer_ids, ['id', 'region_id'], context, say=say):
        areas[pt['id']] = (clean(pt['region_id'][1]) if pt.get('region_id')
                           else UNASSIGNED_AREA)

    say('Tracing receipts to the invoices they settle…')
    credit_ids = [l['id'] for l in lines]
    allocations = []
    for i in range(0, len(credit_ids), 200):
        allocations.extend(odoo.call(
            'account.partial.reconcile', 'search_read',
            [[('credit_move_id', 'in', credit_ids[i:i + 200])]],
            {'fields': ['credit_move_id', 'debit_move_id', 'amount']}, context=context))

    # allocation -> invoice line -> invoice -> salesperson
    debit_ids = sorted({a['debit_move_id'][0] for a in allocations})
    debit_to_move = {
        r['id']: (r['move_id'][0] if r['move_id'] else None)
        for r in _safe_read(odoo, 'account.move.line', debit_ids, ['id', 'move_id'],
                             context, say=say)
    }

    move_ids = sorted({v for v in debit_to_move.values() if v})
    moves = {
        r['id']: r
        for r in _safe_read(odoo, 'account.move', move_ids,
                             ['id', 'name', 'invoice_date', 'invoice_user_id', 'journal_id'],
                             context, say=say)
    }

    say('Allocating receipts to salespeople…')
    by_credit = defaultdict(list)
    for a in allocations:
        by_credit[a['credit_move_id'][0]].append(a)

    rows = []
    for l in lines:
        received = round(l['credit'], 2)
        customer = clean(l['partner_id'][1]) if l['partner_id'] else '(no customer)'
        base = {
            'line_id': l['id'],
            'company_id': l['company_id'][0] if l.get('company_id') else 0,
            'company': (labels.get(str(l['company_id'][0]), '')
                        if l.get('company_id') else ''),
            'date': l['date'],
            'month': l['date'][:7],
            'partner_id': l['partner_id'][0] if l['partner_id'] else 0,
            'customer': customer,
            'area': areas.get(l['partner_id'][0] if l['partner_id'] else 0,
                              UNASSIGNED_AREA),
            'doc': clean(l.get('move_name')),
            'ref': clean(l.get('ref') or l.get('name')),
            'journal': clean(l['journal_id'][1]) if l['journal_id'] else '',
        }

        applied_total = 0.0
        for a in by_credit.get(l['id'], []):
            move = moves.get(debit_to_move.get(a['debit_move_id'][0]), {})
            user = move.get('invoice_user_id')
            amount = round(a['amount'], 2)
            applied_total += amount
            inv_journal = clean(move['journal_id'][1]) if move.get('journal_id') else ''
            rows.append({**base,
                         'invoice': clean(move.get('name')),
                         'invoice_date': move.get('invoice_date') or '',
                         'invoice_journal': inv_journal,
                         # Settling a migrated opening balance is not a
                         # salesperson's collection; it belongs to whoever posted
                         # the migration and would otherwise dominate the ranking.
                         'opening': int(bool(move.get('invoice_date'))
                                        and move['invoice_date'] < cutoff),
                         'user_id': user[0] if user else 0,
                         'salesperson': clean(user[1]) if user else NO_SALESPERSON,
                         'applied': 1,
                         'amount': amount})

        # Reconciliation can exceed the receipt when credit notes are in the mix;
        # never let that manufacture a negative remainder.
        remainder = round(received - applied_total, 2)
        if remainder > 0.005:
            rows.append({**base, 'invoice': '', 'invoice_date': '',
                         'invoice_journal': '', 'opening': 0,
                         'user_id': 0, 'salesperson': UNAPPLIED,
                         'applied': 0, 'amount': remainder})

    return rows


def write(conn, rows):
    conn.execute('DELETE FROM collections')
    conn.executemany(
        'INSERT INTO collections (line_id, company_id, company, date, month,'
        ' partner_id, customer, area, doc,'
        ' ref, journal, invoice, invoice_date, invoice_journal, opening, user_id,'
        ' salesperson, applied, amount)'
        ' VALUES (:line_id,:company_id,:company,:date,:month,:partner_id,:customer,'
        ' :area,:doc,:ref,:journal,'
        ' :invoice,:invoice_date,:invoice_journal,:opening,:user_id,:salesperson,'
        ' :applied,:amount)',
        rows,
    )


# ------------------------------------------------------------------ analytics

DIMENSIONS = {
    'salesperson': ('user_id', 'salesperson'),
    'customer': ('partner_id', 'customer'),
    'day': ('date', 'date'),
    'month': ('month', 'month'),
    'journal': ('journal', 'journal'),
    'company': ('company_id', 'company'),
    'area': ('area', 'area'),
}


def where(filters):
    clauses, params = [], []
    if filters.get('date_from'):
        clauses.append('date >= ?'); params.append(filters['date_from'])
    if filters.get('date_to'):
        clauses.append('date <= ?'); params.append(filters['date_to'])
    if filters.get('user_id'):
        clauses.append('user_id = ?'); params.append(int(filters['user_id']))
    if filters.get('partner_id'):
        clauses.append('partner_id = ?'); params.append(int(filters['partner_id']))
    if filters.get('journal'):
        clauses.append('journal = ?'); params.append(filters['journal'])
    if filters.get('agency') == 'hide':
        clauses.append('partner_id NOT IN (SELECT partner_id FROM agency)')
    elif filters.get('agency') == 'only':
        clauses.append('partner_id IN (SELECT partner_id FROM agency)')
    if filters.get('area') == 'unassigned':
        clauses.append("(area = ? OR area IS NULL OR area = '')")
        params.append('unassigned')
    elif filters.get('area'):
        clauses.append('area = ?'); params.append(filters['area'])
    if filters.get('company_id'):
        clauses.append('company_id = ?'); params.append(int(filters['company_id']))
    if filters.get('basis') == 'real':
        clauses.append('opening = 0')
    elif filters.get('basis') == 'opening':
        clauses.append('opening = 1')
    if filters.get('applied') == 'only':
        clauses.append('applied = 1')
    elif filters.get('applied') == 'unapplied':
        clauses.append('applied = 0')
    if filters.get('q'):
        term = f"%{filters['q'].strip()}%"
        clauses.append('(customer LIKE ? OR salesperson LIKE ? OR doc LIKE ?'
                       ' OR ref LIKE ? OR invoice LIKE ?)')
        params.extend([term] * 5)
    return (' WHERE ' + ' AND '.join(clauses)) if clauses else '', params


def totals(conn, filters):
    w, p = where(filters)
    row = conn.execute(
        'SELECT ROUND(SUM(amount),2) AS total,'
        '       COUNT(DISTINCT line_id)  AS receipts,'
        '       COUNT(DISTINCT partner_id) AS customers,'
        '       COUNT(DISTINCT CASE WHEN applied THEN user_id END) AS salespeople,'
        '       COUNT(DISTINCT date)     AS days,'
        '       MIN(date) AS first_date, MAX(date) AS last_date'
        f'  FROM collections{w}', p).fetchone()
    out = {k: row[k] for k in row.keys()}
    out['total'] = out['total'] or 0.0
    un = conn.execute(
        f'SELECT ROUND(SUM(amount),2) AS v FROM collections{w}'
        + (' AND ' if w else ' WHERE ') + 'applied = 0', p).fetchone()
    out['unapplied'] = un['v'] or 0.0
    op = conn.execute(
        f'SELECT ROUND(SUM(amount),2) AS v FROM collections{w}'
        + (' AND ' if w else ' WHERE ') + 'opening = 1', p).fetchone()
    out['opening'] = op['v'] or 0.0
    out['applied'] = round(out['total'] - out['unapplied'], 2)
    out['per_day'] = round(out['total'] / out['days'], 2) if out['days'] else 0.0
    return out


def breakdown(conn, filters, dim, limit=None):
    key, label = DIMENSIONS[dim]
    w, p = where(filters)
    sql = (f'SELECT {key} AS key, MAX({label}) AS label,'
           '       ROUND(SUM(amount),2) AS total,'
           '       COUNT(DISTINCT line_id) AS receipts,'
           '       COUNT(DISTINCT partner_id) AS customers,'
           '       COUNT(DISTINCT date) AS days,'
           '       MAX(date) AS last_date'
           f'  FROM collections{w} GROUP BY {key}')
    sql += ' ORDER BY key ASC' if dim in ('day', 'month') else ' ORDER BY total DESC'
    if limit:
        sql += f' LIMIT {int(limit)}'
    rows = [dict(r) for r in conn.execute(sql, p)]
    for r in rows:
        r['total'] = r['total'] or 0.0
    return rows


def daily(conn, filters):
    return breakdown(conn, filters, 'day')


def receipts(conn, filters, limit=400, offset=0):
    w, p = where(filters)
    total = conn.execute(f'SELECT COUNT(*) AS n FROM collections{w}', p).fetchone()['n']
    rows = [dict(r) for r in conn.execute(
        'SELECT date, doc, ref, journal, customer, area, invoice, invoice_date,'
        '       salesperson, applied, amount'
        f'  FROM collections{w} ORDER BY date DESC, line_id DESC LIMIT ? OFFSET ?',
        p + [int(limit), int(offset)])]
    return {'rows': rows, 'total': total}


def facets(conn):
    def distinct(sql):
        return [dict(r) for r in conn.execute(sql)]
    return {
        'salespeople': distinct(
            'SELECT user_id AS id, MAX(salesperson) AS name, ROUND(SUM(amount),2) AS total'
            ' FROM collections GROUP BY user_id ORDER BY total DESC'),
        'companies': distinct(
            'SELECT company_id AS id, MAX(company) AS name, ROUND(SUM(amount),2) AS total'
            ' FROM collections GROUP BY company_id ORDER BY total DESC'),
        'areas': distinct(
            'SELECT area AS id, area AS name, ROUND(SUM(amount),2) AS total'
            ' FROM collections GROUP BY area ORDER BY total DESC'),
        'journals': distinct(
            'SELECT journal AS id, journal AS name, ROUND(SUM(amount),2) AS total'
            ' FROM collections GROUP BY journal ORDER BY total DESC'),
        'range': dict(conn.execute(
            'SELECT MIN(date) AS first_date, MAX(date) AS last_date'
            ' FROM collections').fetchone() or {}),
    }


def has_data(conn):
    return conn.execute('SELECT COUNT(*) AS n FROM collections').fetchone()['n'] > 0
