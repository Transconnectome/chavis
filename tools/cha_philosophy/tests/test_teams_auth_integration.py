"""Auth mode selection, recurring refresh and explicitly interactive CLI routing."""
import io
import json
from unittest.mock import Mock

import pytest

from tools.cha_philosophy.cli import main
from tools.cha_philosophy.connectors import TeamsGraphClient
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.teams_auth import TeamsAuthError

CONFIG={'account':'prof@example.invalid','identities':['prof@example.invalid'],
        'lab_recipients':['student@example.invalid'],'teams_team_ids':['team'],
        'teams_tenant_id':'11111111-1111-1111-1111-111111111111',
        'teams_professor_ids':['22222222-2222-2222-2222-222222222222'],
        'teams_auth':{'mode':'device_code','client_id':'33333333-3333-3333-3333-333333333333'}}


def test_legacy_mode_preserves_external_token_route(tmp_path,monkeypatch):
    legacy=Mock(return_value='legacy-client');monkeypatch.setattr(TeamsGraphClient,'from_env',legacy)
    assert TeamsGraphClient.from_config({},tmp_path)=='legacy-client'
    legacy.assert_called_once_with()


@pytest.mark.parametrize('auth',[None,{}, {'mode':'other','client_id':'bad'},
                                  {'mode':'device_code','client_id':'bad'}])
def test_explicit_auth_never_falls_back_to_environment_token(auth,tmp_path,monkeypatch):
    monkeypatch.setenv('CHA_TEAMS_ACCESS_TOKEN','UNRELATED_TOKEN')
    fallback=Mock();monkeypatch.setattr(TeamsGraphClient,'from_env',fallback)
    with pytest.raises(TeamsAuthError):TeamsGraphClient.from_config({**CONFIG,'teams_auth':auth},tmp_path)
    fallback.assert_not_called()


def test_bound_auth_is_lazy_and_connected_to_each_graph_request(tmp_path,monkeypatch):
    auth=Mock();auth.acquire_silent.return_value='bound-token'
    factory=Mock(return_value=auth);monkeypatch.setattr('tools.cha_philosophy.teams_auth.TeamsAuth',factory)
    opener=Mock();opener.open.return_value=io.BytesIO(b'{"value":[]}')
    client=TeamsGraphClient.from_config(CONFIG,tmp_path,opener=opener)
    factory.assert_called_once_with(CONFIG,tmp_path);auth.acquire_silent.assert_not_called()
    assert client.get_json('/me/chats')=={'value':[]}
    auth.acquire_silent.assert_called_once_with(force_refresh=False)
    auth.login_device_code.assert_not_called()


def test_recurring_refresh_attempts_configured_silent_auth_without_env_token(tmp_path,monkeypatch):
    from tools.cha_philosophy.refresh import refresh
    monkeypatch.delenv('CHA_TEAMS_ACCESS_TOKEN',raising=False)
    calls=[]
    def sync(store,config,platform,limit):
        calls.append(platform)
        if platform=='teams':
            return {'results':[{'platform':'teams','state':'error','error_code':'teams_auth_login_required'}]}
        return {'results':[{'platform':platform,'state':'partial'}]}
    monkeypatch.setattr('tools.cha_philosophy.refresh.sync',sync)
    monkeypatch.setattr('tools.cha_philosophy.refresh.local_ollama_available',lambda:False)
    store=Store(tmp_path/'private')
    try:
        result=refresh(store,CONFIG,gmail_limit=1,drive_limit=1,distill_limit=0)
        assert sorted(calls)==['drive','gmail','teams']
        assert result['sources']['teams']['error_code']=='teams_auth_login_required'
        assert result['health']=='source_error'
        assert result['last_success_times']['teams'] is None
    finally:store.close()


def test_status_does_not_open_backend_or_claim_live_auth(tmp_path,monkeypatch,capsys):
    home=tmp_path/'private';home.mkdir();(home/'config.json').write_text('{}')
    backend=Mock(side_effect=AssertionError('must not open keyring'))
    monkeypatch.setattr('tools.cha_philosophy.teams_auth._backend_factory',backend)
    assert main(['--home',str(home),'teams-auth','status'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['state']=='not_configured' and result['live_auth_checked'] is False
    backend.assert_not_called()


def test_cli_login_refuses_redirected_service_context_before_backend(tmp_path,monkeypatch,capsys):
    home=tmp_path/'private';home.mkdir();(home/'config.json').write_text(json.dumps(CONFIG))
    auth=Mock();monkeypatch.setattr('tools.cha_philosophy.teams_auth.TeamsAuth',auth)
    assert main(['--home',str(home),'teams-auth','login'])==1
    result=json.loads(capsys.readouterr().out)
    assert result['error_code']=='teams_login_requires_interactive_terminal'
    auth.assert_not_called()


def test_interactive_cli_displays_only_validated_device_prompt(tmp_path,monkeypatch,capsys):
    import sys
    from tools.cha_philosophy.teams_auth import DeviceLoginPrompt
    home=tmp_path/'private';home.mkdir();(home/'config.json').write_text(json.dumps(CONFIG))
    for stream in (sys.stdin,sys.stdout,sys.stderr):monkeypatch.setattr(stream,'isatty',lambda:True)
    def login(display):
        display(DeviceLoginPrompt('SYNTHETIC_CODE'))
        return {'state':'authenticated','account_bound':True}
    auth=Mock();auth.login_device_code.side_effect=login
    monkeypatch.setattr('tools.cha_philosophy.teams_auth.TeamsAuth',Mock(return_value=auth))
    assert main(['--home',str(home),'teams-auth','login'])==0
    captured=capsys.readouterr()
    assert json.loads(captured.out)['state']=='authenticated'
    assert 'SYNTHETIC_CODE' not in captured.out
    assert captured.err=='Open https://microsoft.com/devicelogin and enter code SYNTHETIC_CODE\n'
    auth.acquire_silent.assert_not_called()
