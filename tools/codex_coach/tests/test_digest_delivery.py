import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import digest_delivery as delivery
from digest_store import DigestStore
from test_digest_store import example

PAGE_ID = 'a' * 32
SOURCE_ID = 'b' * 32


class DigestDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = DigestStore(self.temp.name)
        self.item = self.store.save(example(telegram_text='오늘 바꿀 행동 하나'))

    def tearDown(self):
        self.temp.cleanup()

    def route(self):
        (Path(self.temp.name) / 'telegram-route.json').write_text(json.dumps(
            {'channel': 'telegram', 'target': 'synthetic'}))

    def test_timeout_preserves_archive_and_is_not_resent(self):
        self.route()
        with patch.object(delivery.subprocess, 'run', side_effect=subprocess.TimeoutExpired('send', 45)) as send:
            first = delivery.deliver_digest(self.item, self.temp.name)
            second = delivery.deliver_digest(self.item, self.temp.name)
        self.assertEqual(first['telegram']['status'], 'unknown')
        self.assertEqual(second['telegram']['attempts'], 1)
        send.assert_called_once()
        self.assertEqual(self.store.get(self.item['id'])['markdown'], self.item['markdown'])

    def test_sent_receipt_is_not_resent_and_archive_text_wins(self):
        self.route()
        result = subprocess.CompletedProcess([], 0, '{"payload":{"messageId":"42"}}', '')
        with patch.object(delivery.subprocess, 'run', return_value=result) as send:
            out = delivery.deliver_digest({**self.item, 'telegram_text': 'tampered'}, self.temp.name)
            delivery.deliver_digest(self.item, self.temp.name)
        self.assertEqual(out['telegram']['status'], 'sent')
        self.assertEqual(out['telegram']['receipt']['message_id'], '42')
        send.assert_called_once()
        self.assertNotIn('tampered', send.call_args.args[0])

    def test_unconfigured_delivery_can_resume_after_route_added(self):
        self.assertEqual(delivery.deliver_digest(self.item, self.temp.name)['telegram']['status'], 'unconfigured')
        self.route()
        result = subprocess.CompletedProcess([], 0, '{"payload":{"messageId":"43"}}', '')
        with patch.object(delivery.subprocess, 'run', return_value=result):
            self.assertEqual(delivery.deliver_digest(self.item, self.temp.name)['telegram']['status'], 'sent')

    def test_utf16_limit_with_emoji_and_clickable_notion_links(self):
        text = delivery.telegram_text({**self.item, 'telegram_text': '😀' * 6000}, 'https://notion.so/example')
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 3900)
        self.assertIn(self.item['id'], text)
        blocks = delivery.markdown_blocks('## 제목\n\n[원출처](https://example.org/study)\n\n' + '가' * 5000)
        self.assertEqual(blocks[1]['paragraph']['rich_text'][0]['text']['link']['url'], 'https://example.org/study')
        self.assertTrue(all(len(t['text']['content']) <= 1800 for block in blocks
                            for t in block[block['type']]['rich_text']))

    def test_notion_existing_id_reconciles_missing_suffix_without_duplicate_create(self):
        blocks = delivery.markdown_blocks(self.item['markdown'])
        page = {'id': PAGE_ID, 'url': 'https://notion.so/test', 'properties': {
            'Digest hash': {'rich_text': [{'plain_text': self.item['content_hash']}]}}}
        calls = []
        remote = blocks[:1]
        def request(method, path, payload, token):
            calls.append((method, path, payload))
            if path.endswith('/query'):
                return {'results': [page], 'has_more': False}
            if method == 'GET':
                return {'results': list(remote), 'has_more': False}
            if method == 'PATCH':
                remote.extend(payload['children'])
                return {'results': payload['children']}
            self.fail('must not create duplicate page')
        with patch.dict(os.environ, {'NOTION_TOKEN': 'synthetic'}), patch.object(delivery, 'notion_request', side_effect=request):
            receipt = delivery.sync_notion(self.item, {'data_source_id': SOURCE_ID})
        self.assertEqual(receipt['verified_blocks'], len(blocks))
        appended = [c[2]['children'] for c in calls if c[0] == 'PATCH']
        self.assertEqual(appended, [blocks[1:]])

    def test_notion_batches_and_readback_for_more_than_100_blocks(self):
        item = {**self.item, 'markdown': '\n\n'.join('line ' + str(i) for i in range(225))}
        remote, batches = [], []
        def request(method, path, payload, token):
            if path.endswith('/query'):
                return {'results': [], 'has_more': False}
            if path == 'pages':
                batches.append(len(payload['children']))
                remote.extend(payload['children'])
                return {'id': PAGE_ID, 'url': 'https://notion.so/test'}
            if method == 'GET':
                return {'results': list(remote), 'has_more': False}
            if method == 'PATCH':
                batches.append(len(payload['children']))
                remote.extend(payload['children'])
                return {'results': payload['children']}
            self.fail('unexpected request')
        with patch.dict(os.environ, {'NOTION_TOKEN': 'synthetic'}), patch.object(delivery, 'notion_request', side_effect=request):
            result = delivery.sync_notion(item, {'data_source_id': SOURCE_ID})
        self.assertEqual(batches, [100, 100, 25])
        self.assertEqual(result['verified_blocks'], 225)

    def test_notion_content_conflict_never_appends(self):
        page = {'id': PAGE_ID, 'properties': {'Digest hash': {'rich_text': [{'plain_text': 'wrong'}]}}}
        with patch.dict(os.environ, {'NOTION_TOKEN': 'synthetic'}), patch.object(delivery, 'notion_request',
                return_value={'results': [page]}) as request:
            with self.assertRaisesRegex(delivery.DeliveryError, 'conflict'):
                delivery.sync_notion(self.item, {'data_source_id': SOURCE_ID})
        request.assert_called_once()

    def test_connector_transport_verifies_then_uses_page_link_for_telegram(self):
        self.route()
        connector = types.ModuleType('notion_sync')
        connector.sync_via_codex = lambda item, state, config: {
            'status': 'sent', 'page_url': 'https://notion.so/verified', 'readback_verified': True}
        result = subprocess.CompletedProcess([], 0, '{"payload":{"messageId":"44"}}', '')
        with patch.dict(sys.modules, {'notion_sync': connector}), patch.object(delivery.subprocess, 'run', return_value=result) as send:
            out = delivery.deliver_digest(self.item, self.temp.name, {'transport': 'codex_apps'})
        self.assertEqual(out['notion']['status'], 'sent')
        self.assertIn('https://notion.so/verified', send.call_args.args[0][send.call_args.args[0].index('--message') + 1])

    def test_prepare_notion_only_leaves_telegram_for_later_delivery(self):
        self.route()
        notion_receipt = {'page_id': PAGE_ID, 'url': 'https://notion.so/prepared'}
        result = subprocess.CompletedProcess([], 0, '{"payload":{"messageId":"45"}}', '')
        with patch.object(delivery, 'sync_notion', return_value=notion_receipt) as sync, \
                patch.object(delivery.subprocess, 'run', return_value=result) as send:
            prepared = delivery.deliver_digest(self.item, self.temp.name, targets=('notion',))
            self.assertEqual(set(prepared), {'notion'})
            self.assertIsNone(self.store.delivery(self.item['id'], 'telegram'))
            send.assert_not_called()
            delivered = delivery.deliver_digest(self.item, self.temp.name)
        sync.assert_called_once()
        send.assert_called_once()
        self.assertEqual(delivered['telegram']['status'], 'sent')
        self.assertIn('https://notion.so/prepared', send.call_args.args[0][send.call_args.args[0].index('--message') + 1])

    def test_connector_failure_preserves_safe_reason_and_still_sends_telegram(self):
        self.route()
        connector = types.ModuleType('notion_sync')
        connector.sync_via_codex = lambda item, state, config: {
            'status': 'unknown', 'reason': 'page_readback_mismatch'}
        result = subprocess.CompletedProcess([], 0, '{"payload":{"messageId":"46"}}', '')
        with patch.dict(sys.modules, {'notion_sync': connector}), patch.object(delivery.subprocess, 'run', return_value=result):
            out = delivery.deliver_digest(self.item, self.temp.name, {'transport': 'codex_apps'})
        self.assertEqual(out['notion']['status'], 'unknown')
        self.assertEqual(out['notion']['receipt']['reason'], 'page_readback_mismatch')
        self.assertEqual(out['telegram']['status'], 'sent')
        self.assertIsNone(delivery.DeliveryError('synthetic', reason='Bearer SECRET').reason)


if __name__ == '__main__':
    unittest.main()
