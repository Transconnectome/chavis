"""The Graph reader renews bound credentials without beginning a login."""
import io
import json
import urllib.error
from unittest.mock import Mock

import pytest

from tools.cha_philosophy.connectors import ConnectorError,TeamsGraphClient


def response(value):
    return io.BytesIO(json.dumps(value).encode())


def http_error(status,headers=None):
    return urllib.error.HTTPError('https://graph.microsoft.com/v1.0/me',status,
                                  'PRIVATE_PROVIDER_MESSAGE',headers or {},None)


def test_401_refreshes_once_even_when_network_retry_budget_is_zero():
    provider=Mock(side_effect=['initial-token','renewed-token'])
    opener=Mock();opener.open.side_effect=[http_error(401),response({'id':'expected'})]
    client=TeamsGraphClient(token_provider=provider,opener=opener,max_retries=0)
    assert client.get_json('/me')=={'id':'expected'}
    assert provider.call_args_list[0].kwargs=={'force_refresh':False}
    assert provider.call_args_list[1].kwargs=={'force_refresh':True}
    assert [call.args[0].headers['Authorization'] for call in opener.open.call_args_list]==[
        'Bearer initial-token','Bearer renewed-token']


def test_repeated_401_stops_after_one_silent_refresh():
    provider=Mock(return_value='token');opener=Mock();opener.open.side_effect=http_error(401)
    with pytest.raises(ConnectorError,match='teams_auth_required'):
        TeamsGraphClient(token_provider=provider,opener=opener).get_json('/me')
    assert provider.call_count==opener.open.call_count==2


@pytest.mark.parametrize('challenge',[
    'Bearer error="insufficient_claims", claims="PRIVATE_CLAIMS"',
    'Bearer claims="PRIVATE_CLAIMS"',
])
def test_claims_challenge_does_not_start_or_retry_authentication(challenge):
    provider=Mock(return_value='token');opener=Mock();opener.open.side_effect=http_error(401,{'WWW-Authenticate':challenge})
    with pytest.raises(ConnectorError) as error:
        TeamsGraphClient(token_provider=provider,opener=opener).get_json('/me')
    assert str(error.value)=='teams_reauth_required'
    assert provider.call_count==opener.open.call_count==1


def test_throttle_retry_budget_is_independent_of_auth_refresh():
    provider=Mock(side_effect=['old','new']);opener=Mock();sleep=Mock()
    opener.open.side_effect=[http_error(429),http_error(401),http_error(503),response({'value':[]})]
    result=TeamsGraphClient(token_provider=provider,opener=opener,sleep=sleep,max_retries=2).get_json('/me/chats')
    assert result=={'value':[]} and provider.call_count==2 and sleep.call_count==2


@pytest.mark.parametrize('token',[None,'','\nINJECTED','token\rheader','token\x00bad','x'*65537])
def test_provider_invalid_token_stops_before_any_request(token):
    provider=Mock(return_value=token);opener=Mock()
    with pytest.raises(ConnectorError,match='teams_token_missing_or_invalid'):
        TeamsGraphClient(token_provider=provider,opener=opener).get_json('/me')
    opener.open.assert_not_called()


def test_provider_failure_and_invalid_url_never_leak_secrets():
    provider=Mock(side_effect=RuntimeError('PRIVATE_TOKEN'));opener=Mock()
    client=TeamsGraphClient(token_provider=provider,opener=opener)
    with pytest.raises(ConnectorError) as error:client.get_json('/me')
    assert str(error.value)=='teams_auth_provider_failed'
    provider.reset_mock()
    with pytest.raises(ConnectorError,match='graph_url_not_allowed'):
        client.get_json('https://attacker.invalid/v1.0/me')
    provider.assert_not_called();opener.open.assert_not_called()


def test_static_token_has_no_auth_refresh_fallback():
    opener=Mock();opener.open.side_effect=http_error(401)
    with pytest.raises(ConnectorError,match='teams_auth_required'):
        TeamsGraphClient('legacy-token',opener=opener).get_json('/me')
    assert opener.open.call_count==1
    with pytest.raises(ConnectorError,match='teams_token_provider_invalid'):
        TeamsGraphClient('legacy-token',token_provider=Mock())
