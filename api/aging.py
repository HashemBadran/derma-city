"""Read-time aging.

Age is recomputed on every read against today's date, so a workbook exported on
Monday and a screen opened on Friday disagree by four days — which is correct, and
the reason nothing here is cached into the synced tables.

The aging basis is selectable (`basis`):

  'due'     — age = today - due date. An item ages only once its credit term
              lapses; anything still within terms sits in 'Not Due'.
  'invoice' — age = today - invoice date. An item enters the 1-30 band the day it
              is issued, regardless of remaining term; 'Not Due' then only catches
              the rare future-dated invoice.

Either way the "overdue" money total is measured against the due date, so the
split between money "within terms" and money "overdue" always reflects payment
terms, independent of the aging basis.

Two scopes are supported:

  'all'  — every open receivable
  'aged' — only documents at least `threshold` days old on the chosen basis
"""

from datetime import date

NOT_DUE = 'Not Due'

# Ladder of age bands. 'Not Due' sits outside it because it is not an age: on the
# 'due' basis it holds everything still inside its payment term, and on the
# 'invoice' basis only future-dated invoices (age <= 0).
LADDER = [
    (1, 30, '1-30'),
    (31, 60, '31-60'),
    (61, 90, '61-90'),
    (91, 179, '91-179'),
    (180, 269, '180-269'),
    (270, 364, '270-364'),
    (365, 545, '365-545'),
    (546, None, '546+'),
]

BAND_TITLES = {
    NOT_DUE: 'Within terms',
    '1-30': '1–30 days',
    '31-60': '1–2 months',
    '61-90': '2–3 months',
    '91-179': '3–6 months',
    '180-269': '6–9 months',
    '270-364': '9–12 months',
    '365-545': '1–1.5 years',
    '546+': 'Over 1.5 years',
}


def parse_date(s):
    y, m, d = (int(p) for p in s.split('-'))
    return date(y, m, d)


DEFAULT_BASIS = 'due'
BASES = ('due', 'invoice')


def days_overdue(due_date, as_of=None):
    """Positive when past due, zero or negative while still within terms. Always
    measured against the due date — this is what the overdue money total uses,
    regardless of the aging basis."""
    return ((as_of or date.today()) - parse_date(due_date)).days


def age_on(row, basis, as_of=None):
    """Age of one document on the chosen basis: 'due' counts from the due date,
    'invoice' from the invoice date."""
    anchor = row['inv_date'] if basis == 'invoice' else row['due_date']
    return ((as_of or date.today()) - parse_date(anchor)).days


def band_for(days):
    if days <= 0:
        return NOT_DUE
    for low, high, label in LADDER:
        if days >= low and (high is None or days <= high):
            return label
    return LADDER[-1][2]


def visible_bands(threshold, scope='aged'):
    """Which columns the view should carry."""
    if scope == 'all':
        return [NOT_DUE] + [label for _, _, label in LADDER]
    bands = [(lo, hi, label) for lo, hi, label in LADDER if hi is None or hi >= threshold]
    if not bands:
        return [f'{threshold}+']
    out = []
    for i, (lo, hi, label) in enumerate(bands):
        if i == 0 and lo < threshold:
            label = f'{threshold}-{hi}' if hi is not None else f'{threshold}+'
        out.append(label)
    return out


def band_label(band):
    return BAND_TITLES.get(band, band)


