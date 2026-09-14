"""Resume storage uses the current private store and follows metadata withdrawal."""
from unittest.mock import Mock

import pytest

from tools.cha_philosophy.connectors import ConnectorError,normalize_drive_document
from tools.cha_philosophy.store import Store,digest
from tools.cha_philosophy.sync import sync,sync_drive


CONFIG={'account':'prof@example.invalid','drive_file_ids':['file'],
        'drive_query':'synthetic-query','drive_account_is_professor':True}
META={'id':'file','mimeType':'application/vnd.google-apps.document',
      'modifiedTime':'2026-09-12T00:00:00Z','_account_id':CONFIG['account']}


@pytest.fixture
def store(tmp_path):
    value=Store(tmp_path/'private')
    yield value
    value.close()


def test_live_drive_adapter_receives_current_store_for_private_resume(store,monkeypatch):
    client=Mock();factory=Mock(return_value=client)
    monkeypatch.setattr('tools.cha_philosophy.sync.GogClient',factory)
    operation=Mock(return_value={'platform':'drive','state':'partial'})
    monkeypatch.setattr('tools.cha_philosophy.sync.sync_drive',operation)
    sync(store,CONFIG,'drive',1)
    factory.assert_called_once_with(CONFIG['account'],document_cache_home=store.home)
    operation.assert_called_once_with(store,CONFIG,client,1)


def existing_body(store):
    source=normalize_drive_document(META,{'extracted_text':'Previous body.',
                                        'extraction':{'format':'synthetic'}})
    store.upsert(source,verified_remote=True)
    return source['source_id']


@pytest.mark.parametrize('withdrawal',['source_access_denied','source_not_found','trashed'])
def test_metadata_withdrawal_revokes_body_and_discards_pending_cache(store,withdrawal):
    sid=existing_body(store);client=Mock(account=CONFIG['account'])
    if withdrawal=='trashed':client.get_drive_metadata.return_value={**META,'trashed':True}
    else:client.get_drive_metadata.side_effect=ConnectorError(withdrawal)
    client.discard_document_cache.return_value={'state':'discarded'}
    result=sync_drive(store,CONFIG,client,1)
    assert store.get_source(sid)['status']=='unavailable'
    assert result['processed_files']==1
    client.discard_document_cache.assert_called_once_with('file')
    client.read_document.assert_not_called()


def test_comment_only_denial_does_not_discard_readable_document_cache(store):
    sid=existing_body(store);client=Mock(account=CONFIG['account'])
    client.get_drive_metadata.return_value=dict(META)
    client.read_document.return_value={'extracted_text':'Current body.',
                                       'extraction':{'format':'synthetic'}}
    client.iter_drive_comments.side_effect=ConnectorError('source_access_denied')
    result=sync_drive(store,CONFIG,client,1)
    assert result['processed_files']==1
    assert store.get_source(sid)['status']=='active'
    client.discard_document_cache.assert_not_called()


@pytest.mark.parametrize('code',['document_resume_busy','document_resume_cache_invalid',
                                  'document_resume_write_failed','document_resume_invalidated'])
def test_resume_failure_preserves_pending_file_and_prior_body(store,code):
    from tools.cha_philosophy.refresh import _source_summary
    sid=existing_body(store);before=store.get_source(sid)
    client=Mock(account=CONFIG['account']);client.get_drive_metadata.return_value=dict(META)
    client.read_document.side_effect=ConnectorError(code)
    with pytest.raises(ConnectorError,match=code):sync_drive(store,CONFIG,client,1)
    scope='drive:'+CONFIG['account']+':'+digest(CONFIG['drive_query'])[:12]
    checkpoint=store.checkpoint(scope)
    assert checkpoint['pending']==['file'] and checkpoint['files']==0
    assert store.get_source(sid)==before
    client.iter_drive_comments.assert_not_called()
    summary=_source_summary('drive',{'results':[{'platform':'drive','state':'error','error_code':code}]})
    assert summary['error_code']==code
    assert summary['has_errors'] and summary['attempt_succeeded'] is False
