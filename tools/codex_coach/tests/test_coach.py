import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coach


class CoachTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state = coach.STATE
        coach.STATE = Path(self.tmp.name)
        self.sessions = [{'session_id': 'demo', 'cwd': '/synthetic', 'source_file': '/synthetic/log.jsonl',
                          'messages': [{'role': 'assistant', 'text': 'All done; the requested attachment was not created.'}]}]
        self.card = {'actionable': True, 'confidence': 0.9, 'category': 'completion_evidence',
                     'session_id': 'demo', 'evidence_quote': 'the requested attachment was not created.',
                     'observation': 'synthetic observation', 'cause': 'unknown', 'suggested_prompt': 'verify file',
                     'question': '', 'verification': 'inspect file', 'limitations': 'partial sample'}

    def tearDown(self):
        coach.STATE = self.old_state
        self.tmp.cleanup()

    def test_requires_real_evidence_not_just_score(self):
        self.assertTrue(coach.valid_card(self.card, self.sessions))
        for field, value in [('evidence_quote', 'This never appeared in any session.'),
                             ('session_id', 'wrong-session'), ('confidence', 2), ('actionable', False)]:
            bad = {**self.card, field: value}
            self.assertFalse(coach.valid_card(bad, self.sessions))

    def test_telegram_never_contains_model_text_or_private_quote(self):
        secret = 'confidential personnel review 12345'
        card = {**self.card, 'id': 'demo-card', 'observation': secret, 'suggested_prompt': secret,
                'evidence_quote': secret, 'question': secret}
        self.assertNotIn(secret, coach.alert_text(card))
        self.assertIn('완료 확인', coach.alert_text(card))

    def test_private_storage(self):
        coach.save_json('test.json', {'a': 1})
        self.assertEqual((coach.STATE / 'test.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(coach.STATE.stat().st_mode & 0o777, 0o700)

    def test_dry_run_does_not_consume_or_infer(self):
        batch = {'sessions': self.sessions, 'cursor': {'demo': {'offset': 22}}, 'coverage': {}}
        with patch.object(coach, 'observation', return_value=batch), patch.object(coach, 'infer') as inference:
            result = coach.tick(dry_run=True)
        self.assertEqual(result['status'], 'observed_no_model_no_delivery')
        inference.assert_not_called()
        self.assertFalse((coach.STATE / 'monitor.json').exists())

    def test_pause_never_observes(self):
        coach.save_json('config.json', {'enabled': False})
        with patch.object(coach, 'observation') as observation:
            self.assertEqual(coach.tick()['status'], 'paused')
        observation.assert_not_called()

    def test_failure_counts_budget_and_preserves_cursor(self):
        batch = {'sessions': self.sessions, 'cursor': {'demo': {'offset': 22}}, 'coverage': {}}
        coach.save_json('monitor.json', {'cursor': {'demo': {'offset': 5}}})
        with patch.object(coach, 'observation', return_value=batch), patch.object(coach, 'infer', side_effect=RuntimeError('model_timeout')):
            coach.tick()
        state = coach.read_json('monitor.json', {})
        self.assertEqual(state['cursor']['demo']['offset'], 5)
        self.assertEqual(state['analyses_today'], 1)

    def test_duplicate_and_non_actionable_are_quiet(self):
        batch = {'sessions': self.sessions, 'cursor': {}, 'coverage': {}}
        prior = {**self.card, 'created_at': coach.utcnow().isoformat(), 'id': 'a', 'feedback': [], 'delivery': 'sent'}
        coach.save_json('cards.json', [prior])
        with patch.object(coach, 'observation', return_value=batch), patch.object(coach, 'infer', return_value=copy.deepcopy(self.card)), patch.object(coach, 'send_alert') as send:
            coach.tick()
        send.assert_not_called()
        self.assertEqual(len(coach.read_json('cards.json', [])), 1)

    def test_quiet_hours_save_local_advice(self):
        batch = {'sessions': self.sessions, 'cursor': {}, 'coverage': {}}
        night = datetime(2026, 9, 12, 15, 0, tzinfo=timezone.utc)
        with patch.object(coach, 'utcnow', return_value=night), patch.object(coach, 'observation', return_value=batch), patch.object(coach, 'infer', return_value=copy.deepcopy(self.card)), patch.object(coach, 'send_alert') as send:
            coach.tick()
        send.assert_not_called()
        self.assertEqual(coach.read_json('cards.json', [])[0]['delivery'], 'quiet_hours')

    def test_expired_cards_removed_while_paused(self):
        coach.save_json('cards.json', [{'created_at': '2020-01-01T00:00:00+00:00'}])
        coach.save_json('config.json', {'enabled': False})
        coach.tick()
        self.assertEqual(coach.read_json('cards.json', []), [])

    def test_pause_during_inference_prevents_send(self):
        batch = {'sessions': self.sessions, 'cursor': {}, 'coverage': {}}
        def pause_then_return(*args):
            coach.save_json('config.json', {'enabled': False})
            return copy.deepcopy(self.card)
        day = datetime(2026, 9, 12, 4, 0, tzinfo=timezone.utc)
        with patch.object(coach, 'utcnow', return_value=day), patch.object(coach, 'observation', return_value=batch), patch.object(coach, 'infer', side_effect=pause_then_return), patch.object(coach, 'send_alert') as send:
            coach.tick()
        send.assert_not_called()

    def test_unsent_advice_can_be_revalidated_in_daytime(self):
        batch = {'sessions': self.sessions, 'cursor': {}, 'coverage': {}}
        day = datetime(2026, 9, 12, 4, 0, tzinfo=timezone.utc)
        prior = {**self.card, 'id': 'same-id', 'created_at': day.isoformat(), 'feedback': [], 'delivery': 'quiet_hours'}
        coach.save_json('cards.json', [prior])
        with patch.object(coach, 'utcnow', return_value=day), patch.object(coach, 'observation', return_value=batch), patch.object(coach, 'infer', return_value=copy.deepcopy(self.card)), patch.object(coach, 'send_alert', return_value='sent') as send:
            coach.tick()
        send.assert_called_once()
        self.assertEqual(len(coach.read_json('cards.json', [])), 1)
        self.assertEqual(coach.read_json('cards.json', [])[0]['id'], 'same-id')

    def test_context_preserves_request_and_latest_evidence(self):
        session = {**self.sessions[0], 'messages': [{'role': 'user', 'text': 'REQUEST' + 'x'*5000, 'timestamp': '1'}] +
                   [{'role': 'assistant', 'text': str(i)+'x'*2000, 'timestamp': str(i+2)} for i in range(20)] +
                   [{'role': 'assistant', 'text': 'LATEST_EVIDENCE', 'timestamp': '23'}]}
        current = coach.contextualize([session], coach.utcnow())[0]['messages']
        self.assertTrue(current[0]['text'].startswith('REQUEST'))
        self.assertEqual(current[-1]['text'], 'LATEST_EVIDENCE')
        self.assertLessEqual(sum(len(m['text']) for m in current), 3000)

    def test_debounce_preserves_new_records_for_later(self):
        batch = {'sessions': self.sessions, 'cursor': {'new': 42}, 'coverage': {}}
        coach.save_json('monitor.json', {'last_analysis': coach.utcnow().isoformat(), 'cursor': {'old': 2}})
        with patch.object(coach, 'observation', return_value=batch), patch.object(coach, 'infer') as inference:
            self.assertEqual(coach.tick()['status'], 'analysis_debounce')
        inference.assert_not_called()
        self.assertEqual(coach.read_json('monitor.json', {})['cursor'], {'old': 2})

    def test_research_preserves_old_findings_and_clears_pending(self):
        old = {'source_url': 'https://arxiv.org/old', 'claim': 'previous bounded claim'}
        coach.save_json('learning.json', {'findings': [old]})
        quote = 'An actual sufficiently long source passage.'
        source = {'url': 'https://arxiv.org/new', 'new': True, 'changed': True, 'learning_pending': True,
                  'excerpt': quote, 'retrieved_at': '2026-09-12', 'content_hash': 'hash'}
        finding = {'source_url': source['url'], 'claim': 'new bounded claim', 'methods': 'experiment',
                   'limitations': 'small study', 'possible_application': 'trial', 'evidence_quote': quote}
        with patch('research.refresh', return_value={'sources': [source], 'candidates': []}), patch.object(coach, 'infer', return_value={'findings': [finding]}):
            result = coach.research()
        self.assertEqual(result['grounded_findings'], 1)
        self.assertEqual(len(coach.read_json('learning.json', {})['findings']), 2)
        self.assertFalse(coach.read_json('research.json', {})['sources'][0]['learning_pending'])


if __name__ == '__main__':
    unittest.main()
