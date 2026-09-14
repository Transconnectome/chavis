"""Explicit meeting credential labels, using entirely synthetic message data."""
from copy import deepcopy
import hashlib
import json

import pytest

from tools.cha_philosophy.connectors import sanitize_source
from tools.cha_philosophy.store import Store


MASK = "[REDACTED_SECRET]"


@pytest.mark.parametrize("label", [
    "Passcode", "PASSCODE", "Pass code", "Pass-code", "pass_code",
    "Meeting passcode", "Zoom Passcode", "meeting_password", "meetingPasscode",
    "패스코드", "패스 코드", "회의 암호", "미팅 비밀번호", "줌 패스코드", "접속암호",
])
def test_explicit_labels_redact_text_and_nested_scalar_fields(label):
    text = f"{label}: 927461\nSample size: 240; p = .032; accuracy = .91."
    raw = {"body": text, "authored_text": text,
           "metadata": {"quoted_file_content": {"value": text}, "meeting": {label: 927461}}}
    original = deepcopy(raw)
    result = sanitize_source(raw)
    expected = text.replace("927461", MASK)
    assert result["body"] == result["authored_text"] == expected
    assert result["metadata"]["quoted_file_content"]["value"] == expected
    assert result["metadata"]["meeting"][label] == MASK
    assert "927461" not in json.dumps(result)
    assert result["metadata"]["body_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert raw == original
    assert sanitize_source(result) == result


def test_forwarded_seminar_url_and_standalone_passcode_are_consistently_redacted():
    text = (
        "Forwarded seminar invitation — synthetic fixture\n"
        "Date: 2026-09-18, 14:30\n"
        "Join: https://example.invalid/j/8244567890?pwd=syntheticMeetingPassword&next=kept\n"
        "Meeting ID: 8244567890\n"
        "Passcode： 927461\n"
        "Results: n=240; r=.27; p=.032; 95% CI [.11, .43].\n"
        "Please distinguish association from causation."
    )
    result = sanitize_source({"body": text, "authored_text": text,
                              "metadata": {"description": text, "attachments": [{"meeting_passcode": "927461"}]}})
    rendered = json.dumps(result)
    assert "syntheticMeetingPassword" not in rendered and "927461" not in rendered
    assert result["body"] == result["authored_text"] == result["metadata"]["description"]
    assert "?pwd=" + MASK + "&next=kept" in result["body"]
    assert "Meeting ID: 8244567890" in result["body"]
    assert "Date: 2026-09-18, 14:30" in result["body"]
    assert "n=240; r=.27; p=.032; 95% CI [.11, .43]" in result["body"]
    assert result["metadata"]["attachments"][0]["meeting_passcode"] == MASK


def test_quoted_alphanumeric_credentials_preserve_adjacent_prose():
    text = 'Passcode="synthetic two word phrase"; sample size: 240\n회의 암호: 가상암호문자열; 근거를 확인하세요.'
    result = sanitize_source({"body": text, "authored_text": text})
    assert result["body"] == f"Passcode={MASK}; sample size: 240\n회의 암호: {MASK}; 근거를 확인하세요."
    assert result["body"] == result["authored_text"]
    assert sanitize_source(result) == result


@pytest.mark.parametrize("space", ["\u00a0", "\u202f"])
def test_html_nonbreaking_space_between_label_and_value_is_redacted(space):
    text = f"Passcode:{space}927461\n회의 암호：{space}가상암호문자열"
    result = sanitize_source({"body": text, "authored_text": text})
    assert result["body"] == f"Passcode:{space}{MASK}\n회의 암호：{space}{MASK}"
    assert result["authored_text"] == result["body"]
    assert sanitize_source(result) == result


def test_scientific_values_and_noncredential_codes_are_not_redacted():
    text = (
        "Sample: 927461; study code: 123456; meeting ID: 8244567890.\n"
        "Date: 2026-09-18; time: 14:30; accuracy: .91; p=.032; epoch=16.\n"
        "Passcode accuracy: .95; password length: 8; 코드: 123456; 암호화율: .92."
    )
    raw = {"body": text, "authored_text": text,
           "metadata": {"sample_size": 927461, "study_code": "123456", "meeting_id": "8244567890",
                        "passcode_accuracy": .95, "password_length": 8}}
    assert sanitize_source(raw) == raw


@pytest.mark.parametrize("pauses", [",", ",,,,", ";", ";;;;", ",;,,;"])
def test_one_tap_access_code_suffix_redacts_without_removing_phone_or_meeting_id(pauses):
    dial = f"+12025550123,,8244567890#{pauses}*927461#"
    expected = dial.replace("*927461#", f"*{MASK}#")
    raw = {"body": dial, "authored_text": dial, "metadata": {"invite": {"dial_string": dial}}}
    original = deepcopy(raw)
    result = sanitize_source(raw)
    assert result["body"] == result["authored_text"] == expected
    assert result["metadata"]["invite"]["dial_string"] == expected
    assert result["metadata"]["secret_redaction_count"] == 3
    assert result["metadata"]["body_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
    assert raw == original
    assert sanitize_source(result) == result


def test_multiple_one_tap_lines_preserve_url_and_secret_metadata_redaction_contracts():
    text = (
        "One tap mobile — synthetic fixture\n"
        "+12025550123,,8244567890#,,,,*927461# US\n"
        "+12025550124;;8244567890#;;;;*654321# US\n"
        "Join: https://example.invalid/j/8244567890?pwd=syntheticPassword&next=kept\n"
        "Study code: 927461; meeting ID: 8244567890."
    )
    expected = text.replace("*927461#", f"*{MASK}#").replace("*654321#", f"*{MASK}#").replace("syntheticPassword", MASK)
    result = sanitize_source({"body": text, "authored_text": text,
                              "metadata": {"description": text, "meeting_password": "syntheticPassword"}})
    assert result["body"] == result["authored_text"] == result["metadata"]["description"] == expected
    assert "Study code: 927461; meeting ID: 8244567890." in result["body"]
    assert "?pwd=" + MASK + "&next=kept" in result["body"]
    assert result["metadata"]["meeting_password"] == MASK
    assert result["metadata"]["secret_redaction_count"] == 10
    assert sanitize_source(sanitize_source(result)) == result


def test_ordinary_numeric_hash_and_incomplete_nondial_text_are_unchanged():
    text = (
        "Sample: 927461; meeting ID: 8244567890; telephone: +12025550123.\n"
        "Issue #927461; tag #meeting; 12 * 34; #,,,, ordinary list; *927461#.\n"
        "Incomplete forms: #,*927461 and #,,,927461#; literal #,,,*word#."
    )
    raw = {"body": text, "authored_text": text, "metadata": {"study_code": "927461"}}
    assert sanitize_source(raw) == raw


def test_explicit_local_resanitization_preserves_remote_time_and_invalidates_evidence(tmp_path, monkeypatch):
    store = Store(tmp_path / "synthetic-private")
    text = "최종 결과를 모두 보고하세요.\nPasscode: 927461"
    raw = {"source_id": "synthetic:legacy-seminar", "platform": "gmail", "scope": "synthetic",
           "author_id": "professor@example.test", "authorship": "direct", "status": "active",
           "body": text, "authored_text": text}
    try:
        # Reproduce a legacy row without opening or modifying any actual corpus.
        with monkeypatch.context() as legacy:
            legacy.setattr("tools.cha_philosophy.connectors.sanitize_source", deepcopy)
            store.upsert(raw, verified_remote=True)
        old = store.get_source(raw["source_id"])
        pid = store.add_principle({"statement": "최종 결과를 모두 보고한다.", "domains": ["review"],
                                   "evidence": [{"source_id": old["source_id"], "source_hash": old["source_hash"],
                                                 "quote": "최종 결과를 모두 보고하세요."}]})
        store.review(pid, "evidence_supported", "synthetic reviewer", "Synthetic original review")
        bundle = store.bundle("review", "최종 결과")
        assert store.upsert(old, verified_remote=False)
        current = store.get_source(old["source_id"])
        assert current["verified_at"] == old["verified_at"]
        assert "927461" not in current["body"] and "927461" not in current["authored_text"]
        assert current["source_hash"] != old["source_hash"]
        assert store.get_principle(pid)["status"] == "stale"
        assert store.validate_bundle(bundle)
        assert not store.upsert(current, verified_remote=False)
    finally:
        store.close()
