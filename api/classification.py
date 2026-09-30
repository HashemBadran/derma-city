"""Customer classification by trailing-12-month net sales.

Deliberately kept separate from the receivables aging in aging.py: a customer
can be current on every invoice and still be a weak buyer, or be sitting on
an old overdue balance while remaining the biggest account in the book. The
sales figure comes from odoo_sync.fetch_sales_ttm into the sales_ttm table;
this module only ranks and buckets whatever numbers it's handed.

Customers are ranked richest-to-poorest by trailing-12m sales, then split
into the classic ABC bands by cumulative share of total sales:

  A - the customers making up the first 80% of sales
  B - the next 15%
  C - the last 5% (but > 0 -- they bought something)
  D - nothing bought in the trailing 12 months

Percentile tiers ("Top 10% of buyers", "Weakest 30%") are a second, separate
cut of the same ranking, over buyers only -- Class D customers have no
percentile, the same way a non-buyer isn't "in the bottom 10% of buyers",
they're just not a buyer right now.
"""

import math

CLASSES = ('A', 'B', 'C', 'D')

CLASS_LABELS = {
    'A': 'Class A · first 80% of sales',
    'B': 'Class B · next 15%',
    'C': 'Class C · last 5%',
    'D': 'Class D · nothing in 12 months',
}

# Card titles for the A/B/C rollup only -- D (no purchases) isn't "buying",
# so it doesn't get a summary card.
CLASS_TITLES = {
    'A': 'Carrying the Business',
    'B': 'The Next 15%',
    'C': 'Buying, Barely',
}

TOP_TIERS = (5, 10, 20, 30, 40, 50)
WEAK_TIERS = (10, 20, 30, 40, 50, 60, 70)


def rank(sales_by_partner):
    """`sales_by_partner`: {partner_id: trailing-12m net sales}.

    Returns {partner_id: {sales_12m, sales_class, sales_rank, sales_percentile}}
    for every partner with sales > 0. A partner absent from `sales_by_partner`,
    or with a zero/negative net (an all-time customer who's since been fully
    refunded), is simply absent from the result -- callers default those to
    Class D themselves, since "no sales" isn't a rank.

    `sales_rank` is 1 for the single biggest buyer. `sales_percentile` is that
    rank's position among buyers only, 1-100 where 1 means "top 1%" -- rounded
    up, so the single best buyer in a list of 200 reads as the top 1%, not 0%.
    """
    buyers = sorted(((pid, amt) for pid, amt in sales_by_partner.items() if amt and amt > 0),
                     key=lambda kv: -kv[1])
    total = sum(amt for _, amt in buyers)
    n = len(buyers)
    out = {}
    cum_before = 0.0
    for i, (pid, amt) in enumerate(buyers):
        share_before = (cum_before / total) if total else 0.0
        if share_before < 0.80:
            cls = 'A'
        elif share_before < 0.95:
            cls = 'B'
        else:
            cls = 'C'
        cum_before += amt
        out[pid] = {
            'sales_12m': round(amt, 2),
            'sales_class': cls,
            'sales_rank': i + 1,
            'sales_percentile': math.ceil((i + 1) / n * 100),
        }
    return out


def segment_summary(customers):
    """A/B/C rollup for the classification cards: how many customers, their
    share of trailing-12m sales, and what they collectively owe -- including
    how much of that is 90+ days old by invoice date (see aging.build's
    over_days). Expects `sales_class`/`sales_12m`/`total_open`/`over_days` to
    already be on each customer dict.
    """
    total_sales = sum(c.get('sales_12m') or 0 for c in customers)
    out = {}
    for cls in ('A', 'B', 'C'):
        group = [c for c in customers if c.get('sales_class') == cls]
        sales = sum(c.get('sales_12m') or 0 for c in group)
        out[cls] = {
            'title': CLASS_TITLES[cls],
            'count': len(group),
            'sales': round(sales, 2),
            'sales_share': round(sales / total_sales * 100, 1) if total_sales else 0,
            'total_open': round(sum(c['total_open'] for c in group), 2),
            'over_90_total': round(sum((c.get('over_days') or {}).get(90, 0) for c in group), 2),
        }
    return out
