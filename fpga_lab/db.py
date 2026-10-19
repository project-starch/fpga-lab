"""SQLite state for the hub: jobs, bookings, qualifications, usage, alerts.

One file, one writer (the hub process). Every connection is short-lived, so
the dispatcher thread and request handlers never share a cursor.
"""

import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT NOT NULL, purpose TEXT DEFAULT '',
  board_req TEXT NOT NULL DEFAULT 'any',
  bit_path TEXT NOT NULL, bit_sha TEXT NOT NULL,
  img_path TEXT NOT NULL, img_sha TEXT NOT NULL,
  until_regex TEXT, timeout_s INTEGER NOT NULL DEFAULT 600,
  state TEXT NOT NULL DEFAULT 'queued',   -- queued|running|done|failed|cancelled
  board TEXT, reason TEXT, matched INTEGER,
  created REAL NOT NULL, started REAL, finished REAL
);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  board TEXT NOT NULL, holder TEXT NOT NULL, purpose TEXT DEFAULT '',
  start REAL NOT NULL, end REAL NOT NULL, created REAL NOT NULL, ip TEXT
);
CREATE TABLE IF NOT EXISTS qualified (
  board TEXT NOT NULL, bit_sha TEXT NOT NULL, note TEXT DEFAULT '',
  added REAL NOT NULL, PRIMARY KEY (board, bit_sha)
);
CREATE TABLE IF NOT EXISTS usage (
  board TEXT NOT NULL, minute INTEGER NOT NULL, holder TEXT, kind TEXT,
  PRIMARY KEY (board, minute)
);
CREATE TABLE IF NOT EXISTS users (
  name TEXT PRIMARY KEY, token_sha TEXT NOT NULL UNIQUE, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, board TEXT,
  message TEXT NOT NULL, opened REAL NOT NULL, closed REAL
);
"""


class DB:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def q(self, sql, *args):
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def x(self, sql, *args):
        with self.conn() as c:
            cur = c.execute(sql, args)
            return cur.lastrowid

    # ---------------------------------------------------------------- jobs

    def add_job(self, owner, purpose, board_req, bit_path, bit_sha, img_path,
                img_sha, until_regex, timeout_s):
        return self.x("""INSERT INTO jobs (owner, purpose, board_req, bit_path, bit_sha,
                         img_path, img_sha, until_regex, timeout_s, created)
                         VALUES (?,?,?,?,?,?,?,?,?,?)""",
                      owner, purpose, board_req, bit_path, bit_sha, img_path,
                      img_sha, until_regex, int(timeout_s), time.time())

    def job(self, jid):
        r = self.q('SELECT * FROM jobs WHERE id=?', jid)
        return r[0] if r else None

    def jobs(self, limit=50):
        return self.q('SELECT * FROM jobs ORDER BY id DESC LIMIT ?', limit)

    def next_queued(self):
        return self.q("SELECT * FROM jobs WHERE state='queued' ORDER BY id")

    def set_job(self, jid, **kw):
        cols = ', '.join(f'{k}=?' for k in kw)
        self.x(f'UPDATE jobs SET {cols} WHERE id=?', *kw.values(), jid)

    # ------------------------------------------------------------ bookings

    def add_booking(self, board, holder, purpose, start, end, ip=None):
        clash = self.q("""SELECT * FROM bookings WHERE board=? AND start < ? AND end > ?""",
                       board, end, start)
        if clash:
            b = clash[0]
            raise ValueError(f"overlaps the booking of {b['holder']}")
        return self.x("""INSERT INTO bookings (board, holder, purpose, start, end, created, ip)
                         VALUES (?,?,?,?,?,?,?)""", board, holder, purpose, start, end,
                      time.time(), ip)

    def bookings(self, board=None, since=None):
        since = time.time() if since is None else since
        if board:
            return self.q('SELECT * FROM bookings WHERE board=? AND end > ? ORDER BY start',
                          board, since)
        return self.q('SELECT * FROM bookings WHERE end > ? ORDER BY start', since)

    def cancel_booking(self, bid):
        self.x('DELETE FROM bookings WHERE id=?', bid)

    # --------------------------------------------------------- qualification

    def qualify(self, board, bit_sha, note=''):
        self.x('INSERT OR REPLACE INTO qualified VALUES (?,?,?,?)', board, bit_sha,
               note, time.time())

    def is_qualified(self, board, bit_sha):
        return bool(self.q('SELECT 1 FROM qualified WHERE board=? AND bit_sha=?',
                           board, bit_sha))

    def qualifications(self):
        return self.q('SELECT * FROM qualified ORDER BY board, added')

    # -------------------------------------------------------------- usage

    def record_usage(self, board, minute, holder, kind):
        self.x('INSERT OR REPLACE INTO usage VALUES (?,?,?,?)', board, minute, holder, kind)

    def usage_report(self, since):
        return self.q("""SELECT board, holder, kind, COUNT(*) AS minutes FROM usage
                         WHERE minute >= ? AND holder IS NOT NULL
                         GROUP BY board, holder, kind ORDER BY board, minutes DESC""",
                      int(since // 60))

    # -------------------------------------------------------------- users

    def add_user(self, name, token_sha):
        self.x('INSERT OR REPLACE INTO users VALUES (?,?,?)', name, token_sha, time.time())

    def user_by_token_sha(self, token_sha):
        r = self.q('SELECT name FROM users WHERE token_sha=?', token_sha)
        return r[0]['name'] if r else None

    def users(self):
        return self.q('SELECT name, created FROM users ORDER BY name')

    def remove_user(self, name):
        self.x('DELETE FROM users WHERE name=?', name)

    # ------------------------------------------------------------- alerts

    def open_alert(self, key, board, message):
        if self.q('SELECT 1 FROM alerts WHERE key=? AND closed IS NULL', key):
            return False
        self.x('INSERT INTO alerts (key, board, message, opened) VALUES (?,?,?,?)',
               key, board, message, time.time())
        return True

    def close_alert(self, key):
        if not self.q('SELECT 1 FROM alerts WHERE key=? AND closed IS NULL', key):
            return False
        self.x('UPDATE alerts SET closed=? WHERE key=? AND closed IS NULL', time.time(), key)
        return True

    def open_alerts(self):
        return self.q('SELECT * FROM alerts WHERE closed IS NULL ORDER BY opened')
