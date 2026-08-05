#!/usr/bin/env python3
"""One-time migration: copy the follow-up work out of the old local tracker.db
into a fresh Turso database.

Only the tables a sync never touches are copied — followups, notes, agency,
settings. `customers` and `documents` are skipped on purpose: they are a cache of
Odoo's data, and the first sync against the new database rebuilds them from
scratch anyway. Copying stale cached rows would just risk them briefly disagreeing
with what Odoo actually has.

Usage:
    TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... python scripts/migrate_to_turso.py path/to/tracker.db
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'api'))
import db  # noqa: E402  (needs the sys.path tweak above)

TABLES = {
    'followups': ['partner_id', 'status', 'owner', 'promise_date', 'promise_amount',
                  'next_action_date', 'updated_at'],
    'notes': ['id', 'partner_id', 'body', 'author', 'created_at'],
    'agency': ['partner_id', 'name', 'source', 'matched_as', 'note', 'added_at'],
    'settings': ['key', 'value'],
}


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/migrate_to_turso.py path/to/tracker.db')
        raise SystemExit(1)
    if not os.environ.get('TURSO_DATABASE_URL'):
        print('TURSO_DATABASE_URL is not set — this would migrate into a local file,'
              ' not Turso. Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN first.')
        raise SystemExit(1)

    src_path = sys.argv[1]
    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    db.init()
    dst = db.connect()

    try:
        for table, cols in TABLES.items():
            rows = [dict(r) for r in src.execute(f'SELECT {", ".join(cols)} FROM {table}')]
            if not rows:
                print(f'{table}: nothing to migrate')
                continue
            placeholders = ', '.join(f':{c}' for c in cols)
            dst.executemany(
                f'INSERT OR REPLACE INTO {table} ({", ".join(cols)}) VALUES ({placeholders})',
                rows,
            )
            print(f'{table}: migrated {len(rows)} rows')
    finally:
        src.close()
        dst.close()

    print('\nDone. Run a sync (POST /api/sync, or the "Refresh from Odoo" button once'
          ' deployed) to populate customers and documents from Odoo.')


if __name__ == '__main__':
    main()
