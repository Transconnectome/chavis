"""Office binary capacity stays separate from XML inflation and authored text."""
import json
import subprocess
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from tools.cha_philosophy import connectors as c


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def word_xml(text):
    return f'<w:document xmlns:w="{WORD_NS}"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:document>'


def test_office_binary_budget_accepts_large_container_and_cleans_private_temp():
    paths = []
    def runner(argv, **kwargs):
        path = Path(next(x.removeprefix("--out=") for x in argv if x.startswith("--out=")))
        paths.append(path)
        assert kwargs["preexec_fn"] is c._office_download_file_limit
        assert path.parent.stat().st_mode & 0o077 == 0
        with path.open("wb") as handle:
            handle.seek(2048)  # A ZIP may contain a prefix; avoid large fixtures.
            handle.write(b"\x00")
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("word/document.xml", word_xml("Readable context"))
        return subprocess.CompletedProcess([], 0, "{}", "")
    client = c.GogClient("test@example.invalid", runner=runner)
    with patch.object(c, "_DOCUMENT_BYTES", 1024), patch.object(c, "_OFFICE_DOWNLOAD_BYTES", 8192):
        doc = client.read_document({"id": "f", "mimeType": DOCX_MIME, "size": "4096"})
    assert "Readable context" in doc["extracted_text"]
    assert not paths[0].parent.exists()


def test_download_metadata_caps_and_gmail_scope_are_not_lifted_together():
    runner = Mock()
    client = c.GogClient("test@example.invalid", runner=runner)
    for mime, size in [(PPTX_MIME, c._OFFICE_DOWNLOAD_BYTES + 1),
                       ("application/pdf", c._DOCUMENT_BYTES + 1), ("text/plain", c._DOCUMENT_BYTES + 1)]:
        with pytest.raises(c.ConnectorError, match="document_download_budget_exceeded"):
            client.read_document({"id": "f", "mimeType": mime, "size": size})
    with pytest.raises(c.ConnectorError, match="document_download_budget_invalid"):
        client._call(("gmail", "attachment"), ["m", "a"], _office_download=True)
    runner.assert_not_called()


def test_private_office_download_still_limits_command_stdout(tmp_path):
    tmp_path.chmod(0o700)
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({"unexpected": "x" * 2048}), ""))
    client = c.GogClient("test@example.invalid", runner=runner)
    with patch.object(c, "_DOCUMENT_BYTES", 1024):
        with pytest.raises(c.ConnectorError, match="document_response_budget_exceeded"):
            client._call(("drive", "download"), ["f", "--out=" + str(tmp_path / "source.bin")],
                         _download_root=str(tmp_path), _office_download=True)


def test_office_download_resource_limit_respects_host_hard_limit():
    for hard, expected in [(c.resource.RLIM_INFINITY, c._OFFICE_DOWNLOAD_BYTES), (1024, 1024)]:
        with patch.object(c.resource, "getrlimit", return_value=(512, hard)), patch.object(c.resource, "setrlimit") as set_limit:
            c._office_download_file_limit()
        set_limit.assert_called_once_with(c.resource.RLIMIT_FSIZE, (expected, hard))
    with patch.object(c.resource, "getrlimit", return_value=(512, c.resource.RLIM_INFINITY)), patch.object(c.resource, "setrlimit") as set_limit:
        c._document_file_limit()
    set_limit.assert_called_once_with(c.resource.RLIMIT_FSIZE, (c._DOCUMENT_BYTES, c.resource.RLIM_INFINITY))


def test_large_unread_media_is_never_inflated_or_counted_as_xml(tmp_path):
    path = tmp_path / "media.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", word_xml("Readable text"))
        archive.writestr("word/media/image.bin", b"x" * 100_000)
    opened = []
    original_open = zipfile.ZipFile.open
    def track_open(self, name, *args, **kwargs):
        opened.append(name.filename if isinstance(name, zipfile.ZipInfo) else name)
        return original_open(self, name, *args, **kwargs)
    with patch.object(c, "_DOCUMENT_UNPACKED_BYTES", 1024), patch.object(zipfile.ZipFile, "open", track_open):
        doc = c._office_text(path, presentation=False)
    assert "Readable text" in doc["extracted_text"]
    assert opened == ["word/document.xml"]


@pytest.mark.parametrize("split_parts", [False, True])
def test_xml_inflation_budget_rejects_one_bomb_or_cumulative_parts(tmp_path, split_parts):
    path = tmp_path / "bomb.docx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", word_xml("x" * (500 if split_parts else 5000)))
        if split_parts:
            archive.writestr("word/header1.xml", word_xml("x" * 500))
    with patch.object(c, "_DOCUMENT_UNPACKED_BYTES", 1024):
        with pytest.raises(c.ConnectorError, match="document_archive_budget_exceeded"):
            c._office_text(path, presentation=False)


def test_relationship_to_non_xml_member_cannot_bypass_inflation_budget(tmp_path):
    path = tmp_path / "bomb.pptx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("ppt/presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldId r:id="s"/></p:presentation>')
        archive.writestr("ppt/_rels/presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="s" Target="slides/bomb.bin"/></Relationships>')
        archive.writestr("ppt/slides/bomb.bin", b"x" * 5000)
    with patch.object(c, "_DOCUMENT_UNPACKED_BYTES", 1024):
        with pytest.raises(c.ConnectorError, match="document_archive_budget_exceeded"):
            c._office_text(path, presentation=True)


def test_archive_entry_and_final_text_limits_remain_enforced(tmp_path):
    path = tmp_path / "many.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for number in range(4097):
            archive.writestr("media/" + str(number), b"")
    with pytest.raises(c.ConnectorError, match="document_archive_budget_exceeded"):
        c._office_text(path, presentation=False)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", word_xml("x" * 2000))
    with patch.object(c, "_DOCUMENT_BYTES", 1024):
        with pytest.raises(c.ConnectorError, match="document_text_budget_exceeded"):
            c._office_text(path, presentation=False)


def test_xml_budget_failure_cleans_downloaded_office_file():
    paths = []
    def runner(argv, **kwargs):
        path = Path(next(x.removeprefix("--out=") for x in argv if x.startswith("--out=")))
        paths.append(path)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", word_xml("x" * 5000))
        return subprocess.CompletedProcess([], 0, "{}", "")
    with patch.object(c, "_DOCUMENT_UNPACKED_BYTES", 1024):
        with pytest.raises(c.ConnectorError, match="document_archive_budget_exceeded"):
            c.GogClient("test@example.invalid", runner=runner).read_document({"id": "f", "mimeType": DOCX_MIME, "size": "4096"})
    assert not paths[0].parent.exists()
