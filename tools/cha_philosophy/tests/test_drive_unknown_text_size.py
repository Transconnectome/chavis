"""An absent metadata size is not proof that a stored text file is empty."""
import json
import subprocess
from pathlib import Path

import pytest

from tools.cha_philosophy import connectors as c


META={"id":"f","mimeType":"text/markdown","modifiedTime":"2026-09-12T00:00:00Z"}


def client_for(payload,after=None):
    paths=[];commands=[]
    def runner(argv,**kwargs):
        command=argv[4:6];commands.append(command)
        if command==['drive','download']:
            path=Path(next(a.removeprefix('--out=') for a in argv if a.startswith('--out=')))
            paths.append(path)
            assert path.parent.stat().st_mode & 0o077==0
            assert kwargs['preexec_fn'] is c._document_file_limit
            path.write_bytes(payload)
            return subprocess.CompletedProcess(argv,0,json.dumps({'path':str(path)}),'')
        assert command==['drive','get']
        return subprocess.CompletedProcess(argv,0,json.dumps({'file':META if after is None else after}),'')
    return c.GogClient('prof@example.invalid',runner=runner),paths,commands


@pytest.mark.parametrize('payload',[b'', '원문\r\nSecond line\rLast'.encode('utf-8')])
def test_missing_size_is_bounded_downloaded_and_revision_checked(payload):
    client,paths,commands=client_for(payload)
    result=client.read_document(META)
    assert result['extracted_text']==payload.decode('utf-8')
    assert result['extraction']['downloaded_bytes']==len(payload)
    assert result['extraction']['empty_file'] is (not payload)
    assert result['extraction']['size_metadata_available'] is False
    assert result['extraction']['revision_rechecked_after_download'] is True
    assert commands==[['drive','download'],['drive','get']]
    assert not paths[0].parent.exists()


@pytest.mark.parametrize('update,code',[
    ({'modifiedTime':'2026-09-13T00:00:00Z'},'document_changed_during_read'),
    ({'mimeType':'application/pdf'},'document_changed_during_read'),
    ({'id':'other'},'document_changed_during_read'),
    ({'trashed':True},'source_access_denied'),
])
def test_empty_download_does_not_hide_changed_or_trashed_source(update,code):
    client,paths,_=client_for(b'',after={**META,**update})
    with pytest.raises(c.ConnectorError,match=code):client.read_document(META)
    assert not paths[0].parent.exists()


def test_absent_size_still_obeys_download_budget(monkeypatch):
    monkeypatch.setattr(c,'_DOCUMENT_BYTES',128)
    client,paths,commands=client_for(b'x'*129)
    with pytest.raises(c.ConnectorError,match='document_download_invalid'):
        client.read_document(META)
    assert commands==[['drive','download']]
    assert not paths[0].parent.exists()


@pytest.mark.parametrize('payload,size',[(b'',0),(b'actual text','11')])
def test_final_size_when_available_must_match_download(payload,size):
    client,_,_=client_for(payload,after={**META,'size':size})
    assert client.read_document(META)['extraction']['downloaded_bytes']==len(payload)


@pytest.mark.parametrize('payload,size',[(b'','123'),(b'truncated','100'),(b'longer','0')])
def test_final_size_contradiction_rejects_apparent_empty_or_truncated_file(payload,size):
    client,paths,_=client_for(payload,after={**META,'size':size})
    with pytest.raises(c.ConnectorError,match='document_download_invalid'):
        client.read_document(META)
    assert not paths[0].parent.exists()


@pytest.mark.parametrize('size',[True,False,-1,'-1','1.5',1.5,'unknown'])
def test_invalid_final_size_does_not_verify_download(size):
    client,_,_=client_for(b'',after={**META,'size':size})
    with pytest.raises(c.ConnectorError,match='document_size_metadata_required'):
        client.read_document(META)


def test_final_size_over_budget_fails_even_if_download_is_empty(monkeypatch):
    monkeypatch.setattr(c,'_DOCUMENT_BYTES',128)
    client,_,_=client_for(b'',after={**META,'size':'129'})
    with pytest.raises(c.ConnectorError,match='document_download_budget_exceeded'):
        client.read_document(META)


@pytest.mark.parametrize('meta',[
    {'id':'f','mimeType':'text/markdown'},
    {**META,'modifiedTime':''},
    {**META,'size':'not-a-size'},
    {**META,'mimeType':'application/pdf'},
    {**META,'mimeType':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'},
])
def test_unknown_revision_or_nontext_format_does_not_use_this_path(meta):
    client,_,commands=client_for(b'')
    with pytest.raises(c.ConnectorError,match='document_size_metadata_required'):
        client.read_document(meta)
    assert not commands
