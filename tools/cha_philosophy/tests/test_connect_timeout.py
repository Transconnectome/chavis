"""A failed TCP connection may retry once; auth and partial files cannot."""
import json
import subprocess

import pytest

from tools.cha_philosophy.connectors import ConnectorError, GogClient


TIMEOUT = 'Post "https://example.invalid/token?secret=SYNTHETIC_SECRET": dial tcp: i/o timeout (Client.Timeout exceeded)'


@pytest.mark.parametrize("command,args", [(('gmail', 'thread', 'get'), ['t', '--full']),
                                           (('drive', 'get'), ['f']),
                                           (('drive', 'comments', 'list'), ['f'])])
def test_connection_timeout_retries_exact_read_once(command, args, monkeypatch):
    calls, sleeps = [], []
    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 8, '', TIMEOUT) if len(calls) == 1 else subprocess.CompletedProcess(argv, 0, '{"id":"synthetic"}', '')
    monkeypatch.setattr('tools.cha_philosophy.connectors.time.sleep', sleeps.append)
    result = GogClient('prof@example.invalid', runner=runner)._call(command, args)
    assert result == {'id': 'synthetic'}
    assert calls[0] == calls[1]
    assert sleeps == [2]


def test_second_timeout_remains_failure_without_third_attempt_or_secret(monkeypatch):
    calls, sleeps = [], []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 8, '', TIMEOUT)
    monkeypatch.setattr('tools.cha_philosophy.connectors.time.sleep', sleeps.append)
    with pytest.raises(ConnectorError) as error:
        GogClient('prof@example.invalid', runner=runner).get_drive_file('f')
    assert error.value.code == 'gog_connect_timeout'
    assert error.value.operation == 'drive.get'
    assert 'SYNTHETIC_SECRET' not in str(error.value)
    assert len(calls) == 2 and sleeps == [2]


@pytest.mark.parametrize('stderr,code', [('invalid_grant ' + TIMEOUT, 'gog_auth_required'),
                                        ('403 Forbidden ' + TIMEOUT, 'source_access_denied'),
                                        ('429 rateLimitExceeded ' + TIMEOUT, 'gog_rate_limited'),
                                        ('quotaExceeded ' + TIMEOUT, 'gog_quota_exceeded'),
                                        ('network timeout SYNTHETIC_SECRET', 'gog_read_failed')])
def test_other_failures_never_gain_transport_retry(stderr, code, monkeypatch):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', stderr)
    def no_sleep(_):
        pytest.fail('nontransport error attempted a retry')
    monkeypatch.setattr('tools.cha_philosophy.connectors.time.sleep', no_sleep)
    with pytest.raises(ConnectorError) as error:
        GogClient('prof@example.invalid', runner=runner).get_drive_file('f')
    assert error.value.code == code and len(calls) == 1


@pytest.mark.parametrize('prefix', ['HTTP 401', '429 rateLimitExceeded', 'rate limit exceeded', 'quota exceeded',
                                    'userRateLimitExceeded', 'dailyLimitExceeded', 'storageQuotaExceeded'])
def test_gmail_error_responses_do_not_gain_retry_from_embedded_timeout(prefix, monkeypatch):
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, '', prefix + ' ' + TIMEOUT)
    def no_sleep(_):
        pytest.fail('response-level error attempted a retry')
    monkeypatch.setattr('tools.cha_philosophy.connectors.time.sleep', no_sleep)
    with pytest.raises(ConnectorError) as error:
        GogClient('prof@example.invalid', runner=runner).get_gmail_thread('t')
    assert error.value.code != 'gog_connect_timeout'
    assert len(calls) == 1


@pytest.mark.parametrize('partial_name', ['source.bin', 'source.pptx', None])
def test_download_retry_requires_empty_private_output_directory(tmp_path, monkeypatch, partial_name):
    tmp_path.chmod(0o700)
    output = tmp_path / 'source.bin'
    calls, sleeps = [], []
    def runner(argv, **kwargs):
        calls.append(argv)
        if len(calls) == 1:
            if partial_name:
                (tmp_path / partial_name).write_bytes(b'partial-synthetic-content')
            return subprocess.CompletedProcess(argv, 8, '', TIMEOUT)
        output.write_bytes(b'complete-synthetic-content')
        return subprocess.CompletedProcess(argv, 0, json.dumps({'path': str(output)}), '')
    monkeypatch.setattr('tools.cha_philosophy.connectors.time.sleep', sleeps.append)
    client = GogClient('prof@example.invalid', runner=runner)
    if partial_name:
        with pytest.raises(ConnectorError) as error:
            client._call(('drive', 'download'), ['f', '--out=' + str(output)], _download_root=str(tmp_path))
        assert error.value.code == 'gog_connect_timeout'
        assert (tmp_path / partial_name).read_bytes() == b'partial-synthetic-content'
        assert len(calls) == 1 and sleeps == []
    else:
        assert client._call(('drive', 'download'), ['f', '--out=' + str(output)], _download_root=str(tmp_path)) == {'path': str(output)}
        assert output.read_bytes() == b'complete-synthetic-content'
        assert len(calls) == 2 and sleeps == [2]
