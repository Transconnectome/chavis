"""Permanent, private digest archive; a delivery failure never loses the research."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def week_bounds(day):
    """Inclusive Monday--Sunday dates for an ISO date, including year boundaries."""
    day = date.fromisoformat(str(day))
    start = day - timedelta(days=day.weekday())
    return start.isoformat(), (start + timedelta(days=6)).isoformat()


class DigestStore:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.path = self.state_dir / 'digest.sqlite3'
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS digests (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('daily','weekly')),
                    period_start TEXT NOT NULL, period_end TEXT NOT NULL,
                    created_at TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(kind, period_start, period_end));
                CREATE UNIQUE INDEX IF NOT EXISTS digest_period_export
                    ON digests(kind, period_start);
                CREATE TABLE IF NOT EXISTS sources (
                    digest_id TEXT NOT NULL REFERENCES digests(id), ordinal INTEGER NOT NULL,
                    url TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(digest_id, ordinal));
                CREATE TABLE IF NOT EXISTS feedback (
                    id INTEGER PRIMARY KEY, digest_id TEXT NOT NULL REFERENCES digests(id),
                    created_at TEXT NOT NULL, note TEXT NOT NULL, quality REAL,
                    review_minutes REAL, rework_count INTEGER);
                CREATE TABLE IF NOT EXISTS deliveries (
                    digest_id TEXT NOT NULL REFERENCES digests(id), target TEXT NOT NULL,
                    status TEXT NOT NULL, updated_at TEXT NOT NULL, attempts INTEGER NOT NULL,
                    receipt TEXT NOT NULL, PRIMARY KEY(digest_id, target));
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, digest):
        item = dict(digest)
        if item.get('kind') not in ('daily', 'weekly'):
            raise ValueError('kind must be daily or weekly')
        start = date.fromisoformat(item['period_start']).isoformat()
        end = date.fromisoformat(item.get('period_end', start)).isoformat()
        if start > end:
            raise ValueError('period_start must be <= period_end')
        item.update(period_start=start, period_end=end)
        item.setdefault('id', item['kind'] + ':' + start)
        if not isinstance(item['id'], str) or not item['id'] or len(item['id']) > 200:
            raise ValueError('invalid digest id')
        if not isinstance(item.get('markdown'), str) or not item['markdown'].strip():
            raise ValueError('markdown must be nonempty')
        if not isinstance(item.get('title'), str) or not item['title'].strip():
            raise ValueError('title must be nonempty')
        item.setdefault('sources', [])
        if not isinstance(item['sources'], list) or not all(isinstance(s, dict) and
                isinstance(s.get('url'), str) and s['url'].startswith(('https://', 'http://'))
                for s in item['sources']):
            raise ValueError('sources require an http(s) url')
        # These fields are derived, so read-back can safely be submitted again.
        for key in ('created_at', 'content_hash', 'markdown_path'):
            item.pop(key, None)
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
        digest_hash = hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as db:
            old = db.execute('SELECT content_hash FROM digests WHERE id=?', (item['id'],)).fetchone()
            if old and old['content_hash'] != digest_hash:
                raise ValueError('digest already archived with different content')
            if not old:
                db.execute('INSERT INTO digests VALUES (?, ?, ?, ?, ?, ?, ?)',
                           (item['id'], item['kind'], start, end, now_iso(), digest_hash, payload))
                for ordinal, source in enumerate(item['sources']):
                    db.execute('INSERT INTO sources VALUES (?, ?, ?, ?)',
                               (item['id'], ordinal, source['url'], json.dumps(source, ensure_ascii=False)))
        saved = self.get(item['id'])
        self.export(saved)
        return saved

    def export(self, item):
        folder = self.state_dir / 'digests' / item['kind']
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(folder.parent, 0o700)
        os.chmod(folder, 0o700)
        target = folder / (item['period_start'] + '.md')
        fd, temporary = tempfile.mkstemp(prefix='.digest-', dir=folder)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(item['markdown'].rstrip() + '\n')
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def _decode(self, row):
        if row is None:
            return None
        item = json.loads(row['payload'])
        item.update(created_at=row['created_at'], content_hash=row['content_hash'],
                    markdown_path=str(self.state_dir / 'digests' / item['kind'] /
                                      (item['period_start'] + '.md')))
        return item

    def get(self, digest_id):
        with self.connect() as db:
            return self._decode(db.execute('SELECT * FROM digests WHERE id=?', (digest_id,)).fetchone())

    def list(self, start=None, end=None, kind=None):
        """List digests by inclusive period_start range, with deterministic ordering."""
        where, values = [], []
        for value, field, operator in ((start, 'period_start', '>='), (end, 'period_start', '<=')):
            if value is not None:
                where.append(field + operator + '?')
                values.append(date.fromisoformat(str(value)).isoformat())
        if kind is not None:
            where.append('kind=?')
            values.append(kind)
        query = 'SELECT * FROM digests' + (' WHERE ' + ' AND '.join(where) if where else '')
        with self.connect() as db:
            return [self._decode(row) for row in db.execute(query + ' ORDER BY period_start,id', values)]

    def add_feedback(self, digest_id, note, quality=None, review_minutes=None, rework_count=None):
        if not isinstance(note, str) or not note.strip():
            raise ValueError('feedback note is required')
        if quality is not None and not 1 <= quality <= 5:
            raise ValueError('quality must be 1..5')
        if review_minutes is not None and review_minutes < 0:
            raise ValueError('review_minutes must be >=0')
        if rework_count is not None and (not isinstance(rework_count, int) or rework_count < 0):
            raise ValueError('rework_count must be a nonnegative integer')
        with self.connect() as db:
            result = db.execute('INSERT INTO feedback(digest_id,created_at,note,quality,review_minutes,rework_count) '
                                'VALUES(?,?,?,?,?,?)',
                                (digest_id, now_iso(), note.strip(), quality, review_minutes, rework_count))
            return result.lastrowid

    def feedback(self, start=None, end=None):
        where, values = [], []
        for value, operator in ((start, '>='), (end, '<=')):
            if value is not None:
                where.append('d.period_start' + operator + '?')
                values.append(date.fromisoformat(str(value)).isoformat())
        query = 'SELECT f.* FROM feedback f JOIN digests d ON d.id=f.digest_id'
        with self.connect() as db:
            return [dict(row) for row in db.execute(query +
                    (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY f.id', values)]

    def delivery(self, digest_id, target):
        with self.connect() as db:
            row = db.execute('SELECT * FROM deliveries WHERE digest_id=? AND target=?',
                             (digest_id, target)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result['receipt'] = json.loads(result['receipt'])
            return result

    def claim_delivery(self, digest_id, target, reconcile_unknown=False):
        """Atomic claim. Unknown/inflight Telegram sends cannot be blindly repeated."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status,updated_at FROM deliveries WHERE digest_id=? AND target=?',
                             (digest_id, target)).fetchone()
            blocked = {'sent', 'inflight', 'unknown'} - ({'unknown'} if reconcile_unknown else set())
            if row and row['status'] == 'inflight' and target == 'notion' and reconcile_unknown:
                # The connector times out after 360 seconds. After a process kill,
                # allow its unique-record-ID query to reconcile a stale claim.
                # Telegram has no equivalent query and must remain blocked.
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row['updated_at'])).total_seconds()
                except (TypeError, ValueError):
                    age = 0
                if age >= 1200:
                    blocked.discard('inflight')
            if row and row['status'] in blocked:
                return False
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,1,?) ON CONFLICT(digest_id,target) '
                       'DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,attempts=attempts+1',
                       (digest_id, target, 'inflight', now_iso(), '{}'))
            return True

    def finish_delivery(self, digest_id, target, status, receipt=None):
        if status not in ('sent', 'unknown', 'failed', 'unconfigured'):
            raise ValueError('invalid delivery status')
        with self.connect() as db:
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,0,?) ON CONFLICT(digest_id,target) '
                       'DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,receipt=excluded.receipt',
                       (digest_id, target, status, now_iso(), json.dumps(receipt or {}, ensure_ascii=False)))
        return self.delivery(digest_id, target)


def save_digest(digest, state_dir):
    return DigestStore(state_dir).save(digest)