def build(conn, threshold, as_of=None, scope='aged', company_id=None,
          area=None, basis=DEFAULT_BASIS):
    """Aggregate open documents into per-customer aged positions.

    `basis` picks what the age is counted from — 'due' (from the due date) or
    'invoice' (from the invoice date); see the module docstring.

    In 'all' scope every customer with an open balance is returned, including those
    entirely within their credit terms. In 'aged' scope only documents at least
    `threshold` days old on the chosen basis are counted, and customers with none
    drop out.

    Customers whose included items net to zero or below are kept either way — an
    unapplied credit note is worth seeing, not filtering away.
    """
    as_of = as_of or date.today()
    basis = basis if basis in BASES else DEFAULT_BASIS
    include_all = scope == 'all'
    bands = visible_bands(threshold, scope)
    band_index = {b: i for i, b in enumerate(bands)}

    # Filtering on the document's company, not the customer's: a partner shared
    # between companies still splits correctly.
    where, params = [], []
    if company_id:
        where.append('d.company_id = ?')
        params.append(int(company_id))
    if area == 'unassigned':
        # Anything with no region, however it came to be blank, belongs here.
        where.append("(c.area = ? OR c.area IS NULL OR c.area = '')")
        params.append(area)
    elif area:
        where.append('c.area = ?')
        params.append(area)
    company_sql = (' WHERE ' + ' AND '.join(where)) if where else ''
    # Agency flag and note stats used to be three separate round trips
    # (agency, note count, last note) on top of this one — folded into the
    # main query as LEFT JOINs instead, since each was keyed on partner_id
    # already. One Turso round trip instead of four.
    rows = conn.execute(
        'SELECT c.partner_id, c.name, c.phone, c.mobile, c.email, c.city,'
        '       c.payment_term, c.term_days, c.credit_limit, c.area,'
        '       d.company_id, d.company,'
        '       d.line_id, d.doc, d.ref, d.journal, d.inv_date, d.due_date,'
        '       d.original, d.residual,'
        '       f.status, f.owner, f.promise_date, f.promise_amount,'
        '       f.next_action_date, f.updated_at,'
        '       (ag.partner_id IS NOT NULL) AS is_agency,'
        '       nt.note_count, nt.last_note_at'
        '  FROM customers c'
        '  JOIN documents d ON d.partner_id = c.partner_id'
        '  LEFT JOIN followups f ON f.partner_id = c.partner_id'
        '  LEFT JOIN agency ag ON ag.partner_id = c.partner_id'
        '  LEFT JOIN (SELECT partner_id, COUNT(*) AS note_count,'
        '                    MAX(created_at) AS last_note_at'
        '               FROM notes GROUP BY partner_id) nt'
        '         ON nt.partner_id = c.partner_id'
        + company_sql, params
    ).fetchall()

    customers = {}
    for r in rows:
        pid = r['partner_id']
        c = customers.get(pid)
        if c is None:
            c = customers[pid] = {
                'partner_id': pid,
                'name': r['name'],
                'phone': r['phone'] or r['mobile'] or '',
                'email': r['email'] or '',
                'city': r['city'] or '',
                'company': r['company'] or '',
                'company_id': r['company_id'] or 0,
                'area': r['area'] or 'unassigned',
                'agency': bool(r['is_agency']),
                'payment_term': r['payment_term'] or '',
                'term_days': r['term_days'],
                'credit_limit': r['credit_limit'] or 0.0,
                'status': r['status'] or 'new',
                'owner': r['owner'] or '',
                'promise_date': r['promise_date'] or '',
                'promise_amount': r['promise_amount'] or 0,
                'next_action_date': r['next_action_date'] or '',
                'updated_at': r['updated_at'] or '',
                'notes': r['note_count'] or 0,
                'last_note_at': r['last_note_at'] or '',
                'buckets': [0.0] * len(bands),
                'aged_total': 0.0,      # total of whatever this scope includes
                'overdue_total': 0.0,   # strictly past due, whatever the scope
                'not_due_total': 0.0,
                'total_open': 0.0,      # every open item, regardless of scope
                'aged_docs': 0,
                'open_docs': 0,
                'oldest_days': None,
                'oldest_due': '',
                'next_due': '',
                'documents': [],
            }

        # `age` drives the bands and the 'aged' cutoff and follows `basis`.
        # `overdue` follows the due date always, and feeds the money split below.
        age = age_on(r, basis, as_of)
        overdue = days_overdue(r['due_date'], as_of)
        residual = r['residual']
        c['total_open'] += residual
        c['open_docs'] += 1
        if overdue > 0:
            c['overdue_total'] += residual
        else:
            c['not_due_total'] += residual

        if not (include_all or age >= threshold):
            continue

        band = band_for(age)
        if band not in band_index:
            band = bands[0]
        c['buckets'][band_index[band]] += residual
        c['aged_total'] += residual
        c['aged_docs'] += 1
        if c['oldest_days'] is None or age > c['oldest_days']:
            c['oldest_days'] = age
            c['oldest_due'] = r['due_date']
        if overdue <= 0 and (not c['next_due'] or r['due_date'] < c['next_due']):
            c['next_due'] = r['due_date']
        c['documents'].append({
            'line_id': r['line_id'],
            'doc': r['doc'],
            'ref': r['ref'],
            'journal': r['journal'],
            'inv_date': r['inv_date'],
            'due_date': r['due_date'],
            'days': age,
            'overdue_days': overdue,
            'band': band,
            'original': round(r['original'], 2),
            'residual': round(residual, 2),
        })

    included = []
    for c in customers.values():
        if c['aged_docs'] == 0:
            continue
        c['buckets'] = [round(v, 2) for v in c['buckets']]
        for key in ('aged_total', 'overdue_total', 'not_due_total', 'total_open'):
            c[key] = round(c[key], 2)
        if c['oldest_days'] is None:
            c['oldest_days'] = 0
        c['over_limit'] = bool(c['credit_limit']) and c['total_open'] > c['credit_limit']
        # Owes nothing overall, yet still has documents in the aged bands: an old
        # invoice and an unapplied credit that cancel out. Real in the ledger,
        # but there is nothing to collect, so it must not read as money owed.
        c['settled'] = round(c['total_open'], 2) == 0 and c['aged_docs'] > 0
        c['documents'].sort(key=lambda d: -d['days'])
        included.append(c)

    included.sort(key=lambda c: -c['aged_total'])

    totals = {
        'bands': bands,
        'band_totals': [
            round(sum(c['buckets'][i] for c in included), 2) for i in range(len(bands))
        ],
        'aged_total': round(sum(c['aged_total'] for c in included), 2),
        'overdue_total': round(sum(c['overdue_total'] for c in included), 2),
        'not_due_total': round(sum(c['not_due_total'] for c in included), 2),
        'total_open': round(sum(c['total_open'] for c in included), 2),
        'customers': len(included),
        'documents': sum(c['aged_docs'] for c in included),
        'threshold': threshold,
        'scope': scope,
        'basis': basis,
        'as_of': as_of.isoformat(),
    }
    return included, totals
