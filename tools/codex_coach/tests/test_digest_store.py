from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from digest_store import DigestStore, week_bounds


def example(day='2026-09-12', **extra):
    return {'kind': 'daily', 'period_start': day, 'title': '근거 확인',
            'markdown': '# 근거 확인\n\n검증할 행동 하나.',
            'sources': [{'url': 'https://example.org/study', 'title': 'Study',
                         'retrieved_at': day, 'evidence_level': 'primary',
                         'limitations': 'synthetic'}], **extra}


class DigestStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DigestStore(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_roundtrip_idempotent_and_private(self):
        saved = self.store.save(example())
        self.assertEqual(self.store.save(saved), saved)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.get(saved['id'])['sources'][0]['limitations'], 'synthetic')
        self.assertEqual(Path(saved['markdown_path']).read_text(), saved['markdown'] + '\n')
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(saved['markdown_path']).stat().st_mode & 0o777, 0o600)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0], 1)

    def test_immutable_archive_and_repair_export(self):
        saved = self.store.save(example())
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.store.save(example(markdown='rewritten'))
        Path(saved['markdown_path']).unlink()
        self.store.save(example())
        self.assertTrue(Path(saved['markdown_path']).exists())

    def test_week_year_boundary_and_inclusive_sql_dates(self):
        self.assertEqual(week_bounds('2027-01-01'), ('2026-12-28', '2027-01-03'))
        for day in ('2026-12-27', '2026-12-28', '2027-01-03', '2027-01-04'):
            self.store.save(example(day))
        selected = self.store.list(*week_bounds('2027-01-01'), kind='daily')
        self.assertEqual([d['period_start'] for d in selected], ['2026-12-28', '2027-01-03'])

    def test_kind_unique_and_invalid_dates(self):
        self.store.save(example())
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save(example(id='another-id'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save(example(id='another-id', period_end='2026-09-13'))
        with self.assertRaises(ValueError):
            self.store.save(example(period_start='2026-02-30'))
        with self.assertRaises(ValueError):
            self.store.save(example(period_end='2026-09-11'))

    def test_feedback_foreign_key_and_unreported_values_stay_null(self):
        item = self.store.save(example())
        self.store.add_feedback(item['id'], '적용함', review_minutes=12, rework_count=1)
        feedback = self.store.feedback('2026-09-12', '2026-09-12')[0]
        self.assertIsNone(feedback['quality'])
        self.assertEqual(feedback['review_minutes'], 12)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_feedback('missing', 'note')

    def test_delivery_claim_blocks_concurrent_and_ambiguous_duplicates(self):
        item = self.store.save(example())
        self.assertTrue(self.store.claim_delivery(item['id'], 'telegram'))
        other = DigestStore(self.temp.name)
        self.assertFalse(other.claim_delivery(item['id'], 'telegram'))
        self.store.finish_delivery(item['id'], 'telegram', 'unknown', {'code': 'timeout'})
        self.assertFalse(other.claim_delivery(item['id'], 'telegram'))
        self.assertTrue(other.claim_delivery(item['id'], 'telegram', reconcile_unknown=True))

    def test_only_stale_notion_inflight_can_be_reconciled(self):
        item = self.store.save(example())
        for target in ('notion', 'telegram'):
            self.assertTrue(self.store.claim_delivery(item['id'], target))
            self.assertFalse(self.store.claim_delivery(item['id'], target, reconcile_unknown=True))
        with self.store.connect() as db:
            db.execute('UPDATE deliveries SET updated_at=? WHERE digest_id=?',
                       ('2020-01-01T00:00:00+00:00', item['id']))
        self.assertTrue(self.store.claim_delivery(item['id'], 'notion', reconcile_unknown=True))
        self.assertFalse(self.store.claim_delivery(item['id'], 'telegram', reconcile_unknown=True))
        self.assertFalse(self.store.claim_delivery(item['id'], 'notion', reconcile_unknown=True))


if __name__ == '__main__':
    unittest.main()
