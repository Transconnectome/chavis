"""Detached MIME body reads must preserve complete mail and exclude attachments."""
import base64
import json
import subprocess
from pathlib import Path

import pytest

from tools.cha_philosophy.connectors import ConnectorError,GogClient,normalize_gmail
from tools.cha_philosophy.tests.test_connectors import mail,thread


def fixture_runner(raw,bodies):
    calls=[];paths=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if 'attachment' not in argv:return subprocess.CompletedProcess(argv,0,json.dumps(raw),'')
        index=argv.index('attachment');aid=argv[index+2]
        path=Path(next(x.split('=',1)[1] for x in argv if x.startswith('--out=')))
        assert path.parent.stat().st_mode & 0o077==0
        assert kwargs['shell'] is False and 'preexec_fn' in kwargs
        paths.append(path);path.write_bytes(bodies[aid])
        return subprocess.CompletedProcess(argv,0,json.dumps({'path':str(path)}),'')
    return run,calls,paths


def test_detached_inline_body_is_complete_and_real_file_attachments_are_omitted():
    text='근거가 부족하면 결론을 보류하세요.'.encode()
    msg=mail('m');headers=msg['payload']['headers']
    msg['payload']={'mimeType':'multipart/mixed','headers':headers,'parts':[
        {'mimeType':'text/plain','body':{'attachmentId':'body','size':len(text)}},
        {'mimeType':'text/plain','filename':'student.txt','body':{'attachmentId':'file','size':100}},
        {'mimeType':'text/html','headers':[{'name':'Content-Disposition','value':'attachment'}],
         'body':{'attachmentId':'attached-html','size':100}}]}
    runner,calls,paths=fixture_runner(thread(msg),{'body':text})
    result=GogClient('prof@example.org',runner=runner).get_thread('thread-1')
    rows=normalize_gmail(result,{'prof@example.org'},{'student@example.org'})
    assert rows[0]['body']==text.decode() and rows[0]['authored_text']==text.decode()
    assert len(calls)==2 and len(paths)==1
    assert all(not p.parent.exists() for p in paths)


def test_incomplete_body_download_fails_and_cleans_private_temporary_file():
    msg=mail('m');msg['payload']['body']={'attachmentId':'body','size':50}
    runner,_,paths=fixture_runner(thread(msg),{'body':b'incomplete'})
    with pytest.raises(ConnectorError,match='gmail_body_download_mismatch'):
        GogClient('prof@example.org',runner=runner).get_thread('thread-1')
    assert paths and all(not p.parent.exists() for p in paths)


def test_oversized_detached_body_is_never_downloaded():
    msg=mail('m');msg['payload']['body']={'attachmentId':'body','size':10**9}
    runner,calls,_=fixture_runner(thread(msg),{})
    with pytest.raises(ConnectorError,match='gmail_body_budget_exceeded'):
        GogClient('prof@example.org',runner=runner).get_thread('thread-1')
    assert len(calls)==1


def test_attachment_command_cannot_download_outside_private_adapter_path():
    client=GogClient('prof@example.org',runner=lambda *a,**k:pytest.fail('must reject before execution'))
    with pytest.raises(ConnectorError,match='private_download_required'):
        client._call(('gmail','attachment'),['m','a','--out=/tmp/arbitrary.txt'])


def test_html_body_with_valueless_class_preserves_authored_and_quoted_boundary():
    # HTMLParser represents a present attribute without a value as None.
    msg=mail('m')
    html='<div class>직접 의견</div><blockquote class="gmail_quote"><div class>과거 타인의 의견</div></blockquote>'
    msg['payload']['mimeType']='text/html'
    msg['payload']['body']={'data':base64.urlsafe_b64encode(html.encode()).decode()}
    rows=normalize_gmail(thread(msg),{'prof@example.org'},{'student@example.org'})
    assert '직접 의견' in rows[0]['authored_text']
    assert '과거 타인의 의견' not in rows[0]['authored_text']
    assert '과거 타인의 의견' in rows[0]['body']
