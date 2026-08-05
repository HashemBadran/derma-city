"""Database layer backed by Turso (libSQL) in production, a local SQLite file in dev.

Same store as before, split the same way:

  synced  (customers, documents)  — wiped and rewritten on every Odoo sync
  local   (followups, notes)      — never touched by a sync; this is the tracking work

Turso/libSQL speaks the SQLite dialect, so the schema and almost every query are
unchanged from the original sqlite3 version. What differs is the client: libsql_client
talks HTTP to a remote database (or opens a local file with the same API), and its
result objects are not sqlite3.Row, so `Row`/`Result` below reproduce that interface
(`row['col']`, `dict(row)`, `.fetchone()`, `.fetchone().lastrowid`) so the rest of the
app — aging.py, agency.py, collections_data.py, app.py — did not need to change.

Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN to point at a hosted database; leave them
unset to fall back to a local tracker.db file for local development.
"""

import os
import re
from datetime import datetime

import libsql_client

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'tracker.db')

TURSO_URL = os.environ.get('TURSO_DATABASE_URL')
TURSO_TOKEN = os.environ.get('TURSO_AUTH_TOKEN')

STATUSES = [
    ('new', 'New'),
    ('contacted', 'Contacted'),
    ('promised', 'Promised to Pay'),
    ('partial', 'Partial Payment'),
    ('disputed', 'Disputed'),
    ('escalated', 'Escalated'),
    ('legal', 'Legal / Write-off'),
    ('resolved', 'Resolved'),
]
STATUS_KEYS = {k for k, _ in STATUSES}

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    partner_id INTEGER PRIMARY KEY,
    name    TEXT NOT NULL DEFAULT '',
    phone   TEXT DEFAULT '',
    mobile  TEXT DEFAULT '',
    email   TEXT DEFAULT '',
    vat     TEXT DEFAULT '',
    city    TEXT DEFAULT '',
    company TEXT DEFAULT '',
    area    TEXT DEFAULT '',
    payment_term TEXT DEFAULT '',
    term_days    INTEGER DEFAULT NULL,
    credit_limit REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS documents (
    line_id    INTEGER PRIMARY KEY,
    partner_id INTEGER NOT NULL,
    company_id INTEGER NOT NULL DEFAULT 0,
    company    TEXT NOT NULL DEFAULT '',
    doc        TEXT DEFAULT '',
    ref        TEXT DEFAULT '',
    journal    TEXT DEFAULT '',
    inv_date   TEXT NOT NULL,
    due_date   TEXT NOT NULL,
    original   REAL NOT NULL DEFAULT 0,
    residual   REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_documents_partner ON documents(partner_id);
CREATE INDEX IF NOT EXISTS idx_documents_due ON documents(due_date);
CREATE INDEX IF NOT EXISTS idx_documents_company ON documents(company_id);

CREATE TABLE IF NOT EXISTS followups (
    partner_id       INTEGER PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'new',
    owner            TEXT DEFAULT '',
    promise_date     TEXT DEFAULT '',
    promise_amount   REAL DEFAULT 0,
    next_action_date TEXT DEFAULT '',
    updated_at       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_id INTEGER NOT NULL,
    body       TEXT NOT NULL,
    author     TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notes_partner ON notes(partner_id);

-- One row per allocation of a receipt: a payment split across three invoices
-- becomes three rows, plus a fourth for any unapplied remainder. Summing
-- `amount` therefore always returns the cash actually banked, while grouping by
-- salesperson or customer stays exact.
CREATE TABLE IF NOT EXISTS collections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    line_id     INTEGER NOT NULL,
    company_id  INTEGER NOT NULL DEFAULT 0,
    company     TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL,
    month       TEXT NOT NULL,
    partner_id  INTEGER NOT NULL DEFAULT 0,
    customer    TEXT NOT NULL DEFAULT '',
    area        TEXT NOT NULL DEFAULT '',
    doc         TEXT NOT NULL DEFAULT '',
    ref         TEXT NOT NULL DEFAULT '',
    journal     TEXT NOT NULL DEFAULT '',
    invoice     TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    invoice_journal TEXT NOT NULL DEFAULT '',
    opening INTEGER NOT NULL DEFAULT 0,
    user_id     INTEGER NOT NULL DEFAULT 0,
    salesperson TEXT NOT NULL DEFAULT '',
    applied     INTEGER NOT NULL DEFAULT 1,
    amount      REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_coll_date    ON collections(date);
CREATE INDEX IF NOT EXISTS idx_coll_partner ON collections(partner_id);
CREATE INDEX IF NOT EXISTS idx_coll_user    ON collections(user_id);
CREATE INDEX IF NOT EXISTS idx_coll_company ON collections(company_id);
CREATE INDEX IF NOT EXISTS idx_coll_area    ON collections(area);

-- Customers handed to a collection agency. Local only: Odoo has no field for
-- this and a sync must never clear it.
CREATE TABLE IF NOT EXISTS agency (
    partner_id INTEGER PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT '',
    matched_as TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    added_at   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at  TEXT NOT NULL,
    lines      INTEGER NOT NULL DEFAULT 0,
    customers  INTEGER NOT NULL DEFAULT 0,
    total_open REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- Row/Result

class Row:
    """Mimics sqlite3.Row: index or column-name access, and dict(row) support."""
    __slots__ = ('_cols', '_vals')

    def __init__(self, columns, values):
        self._cols = columns
        self._vals = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._vals[key]
        return self._vals[self._cols.index(key)]

    def keys(self):
        return list(self._cols)

    def __iter__(self):
        return iter(self._vals)

    def __repr__(self):
        return repr(dict(zip(self._cols, self._vals)))


class Result:
    """Mimics a sqlite3 cursor closely enough for this app: fetchone/fetchall/lastrowid."""

    def __init__(self, result_set):
        self._rs = result_set

    def fetchall(self):
        cols = self._rs.columns
        return [Row(cols, list(r)) for r in self._rs.rows]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        return self._rs.last_insert_rowid


# --------------------------------------------------------------------------- Connection

class Conn:
    """Thin wrapper around libsql_client.ClientSync with a sqlite3-like surface.

    Statements autocommit individually (Turso's HTTP transport does not support
    the transaction() API used here, only single statements and atomic batches),
    so `with conn:` is a no-op rather than a real transaction. The one place that
    genuinely needs atomicity — replacing the whole synced dataset on an Odoo
    sync — uses `conn.batch()` directly instead of a stream of separate execute()
    calls; see odoo_sync.py.
    """

    def __init__(self, client):
        self._client = client

    def execute(self, sql, params=None):
        rs = self._client.execute(sql, params if params is not None else [])
        return Result(rs)

    def executemany(self, sql, seq):
        seq = list(seq)
        if not seq:
            return
        stmts = [libsql_client.Statement(sql, p) for p in seq]
        for i in range(0, len(stmts), 400):
            self._client.batch(stmts[i:i + 400])

    def batch(self, statements):
        """statements: list of sql strings or (sql, params) tuples. Chunked, atomic per chunk."""
        stmts = [libsql_client.Statement(s[0], s[1]) if isinstance(s, tuple)
                 else libsql_client.Statement(s, [])
                 for s in statements]
        for i in range(0, len(stmts), 400):
            self._client.batch(stmts[i:i + 400])

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def close(self):
        self._client.close()


def connect():
    if TURSO_URL:
        # The Turso dashboard hands out a libsql:// URL, which this client turns
        # into a websocket connection — and that handshake fails in some hosting
        # environments (confirmed against this project's own Vercel-adjacent
        # setup). Plain HTTPS has no such problem and is the better fit for a
        # stateless serverless function anyway (one request, one connection, no
        # socket to keep alive), so the scheme is normalised here rather than
        # asking whoever sets the env var to remember to edit it by hand.
        url = re.sub(r'^libsql://', 'https://', TURSO_URL)
        client = libsql_client.create_client_sync(url, auth_token=TURSO_TOKEN)
    else:
        client = libsql_client.create_client_sync(f'file:{DB_PATH}')
    return Conn(client)


def init():
    """Create every table and index if it does not already exist."""
    statements = [st.strip() for st in SCHEMA.split(';') if st.strip()]
    conn = connect()
    try:
        for st in statements:
            conn.execute(st)
    finally:
        conn.close()


def get_setting(conn, key, default=None):
    row = conn.execute('SELECT value FROM settings WHERE key = ?', [key]).fetchone()
    return row['value'] if row else default


def set_setting(conn, key, value):
    conn.execute(
        'INSERT INTO settings (key, value) VALUES (?,?)'
        ' ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        [key, str(value)],
    )


def ensure_followup(conn, partner_id):
    conn.execute(
        'INSERT OR IGNORE INTO followups (partner_id, updated_at) VALUES (?,?)',
        [partner_id, datetime.now().isoformat(timespec='seconds')],
    )


def has_data(conn):
    return conn.execute('SELECT COUNT(*) AS n FROM documents').fetchone()['n'] > 0
