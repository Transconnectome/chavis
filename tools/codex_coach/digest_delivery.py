"""Receipted digest delivery using the existing Telegram route and Notion API.

Notion API contract: developers.notion.com/reference/post-page and
developers.notion.com/reference/patch-block-children (version 2025-09-03).
Only public-source coaching prose belongs in these digests, never personnel records.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from digest_store import DigestStore


def rich_text(text):
    result, position = [], 0
    for match in re.finditer(r'\[([^\]\n]+)\]\((https?://[^\s)]+)\)', text):
        if match.start() > position:
            result.append({'type': 'text', 'text': {'content': text[position:match.start()]}})
        result.append({'type': 'text', 'text': {'content': match.group(1),
                                              'link': {'url': match.group(2)}}})
        position = match.end()
    if position < len(text):
        result.append({'type': 'text', 'text': {'content': text[position:]}})
    return result or [{'type': 'text', 'text': {'content': ''}}]


def markdown_blocks(markdown):
    """Preserve all text and links; keep each rich-text string under 2,000 chars."""
    blocks = []
    code = False
    for paragraph in markdown.split('\n\n'):
        for line in paragraph.splitlines():
            if line.startswith('```'):
                code = not code
                continue
            if not line.strip():
                continue
            kind, text = 'paragraph', line
            if not code:
                if re.match(r'^#{1,3} ', line):
                    count = len(line) - len(line.lstrip('#'))
                    kind, text = 'heading_' + str(count), line[count + 1:]
                elif line.startswith('- '):
                    kind, text = 'bulleted_list_item', line[2:]
                elif line.startswith('> '):
                    kind, text = 'quote', line[2:]
            for offset in range(0, len(text), 1800):
                blocks.append({'object': 'block', 'type': kind,
                               kind: {'rich_text': rich_text(text[offset:offset + 1800])}})
    return blocks


def block_signature(block):
    kind = block.get('type')
    data = block.get(kind, {})
    return kind, ''.join(item.get('plain_text', item.get('text', {}).get('content', ''))
                         for item in data.get('rich_text', []))


class DeliveryError(Exception):
    def __init__(self, code, ambiguous=False, reason=None):
        self.code, self.ambiguous = code, ambiguous
        self.reason = reason if isinstance(reason, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,99}', reason) else None
        super().__init__(code)


def notion_request(method, path, payload, token):
    request = urllib.request.Request('https://api.notion.com/v1/' + path,
        data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
        headers={'Authorization': 'Bearer ' + token, 'Notion-Version': '2025-09-03',
                 'Content-Type': 'application/json'}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # No response bodies: they can echo user content or authentication details.
        raise DeliveryError('notion_http_' + str(error.code), error.code >= 500) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise DeliveryError('notion_network_or_invalid_receipt', method != 'GET') from None


def _notion_children(page_id, token):
    result, cursor = [], None
    while True:
        path = 'blocks/' + page_id + '/children?page_size=100'
        if cursor:
            path += '&start_cursor=' + urllib.parse.quote(cursor, safe='')
        response = notion_request('GET', path, None, token)
        result.extend(response['results'])
        if not response.get('has_more'):
            return result
        cursor = response['next_cursor']


def _text_property(page, name):
    return ''.join(item.get('plain_text', item.get('text', {}).get('content', ''))
                   for item in page.get('properties', {}).get(name, {}).get('rich_text', []))


def sync_notion(item, config):
    token = os.environ.get(config.get('token_env', 'NOTION_TOKEN'), '')
    data_source_id = config.get('data_source_id', '')
    if not token or not re.fullmatch(r'[a-fA-F0-9-]{32,36}', data_source_id):
        raise DeliveryError('notion_unconfigured')
    query = notion_request('POST', 'data_sources/' + data_source_id + '/query',
        {'filter': {'property': 'Record ID', 'rich_text': {'equals': item['id']}}, 'page_size': 2}, token)
    matches = query.get('results', [])
    if len(matches) > 1 or query.get('has_more'):
        raise DeliveryError('notion_duplicate_record_ids')
    blocks = markdown_blocks(item['markdown'])
    expected = [block_signature(block) for block in blocks]
    if matches:
        page = matches[0]
        if _text_property(page, 'Digest hash') != item['content_hash']:
            raise DeliveryError('notion_record_content_conflict')
    else:
        page = notion_request('POST', 'pages', {
            'parent': {'type': 'data_source_id', 'data_source_id': data_source_id},
            'properties': {
                'Name': {'title': rich_text(item['title'][:1800])},
                'Record ID': {'rich_text': rich_text(item['id'])},
                'Digest hash': {'rich_text': rich_text(item['content_hash'])},
                'Kind': {'select': {'name': item['kind']}},
                'Period': {'date': {'start': item['period_start'], 'end': item['period_end']}},
            }, 'children': blocks[:100]}, token)
    page_id = page.get('id', '')
    if not re.fullmatch(r'[a-fA-F0-9-]{32,36}', page_id):
        raise DeliveryError('notion_missing_page_receipt', ambiguous=True)
    # Reconcile a prior timeout by reading actual children and appending only a
    # proven missing suffix. Never call create twice without this unique-ID query.
    actual = [block_signature(block) for block in _notion_children(page_id, token)]
    if actual != expected[:len(actual)]:
        raise DeliveryError('notion_page_content_conflict')
    for offset in range(len(actual), len(blocks), 100):
        notion_request('PATCH', 'blocks/' + page_id + '/children',
                       {'children': blocks[offset:offset + 100]}, token)
    verified = [block_signature(block) for block in _notion_children(page_id, token)]
    if verified != expected:
        raise DeliveryError('notion_readback_mismatch', ambiguous=True)
    return {'page_id': page_id, 'url': page.get('url', ''), 'verified_blocks': len(verified),
            'content_hash': item['content_hash']}


def telegram_text(item, notion_url=''):
    text = item.get('telegram_text') or item['markdown']
    if item.get('research_degraded'):
        text = '오늘 새 자료 검색에 실패하여 기존 확인 자료로 작성한 적용 점검입니다.\n\n' + text
    suffix = ('\n\n전체 기록: ' + notion_url if notion_url else '')
    evidence_urls = list(dict.fromkeys(e.get('url') for e in item.get('evidence', []) if e.get('url')))[:3]
    if evidence_urls:
        suffix += '\n\n근거: ' + '\n'.join(evidence_urls)
    suffix += '\n\n기록 ID: ' + item['id']
    # Telegram counts UTF-16 units; leave room for the receipts and visible link.
    budget = 3900 - len(suffix.encode('utf-16-le')) // 2
    raw = text.encode('utf-16-le')
    if len(raw) // 2 > budget:
        text = raw[:max(0, budget - 2) * 2].decode('utf-16-le', errors='ignore').rstrip() + '…'
    return text + suffix


def send_telegram(item, state_dir, notion_url=''):
    route_path = Path(state_dir) / 'telegram-route.json'
    route = json.loads(route_path.read_text()) if route_path.exists() else {}
    if route.get('channel') != 'telegram' or not route.get('target'):
        raise DeliveryError('telegram_unconfigured')
    command = [shutil.which('openclaw') or '/home/juke/.npm-global/bin/openclaw',
               'message', 'send', '--channel', 'telegram', '--target', route['target'],
               '--message', telegram_text(item, notion_url), '--json']
    if route.get('account'):
        command += ['--account', route['account']]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=45)
    except subprocess.TimeoutExpired:
        raise DeliveryError('telegram_timeout', ambiguous=True) from None
    except OSError:
        raise DeliveryError('telegram_command_unavailable') from None
    if result.returncode != 0:
        raise DeliveryError('telegram_command_failed', ambiguous=True)
    try:
        receipt = json.loads(result.stdout[result.stdout.index('{'):])
        payload = receipt.get('payload', receipt)
        message_id = payload.get('messageId') or payload.get('result', {}).get('message_id')
        if not message_id:
            raise ValueError('no message id')
    except (ValueError, TypeError, AttributeError):
        raise DeliveryError('telegram_missing_receipt', ambiguous=True) from None
    return {'message_id': str(message_id)}


def deliver_digest(item, state_dir, notion_config=None, targets=None):
    selected_targets = ('notion', 'telegram') if targets is None else tuple(targets)
    if any(target not in ('notion', 'telegram') for target in selected_targets):
        raise ValueError('targets must contain only notion and/or telegram')
    store = DigestStore(state_dir)
    archived = store.get(item['id'])
    if archived is None:
        raise ValueError('archive the digest before delivery')
    # The archive is authoritative; callers cannot send altered text for its ID.
    item = archived
    results = {}
    # Always synchronize Notion first when both transports were requested so the
    # Telegram message can link to its verified full record.
    for target in ('notion', 'telegram'):
        if target not in selected_targets:
            continue
        if not store.claim_delivery(item['id'], target, reconcile_unknown=(target == 'notion')):
            results[target] = store.delivery(item['id'], target)
            continue
        try:
            if target == 'notion':
                if (notion_config or {}).get('transport') == 'codex_apps':
                    from notion_sync import sync_via_codex
                    result = sync_via_codex(item, Path(state_dir), notion_config)
                    if result.get('status') != 'sent':
                        raise DeliveryError('notion_connector_' + result.get('status', 'unknown'),
                                            ambiguous=result.get('status') != 'failed', reason=result.get('reason'))
                    receipt = {**result, 'url': result.get('page_url', '')}
                else:
                    receipt = sync_notion(item, notion_config or {})
            else:
                notion = store.delivery(item['id'], 'notion')
                notion_url = notion['receipt'].get('url', '') if notion and notion['status'] == 'sent' else ''
                receipt = send_telegram(item, state_dir, notion_url)
            results[target] = store.finish_delivery(item['id'], target, 'sent', receipt)
        except DeliveryError as error:
            status = 'unconfigured' if error.code.endswith('_unconfigured') else ('unknown' if error.ambiguous else 'failed')
            receipt = {'code': error.code}
            if error.reason:
                receipt['reason'] = error.reason
            results[target] = store.finish_delivery(item['id'], target, status, receipt)
        except Exception:
            # A programming/receipt error after a write may still mean delivered.
            results[target] = store.finish_delivery(item['id'], target, 'unknown',
                                                   {'code': target + '_unexpected_error'})
    return results
