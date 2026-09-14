"""Offline whole-input app preparation, distinct from local inference limits."""
import json

import pytest

from tools.cha_philosophy.cli import main
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.task import TaskError, prepare_task


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "private")
    yield value
    value.close()


def manuscript_and_rubric():
    manuscript = "\n\n".join(
        "# " + section + "\n" +
        "This synthetic result records an association, not a causal effect. " * 90
        for section in ("Introduction", "Methods", "Results", "Discussion", "References")
    ) + "\n검증 대상 수치: n=123; r=.27. Citation: [1] Author (2025), pp. 4–7.\n"
    rubric = "Question: 40%. Methods: 60%. No other criteria.\n예외: 미보고는 0점이 아니라 확인 불가."
    return [{"kind": "manuscript", "content": manuscript},
            {"kind": "rubric", "content": rubric}]


@pytest.mark.parametrize("task", ["writing", "evaluation", "review"])
def test_app_prepares_full_manuscript_and_rubric_with_stable_source_ids(store, task, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("preparation must not construct an inference client")

    monkeypatch.setattr("tools.cha_philosophy.task.LocalOllamaClient", forbidden)
    inputs = manuscript_and_rubric()
    assert len(inputs[0]["content"].encode("utf-8")) > 30_000
    assert len(inputs[0]["content"].split()) > 4_500
    before = store.db.total_changes
    bundle = prepare_task(store, task, "Use the whole manuscript and the exact supplied rubric.", inputs)
    assert bundle["preparation"] == {
        "target": "app", "full_task_inputs_supplied": True,
        "local_context_fit": False, "app_context_fit_verified": False,
        "input_reading_verified": False, "task_completion_verified": False,
    }
    assert bundle["task_receipt_id"]
    assert bundle["selection"]["task_inputs_truncated"] is False
    sources = {s["kind"]: s for s in bundle["sources"] if s["platform"] == "task"}
    for item in inputs:
        assert sources[item["kind"]]["authored_text"] == item["content"]
        assert sources[item["kind"]]["authority"] == "current_user_supplied"
    again = prepare_task(store, task, "Use the whole manuscript and the exact supplied rubric.", inputs)
    assert bundle["sources"] == again["sources"]
    assert store.db.total_changes == before
    with pytest.raises(TaskError) as caught:
        prepare_task(store, task, "Use the whole manuscript and the exact supplied rubric.",
                     inputs, target="local")
    assert caught.value.code == "task_input_requires_chunking"
    assert caught.value.needs_chunking


def test_app_retains_all_three_selected_principles_and_their_complete_evidence(store):
    for i in range(3):
        quote = ("The evidence must support the stated conclusion. " * 100) + str(i)
        sid = f"synthetic:full-evidence:{i}"
        store.upsert({"source_id": sid, "platform": "gmail", "scope": "synthetic",
                      "authorship": "direct", "author_id": "synthetic-professor",
                      "status": "active", "body": quote, "authored_text": quote},
                     verified_remote=True)
        source = store.get_source(sid)
        pid = store.add_principle({
            "statement": f"Evidence supports the conclusion {i}", "domains": ["review"],
            "evidence": [{"source_id": sid, "source_hash": source["source_hash"], "quote": quote}],
        })
        store.review(pid, "evidence_supported", "synthetic reviewer", "synthetic fixture")
    bundle = prepare_task(store, "review", "Review the evidence supporting the conclusion.", [])
    assert len(bundle["principles"]) == 3
    assert bundle["selection"]["context_omitted_principle_ids"] == []
    assert bundle["preparation"]["local_context_fit"] is False
    for principle in bundle["principles"]:
        for evidence in principle["evidence"]:
            source = next(s for s in bundle["sources"] if s["source_id"] == evidence["source_id"])
            assert source["authored_text"] == evidence["quote"]
            assert source["authored_text"] == store.get_source(source["source_id"])["authored_text"]


@pytest.mark.parametrize("target", [None, "remote", [], 16])
def test_invalid_preparation_target_is_explicit(store, target):
    with pytest.raises(TaskError) as caught:
        prepare_task(store, "review", "Review this.", [], target=target)
    assert caught.value.code == "invalid_preparation_target"


def test_cli_defaults_to_app_full_input_and_offers_explicit_local_limit(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("CLI preparation must not construct an inference client")

    monkeypatch.setattr("tools.cha_philosophy.distill.LocalOllamaClient", forbidden)
    request = tmp_path / "request.txt"
    request.write_text("Review all sections and follow the complete rubric.")
    specs = []
    for item in manuscript_and_rubric():
        path = tmp_path / (item["kind"] + ".txt")
        path.write_text(item["content"], encoding="utf-8")
        specs += ["--input", item["kind"] + "=" + str(path)]
    args = ["--home", str(tmp_path / "cli-store"), "prepare", "review",
            "--request", str(request)] + specs
    output = tmp_path / "bundle.json"
    assert main(args + ["--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["preparation"]["target"] == "app"
    assert result["task_receipt_id"]
    assert result["task_instructions"]
    assert output.stat().st_mode & 0o077 == 0
    capsys.readouterr()
    local_output = tmp_path / "local-bundle.json"
    assert main(args + ["--target", "local", "--output", str(local_output)]) == 1
    error = json.loads(capsys.readouterr().out)
    assert error["error_code"] == "task_input_requires_chunking"
    assert error["needs_chunking"] is True
    assert not local_output.exists()
