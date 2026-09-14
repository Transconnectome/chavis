import zipfile

import pytest

from tools.cha_philosophy.connectors import ConnectorError, _office_text, normalize_drive_document


WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def word_xml(text):
    return f'<w:document xmlns:w="{WORD_NS}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'


def test_windows_member_names_recover_body_and_header_without_authorship_promotion(tmp_path):
    path = tmp_path / "source.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word\\document.xml", word_xml("연구 질문과 실제 근거"))
        archive.writestr("word\\header1.xml", word_xml("Header context"))
    document = _office_text(path, presentation=False)
    assert "연구 질문과 실제 근거" in document["extracted_text"]
    assert "Header context" in document["extracted_text"]
    assert document["extraction"]["part_ids"] == ["word/document.xml", "word/header1.xml"]
    assert document["extraction"]["archive_path_separators_normalized"] is True
    source = normalize_drive_document(
        {"id": "f", "_account_id": "prof@example.org",
         "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}, document)
    assert (source["authorship"], source["authored_text"]) == ("unverified", "")


def test_mixed_separator_aliases_are_rejected_before_reading(tmp_path):
    path = tmp_path / "source.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word\\document.xml", word_xml("first"))
        archive.writestr("word/document.xml", word_xml("different"))
    with pytest.raises(ConnectorError, match="document_archive_duplicate_parts"):
        _office_text(path, presentation=False)


def test_presentation_relationships_resolve_to_windows_named_parts(tmp_path):
    path = tmp_path / "source.pptx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("ppt\\presentation.xml", '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId r:id="rId1"/></p:sldIdLst></p:presentation>')
        archive.writestr("ppt\\_rels\\presentation.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="slide" Target="slides/slide1.xml"/></Relationships>')
        archive.writestr("ppt\\slides\\slide1.xml", '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:p><a:r><a:t>Preserve the actual slide</a:t></a:r></a:p></p:sld>')
    document = _office_text(path, presentation=True)
    assert document["extraction"]["slide_count"] == 1
    assert document["extraction"]["part_ids"] == ["ppt/slides/slide1.xml"]
    assert "Preserve the actual slide" in document["extracted_text"]
    assert document["extraction"]["archive_path_separators_normalized"] is True
