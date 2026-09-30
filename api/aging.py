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

# Alternate, flat 60/90-day ladder — some readers of the report just want even
# round-number buckets instead of the calendar-month framing above.
LADDER_60 = [
    (1, 60, '1-60'),
    (61, 120, '61-120'),
    (121, 180, '121-180'),
    (181, 270, '181-270'),
    (271, 359, '271-359'),
    (360, None, '360+'),
]

BAND_TITLES_60 = {
    NOT_DUE: 'Within terms',
    '1-60': '1–60 days',
    '61-120': '61–120 days',
    '121-180': '121–180 days',
    '181-270': '181–270 days',
    '271-359': '271–359 days',
    '360+': '360+ days',
}

# Flat 90-day ladder — quarter-sized buckets instead of 60-day or
# calendar-month ones.
LADDER_90 = [
    (1, 90, '1-90'),
    (91, 180, '91-180'),
    (181, 270, '181-270'),
    (271, 360, '271-360'),
    (361, None, '361+'),
]

BAND_TITLES_90 = {
    NOT_DUE: 'Within terms',
    '1-90': '1–90 days',
    '91-180': '91–180 days',
    '181-270': '181–270 days',
    '271-360': '271–360 days',
    '361+': '361+ days',
}


# Finest ladder: even 30-day bands all the way to a year, then two coarser
# bands past that — for readers who want to see exactly which month an item
# fell into rather than a wide 90+/180+ bucket.
LADDER_MONTHLY = [
    (1, 30, '1-30'),
    (31, 60, '31-60'),
    (61, 90, '61-90'),
    (91, 120, '91-120'),
    (121, 150, '121-150'),
    (151, 180, '151-180'),
    (181, 210, '181-210'),
    (211, 240, '211-240'),
    (241, 270, '241-270'),
    (271, 300, '271-300'),
    (301, 360, '301-360'),
    (361, 450, '361-450'),
    (451, None, '451+'),
]

BAND_TITLES_MONTHLY = {
    NOT_DUE: 'Within terms',
    '1-30': '1–30 days',
    '31-60': '1–2 months',
    '61-90': '2–3 months',
    '91-120': '3–4 months',
    '121-150': '4–5 months',
    '151-180': '5–6 months',
    '181-210': '6–7 months',
    '211-240': '7–8 months',
    '241-270': '8–9 months',
    '271-300': '9–10 months',
    '301-360': '10–12 months',
    '361-450': '1–1.25 years',
    '451+': 'Over 15 months',
}

SCHEMES = {
    'standard': {'label': 'Standard bands', 'ladder': LADDER, 'titles': BAND_TITLES},
    'sixty': {'label': '60-day bands', 'ladder': LADDER_60, 'titles': BAND_TITLES_60},
    'ninety': {'label': '90-day bands', 'ladder': LADDER_90, 'titles': BAND_TITLES_90},
    'monthly': {'label': 'Monthly bands', 'ladder': LADDER_MONTHLY, 'titles': BAND_TITLES_MONTHLY},
}
DEFAULT_SCHEME = 'standard'


def parse_date(s):
    y, m, d = (int(p) for p in s.split('-'))
    return date(y, m, d)


DEFAULT_BASIS = 'due'
BASES = ('due', 'invoice')

# Fixed milestones for the "Over N Days" KPI tiles — always read from the
# invoice date regardless of the view's own aging `basis`, the same way
# overdue_total is always read from the due date regardless of `scheme`. A
# reader asking "how much is over 240 days old" means the invoice's age, not
# whichever basis happens to be selected, and the cutoff has to be exact
# rather than snapped to whatever band scheme is active (its bands can be 90
# days wide), so these are summed straight from each document's age.
OVER_DAYS_MILESTONES = (90, 180, 240, 360)


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


def _ladder(scheme):
    return SCHEMES.get(scheme, SCHEMES[DEFAULT_SCHEME])['ladder']


def band_for(days, scheme=DEFAULT_SCHEME):
    if days <= 0:
        return NOT_DUE
    ladder = _ladder(scheme)
    for low, high, label in ladder:
        if days >= low and (high is None or days <= high):
            return label
    return ladder[-1][2]


def visible_band_ranges(threshold, scope='aged', scheme=DEFAULT_SCHEME):
    """Which columns the view should carry, as (label, low, high) triples —
    `high` is None for the open-ended last band. Lets a caller (the "Over N
    Days" KPI tiles, for one) sum whichever bands lie at or past some cutoff
    without having to parse it back out of the label text."""
    ladder = _ladder(scheme)
    if scope == 'all':
        return [(NOT_DUE, 0, 0)] + [(label, lo, hi) for lo, hi, label in ladder]
    bands = [(lo, hi, label) for lo, hi, label in ladder if hi is None or hi >= threshold]
    if not bands:
        return [(f'{threshold}+', threshold, None)]
    out = []
    for i, (lo, hi, label) in enumerate(bands):
        if i == 0 and lo < threshold:
            lo = threshold
            label = f'{threshold}-{hi}' if hi is not None else f'{threshold}+'
        out.append((label, lo, hi))
    return out


