"""CLI outputs preserve existing files and report failures without private text."""
import json
import pytest

from tools.cha_philosophy.cli import main, save_or_emit


def test_explicit_output_is_private_and_existing_manuscript_is_preserved(tmp_path,capsys):
    output=tmp_path/"result.json"
    save_or_emit({"status":"prepared","text":"private result"},output)
    assert output.stat().st_mode & 0o077 == 0
    prior=output.read_bytes()
    try:save_or_emit({"status":"different"},output)
    except FileExistsError:pass
    else:raise AssertionError("existing result was overwritten")
    assert output.read_bytes()==prior
    assert "private result" not in capsys.readouterr().out


def test_cli_exception_does_not_leak_request_or_backend_message(tmp_path,monkeypatch,capsys):
    request=tmp_path/"request.txt";request.write_text("SYNTHETIC_PRIVATE_REQUEST")
    def fail(*args,**kwargs):raise ValueError("SYNTHETIC_SECRET_BACKEND_MESSAGE")
    monkeypatch.setattr("tools.cha_philosophy.task.prepare_task",fail)
    assert main(["--home",str(tmp_path/"store"),"prepare","review","--request",str(request)])==1
    output=capsys.readouterr().out
    assert json.loads(output)=={"status":"error","error_type":"ValueError"}
    assert "SYNTHETIC" not in output


def test_sync_cli_returns_failure_when_source_sync_failed(tmp_path,monkeypatch,capsys):
    config=tmp_path/"config.json";config.write_text("{}")
    monkeypatch.setattr("tools.cha_philosophy.sync.sync",lambda *a,**kw:{"results":[{"platform":"gmail","state":"error","error_code":"gog_auth_required"}]})
    assert main(["--home",str(tmp_path/"store"),"sync","--config",str(config),"--platform","gmail"])==1
    assert json.loads(capsys.readouterr().out)["results"][0]["error_code"]=="gog_auth_required"


def test_refresh_cli_reports_top_level_failure(tmp_path,monkeypatch,capsys):
    config=tmp_path/"config.json";config.write_text("{}")
    monkeypatch.setattr("tools.cha_philosophy.refresh.refresh",lambda *a,**kw:{"state":"error","overall_complete":False})
    assert main(["--home",str(tmp_path/"store"),"refresh","--config",str(config)])==1


@pytest.mark.parametrize("failure", [
    {"sources":{"gmail":{"state":"error","error_code":"text_body_requires_attachment_fetch"}}},
    {"inference":{"state":"error","error_code":"inference_cycle_failed"}},
    {"sources":{"teams":{"state":"partial","channel_sync":{"state":"complete_configured_channels"},
                            "chat_sync":{"state":"error","error_code":"gog_auth_required"}}}},
    {"inference":{"state":"partial","completed_sources":1,"failed_sources":1}},
])
def test_refresh_cli_fails_finished_cycle_with_nested_failure(tmp_path,monkeypatch,capsys,failure):
    config=tmp_path/"config.json";config.write_text("{}")
    result={"state":"finished_bounded_cycle","overall_complete":False,**failure}
    monkeypatch.setattr("tools.cha_philosophy.refresh.refresh",lambda *a,**kw:result)
    assert main(["--home",str(tmp_path/"store"),"refresh","--config",str(config)])==1
    assert json.loads(capsys.readouterr().out)==result


def test_refresh_cli_allows_successful_bounded_progress(tmp_path,monkeypatch,capsys):
    config=tmp_path/"config.json";config.write_text("{}")
    result={"state":"finished_bounded_cycle","overall_complete":False,
            "sources":{"gmail":{"state":"partial","threads_processed":30}},
            "inference":{"state":"backend_unavailable"}}
    monkeypatch.setattr("tools.cha_philosophy.refresh.refresh",lambda *a,**kw:result)
    assert main(["--home",str(tmp_path/"store"),"refresh","--config",str(config)])==0
