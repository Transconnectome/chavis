import copy
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools.cha_philosophy import connectors as c
from tools.cha_philosophy import pdf_ocr
from tools.cha_philosophy.store import Store
from tools.cha_philosophy.sync import sync


PDF = b"%PDF-1.7 synthetic image only"
META = {"id": "f", "mimeType": "application/pdf", "size": str(len(PDF)),
        "modifiedTime": "2026-09-01T00:00:00Z"}
OCR = {"extracted_text": "[page 1]\nRecognized context",
       "extraction": {"format": "pdf_ocr", "text_scope": "full_page_ocr_all_pages",
                      "partial_text": True, "page_count": 1, "all_pages_processed": True,
                      "limitations": ["ocr_transcription_not_independently_verified"]}}


def client(monkeypatch, *, final=None, text="", ocr_error=None):
    calls = []

    def gog(argv, **kwargs):
        calls.append(argv)
        if "download" in argv:
            target = Path(next(arg[6:] for arg in argv if arg.startswith("--out=")))
            target.write_bytes(PDF)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"path": str(target)}), "")
        assert "get" in argv
        if isinstance(final, subprocess.CompletedProcess):
            return final
        return subprocess.CompletedProcess(argv, 0, json.dumps({"file": META if final is None else final}), "")

    def convert(argv, **kwargs):
        assert argv[0] == "pdftotext" and kwargs["shell"] is False
        Path(argv[-1]).write_text(text)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(c.subprocess, "run", convert)
    backend = Mock(return_value=copy.deepcopy(OCR), side_effect=ocr_error)
    monkeypatch.setattr(pdf_ocr, "extract_pdf_ocr", backend)
    return c.GogClient("prof@example.org", runner=gog, pdf_ocr_tessdata="/synthetic/tessdata"), backend, calls


def test_ocr_fallback_is_rechecked_and_remains_unverified_context(monkeypatch):
    reader, backend, calls = client(monkeypatch)
    document = reader.read_document(META)
    backend.assert_called_once()
    assert backend.call_args.args[1] == "/synthetic/tessdata"
    assert document["extraction"]["drive_metadata_rechecked"] is True
    assert document["extraction"]["partial_text"] is True
    assert len(calls) == 2
    source = c.normalize_drive_document({**META, "_account_id": "prof@example.org"}, document)
    assert (source["authorship"], source["authored_text"]) == ("unverified", "")


def test_extractable_pdf_keeps_existing_text_without_ocr(monkeypatch):
    reader, backend, calls = client(monkeypatch, text="Embedded original text")
    document = reader.read_document(META)
    backend.assert_not_called()
    assert document["extracted_text"] == "Embedded original text"
    assert document["extraction"]["format"] == "pdf"
    assert len(calls) == 1


@pytest.mark.parametrize("final", [
    {**META, "modifiedTime": "2026-09-02T00:00:00Z"},
    {**META, "id": "other"},
    {**META, "mimeType": "text/plain"},
    {**META, "size": str(len(PDF) + 1)},
])
def test_ocr_result_cannot_be_bound_to_a_changed_file(monkeypatch, final):
    reader, backend, _ = client(monkeypatch, final=final)
    with pytest.raises(c.ConnectorError, match="document_changed_during_read"):
        reader.read_document(META)
    backend.assert_called_once()


@pytest.mark.parametrize("final", [
    {**META, "trashed": True},
    subprocess.CompletedProcess([], 1, "", "403 Forbidden PRIVATE"),
    subprocess.CompletedProcess([], 1, "", "404 Not Found PRIVATE"),
])
def test_final_file_loss_is_marked_for_all_source_revocation(monkeypatch, final):
    reader, _, _ = client(monkeypatch, final=final)
    with pytest.raises(c.ConnectorError) as caught:
        reader.read_document(META)
    assert caught.value.file_access_withdrawn is True
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("meta,code", [
    ({**META, "size": str(len(PDF) + 1)}, "document_download_invalid"),
    ({k: v for k, v in META.items() if k != "modifiedTime"}, "document_pdf_ocr_revision_required"),
])
def test_unbound_or_incomplete_input_does_not_start_ocr(monkeypatch, meta, code):
    reader, backend, _ = client(monkeypatch)
    with pytest.raises(c.ConnectorError, match=code):
        reader.read_document(meta)
    backend.assert_not_called()


def test_ocr_failure_is_an_explicit_document_gap(monkeypatch):
    code = sorted(pdf_ocr.SAFE_ERROR_CODES)[0]
    reader, _, _ = client(monkeypatch, ocr_error=pdf_ocr.PdfOcrError(code))
    with pytest.raises(c.ConnectorError) as caught:
        reader.read_document(META)
    assert caught.value.code == "document_" + code


def test_sync_passes_only_explicitly_configured_ocr_path(tmp_path, monkeypatch):
    store = Store(tmp_path / "private")
    created = Mock(return_value=object())
    monkeypatch.setattr("tools.cha_philosophy.sync.GogClient", created)
    monkeypatch.setattr("tools.cha_philosophy.sync.sync_drive",
                        lambda *args: {"platform": "drive", "state": "partial"})
    config = {"account": "prof@example.org", "drive_pdf_ocr_tessdata": "/synthetic/tessdata"}
    sync(store, config, platform="drive", limit=1)
    created.assert_called_once_with("prof@example.org", document_cache_home=store.home,
                                    pdf_ocr_tessdata="/synthetic/tessdata")
    store.close()