def visible_bands(threshold, scope='aged', scheme=DEFAULT_SCHEME):
    """Which columns the view should carry."""
    return [label for label, _, _ in visible_band_ranges(threshold, scope, scheme)]


def band_label(band, scheme=DEFAULT_SCHEME):
    titles = SCHEMES.get(scheme, SCHEMES[DEFAULT_SCHEME])['titles']
    return titles.get(band, band)


def build(conn, threshold, as_of=None, scope='aged', company_id=None,
          area=None, basis=DEFAULT_BASIS, scheme=DEFAULT_SCHEME):
    """Aggregate open documents into per-customer aged positions.

    `basis` picks what the age is counted from — 'due' (from the due date) or
    'invoice' (from the invoice date); see the module docstring. `scheme`
    picks which bucket ladder the age lands in (see SCHEMES) — orthogonal to
    basis: either date can be read against any of the three bucket widths.

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
    band_ranges = visible_band_ranges(threshold, scope, scheme)
    bands = [label for label, _, _ in band_ranges]
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
        'SELECT c.partner_id, c.name, c.name_en AS name_en_synced, c.phone, c.mobile,'
        '       c.email, c.city,'
        '       c.payment_term, c.term_days, c.credit_limit, c.area,'
        '       c.salesperson_id, c.salesperson,'
        '       d.company_id, d.company,'
        '       d.line_id, d.doc, d.ref, d.journal, d.inv_date, d.due_date,'
        '       d.original, d.residual,'
        '       f.status, f.owner, f.promise_date, f.promise_amount,'
        '       f.next_action_date, f.updated_at, f.salesperson_override,'
        '       f.name_en AS name_en_override,'
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
                # Odoo-synced English name (see odoo_sync.detect_name_en_field),
                # when this instance has a matching field; a local override, if
                # set, always wins for display without touching Odoo — same
                # pattern as salesperson below.
                'name_en_synced': r['name_en_synced'] or '',
                'name_en_override': r['name_en_override'] or '',
                'name_en': r['name_en_override'] or r['name_en_synced'] or '',
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
                # The salesperson synced from Odoo (res.partner.user_id) is the
                # baseline; a local override, if set, wins for display/filtering
                # without ever touching Odoo. Both are exposed so the UI can show
                # "overridden from X" and offer a reset back to the synced value.
                'salesperson_synced': r['salesperson'] or '',
                'salesperson_override': r['salesperson_override'] or '',
                'salesperson': r['salesperson_override'] or r['salesperson'] or '',
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
                'over_days': {cutoff: 0.0 for cutoff in OVER_DAYS_MILESTONES},
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
        invoice_age = age if basis == 'invoice' else age_on(r, 'invoice', as_of)
        residual = r['residual']
        c['total_open'] += residual
        c['open_docs'] += 1
        if overdue > 0:
            c['overdue_total'] += residual
        else:
            c['not_due_total'] += residual
        for cutoff in OVER_DAYS_MILESTONES:
            if invoice_age >= cutoff:
                c['over_days'][cutoff] += residual

        if not (include_all or age >= threshold):
            continue

        band = band_for(age, scheme)
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
        c['over_days'] = {cutoff: round(v, 2) for cutoff, v in c['over_days'].items()}
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
        # (low, high) per band, same order as `bands`/`band_totals` — lets a
        # reader sum "everything at least N days old" without parsing the
        # label text, since that changes shape per scheme (e.g. '271-359' vs
        # '9-10 months'). `high` is None for the open-ended last band.
        'band_ranges': [[lo, hi] for _, lo, hi in band_ranges],
        'band_totals': [
            round(sum(c['buckets'][i] for c in included), 2) for i in range(len(bands))
        ],
        'aged_total': round(sum(c['aged_total'] for c in included), 2),
        'overdue_total': round(sum(c['overdue_total'] for c in included), 2),
        'not_due_total': round(sum(c['not_due_total'] for c in included), 2),
        'total_open': round(sum(c['total_open'] for c in included), 2),
        # Keyed by cutoff as a string — JSON object keys are always strings,
        # so {180: ...} would round-trip as {"180": ...} anyway.
        'over_days': {str(cutoff): round(sum(c['over_days'][cutoff] for c in included), 2)
                      for cutoff in OVER_DAYS_MILESTONES},
        'customers': len(included),
        'documents': sum(c['aged_docs'] for c in included),
        'threshold': threshold,
        'scope': scope,
        'basis': basis,
        'scheme': scheme,
        'as_of': as_of.isoformat(),
    }
    return included, totals
