"""Validate two alternative Docs views from the explicitly selected local reader.

The preview is base text with pending suggestions rejected, not a review that
accepts any suggestion. Both views are unverified document context, not utterances.
"""
import re

from .connectors import ConnectorError, _DOCUMENT_BYTES


VIEW_MODES = ("PREVIEW_WITHOUT_SUGGESTIONS", "SUGGESTIONS_INLINE")
BASE_LIMITATIONS = frozenset({
    "images_ocr_and_embedded_drawings_not_extracted", "embedded_object_alt_text_not_extracted",
    "equations_not_rendered", "automatic_page_numbers_not_rendered", "list_numbering_and_layout_not_rendered",
    "text_style_and_hyperlink_targets_not_extracted", "suggestion_style_changes_not_interpreted",
    "views_are_alternative_representations_not_independent_evidence", "source_text_requires_consumer_sanitization",
})
_KINDS = {"text", "rich_link", "person", "date", "footnote_reference", "unsupported_equation",
          "unsupported_auto_text", "unsupported_inline_object"}
_REGIONS = ("body", "header", "footer", "footnote")


def _invalid(code="document_docs_response_invalid"):
    raise ConnectorError(code)


def _id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) is not None


def _size(text):
    try:
        return len(text.encode("utf-8"))
    except (AttributeError, UnicodeError):
        _invalid()


def _path_key(path):
    """Validate the emitted structural path and return its numeric order key."""
    if not isinstance(path, list) or not 4 <= len(path) <= 100:
        _invalid()
    index, depth, key = 0, 0, []

    def number(at):
        if (at >= len(path) or not isinstance(path[at], str)
                or not re.fullmatch(r"0|[1-9][0-9]{0,5}", path[at]) or int(path[at]) >= 200000):
            _invalid()
        return int(path[at])

    while index < len(path):
        if path[index] != "content":
            _invalid()
        key.append((0, number(index + 1)))
        index += 2
        if index >= len(path):
            _invalid()
        if path[index] == "elements":
            key.append((1, number(index + 1)))
            if index + 2 != len(path):
                _invalid()
            return tuple(key)
        if path[index] == "tableRows":
            key.append((2, number(index + 1)))
            if index + 2 >= len(path) or path[index + 2] != "tableCells":
                _invalid()
            key.append((3, number(index + 3)))
            index += 4
        elif path[index] == "tableOfContents":
            key.append((4, 0))
            index += 1
        else:
            _invalid()
        depth += 1
        if depth > 16:
            _invalid("document_docs_content_depth_exceeded")
    _invalid()


def _tabs(tabs, count):
    if not isinstance(tabs, list) or type(count) is not int or count != len(tabs) or not 1 <= count <= 1024:
        _invalid("document_docs_tab_inventory_invalid")
    known, stack, sibling_counts = {}, [], {}
    for position, tab in enumerate(tabs):
        if (not isinstance(tab, dict) or set(tab) != {"tabId", "title", "parentTabId", "index", "nestingLevel"}
                or not _id(tab["tabId"]) or tab["tabId"] in known or not isinstance(tab["title"], str)
                or type(tab["nestingLevel"]) is not int or not 0 <= tab["nestingLevel"] <= 8
                or type(tab["index"]) is not int):
            _invalid("document_docs_tab_inventory_invalid")
        depth = tab["nestingLevel"]
        if depth > len(stack):
            _invalid("document_docs_tab_inventory_invalid")
        stack = stack[:depth]
        parent = stack[-1] if stack else ""
        if tab["parentTabId"] != parent or tab["index"] != sibling_counts.get(parent, 0):
            _invalid("document_docs_tab_inventory_invalid")
        sibling_counts[parent] = tab["index"] + 1
        known[tab["tabId"]] = position
        stack.append(tab["tabId"])
    return known


def _record(record, tab_order):
    base = {"tabId", "region", "regionId", "path", "kind", "text", "startIndex", "endIndex",
            "suggestedInsertionIds", "suggestedDeletionIds"}
    if not isinstance(record, dict) or not base <= set(record):
        _invalid()
    kind, region = record["kind"], record["region"]
    if (not isinstance(kind, str) or kind not in _KINDS or not isinstance(region, str) or region not in _REGIONS
            or not isinstance(record["tabId"], str) or record["tabId"] not in tab_order
            or not isinstance(record["text"], str)
            or (record["regionId"] != "" if region == "body" else not _id(record["regionId"]))
            or any(type(record[key]) is not int or not 0 <= record[key] <= 2**63 - 1 for key in ("startIndex", "endIndex"))
            or record["endIndex"] < record["startIndex"]):
        _invalid()
    extra = set(record) - base
    if kind == "footnote_reference":
        if (extra != {"footnoteId", "footnoteNumber"} or not _id(record["footnoteId"])
                or not isinstance(record["footnoteNumber"], str) or record["text"] != record["footnoteNumber"]):
            _invalid()
    elif kind == "person":
        if extra - {"displayFallback"} or (extra and record["displayFallback"] != "email"):
            _invalid()
    elif extra:
        _invalid()
    if (kind.startswith("unsupported_") and record["text"]) or (kind == "text" and not record["text"]):
        _invalid()
    for name in ("suggestedInsertionIds", "suggestedDeletionIds"):
        ids = record[name]
        if not isinstance(ids, list) or any(not _id(item) for item in ids) or ids != sorted(set(ids)):
            _invalid()
    return (tab_order[record["tabId"]], _REGIONS.index(region), record["regionId"], _path_key(record["path"]))


def _render(records, tabs, *, inline):
    """Keep adjacent text runs together; retain full positions in metadata."""
    if not any(record["text"] for record in records):
        return ""
    titles = {tab["tabId"]: tab["title"] for tab in tabs}
    paragraphs, current, text = [], None, []
    for record in records:
        unsupported = record["kind"].startswith("unsupported_")
        if not record["text"] and not unsupported:
            continue
        paragraph = (record["tabId"], record["region"], record["regionId"], tuple(record["path"][:-2]))
        if paragraph != current:
            if current is not None:
                paragraphs.append((current, "".join(text)))
            current, text = paragraph, []
        value = "[not extracted: " + record["kind"] + "]" if unsupported else record["text"]
        if inline and (record["suggestedInsertionIds"] or record["suggestedDeletionIds"]):
            tags = []
            if record["suggestedInsertionIds"]:
                tags.append("insertion=" + ",".join(record["suggestedInsertionIds"]))
            if record["suggestedDeletionIds"]:
                tags.append("deletion=" + ",".join(record["suggestedDeletionIds"]))
            value = "[suggestion " + " ".join(tags) + "]" + value + "[/suggestion]"
        text.append(value)
    if current is not None:
        paragraphs.append((current, "".join(text)))
    result, region = [], None
    for location, text in paragraphs:
        if location[:3] != region:
            region = location[:3]
            result.append("[tab " + region[0] + ": " + titles[region[0]] + "; " + region[1] +
                          (" " + region[2] if region[2] else "") + "]")
        if "tableRows" in location[3] or "tableOfContents" in location[3]:
            result.append("[structure " + "/".join(location[3]) + "]")
        result.append(text)
    rendered = "\n".join(result)
    if inline:
        rendered = "[SUGGESTIONS_INLINE: alternative view with unaccepted proposals; not independent evidence]\n" + rendered
    return rendered


def parse_document_views(document_id, payload):
    keys = {"schemaVersion", "documentId", "title", "revisionId", "tabCount", "tabs", "views", "textBytes",
            "revisionAndTabsRechecked", "tabHierarchyRechecked", "partialText", "limitations"}
    if not isinstance(payload, dict) or set(payload) != keys:
        _invalid()
    revision = payload["revisionId"]
    if (type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1
            or payload["documentId"] != document_id or not isinstance(payload["title"], str)
            or not isinstance(revision, str) or (revision and not revision.strip())
            or payload["revisionAndTabsRechecked"] is not bool(revision)
            or payload["tabHierarchyRechecked"] is not True or payload["partialText"] is not True
            or type(payload["textBytes"]) is not int or not 0 <= payload["textBytes"] <= _DOCUMENT_BYTES):
        _invalid()
    limitations = payload["limitations"]
    expected = BASE_LIMITATIONS | ({"revision_id_unavailable"} if not revision else set())
    if (not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations)
            or len(limitations) != len(set(limitations)) or set(limitations) != expected):
        _invalid()
    tab_order = _tabs(payload["tabs"], payload["tabCount"])
    total = _size(payload["title"]) + sum(_size(tab["title"]) for tab in payload["tabs"])
    views = payload["views"]
    if not isinstance(views, list) or len(views) != 2:
        _invalid()
    records_count, documents = 0, []
    for mode, view in zip(VIEW_MODES, views):
        if (not isinstance(view, dict) or set(view) != {"mode", "records", "textBytes"} or view["mode"] != mode
                or not isinstance(view["records"], list) or type(view["textBytes"]) is not int
                or not 0 <= view["textBytes"] <= _DOCUMENT_BYTES):
            _invalid()
        previous, byte_count = None, 0
        for record in view["records"]:
            records_count += 1
            if records_count > 200000:
                _invalid("document_docs_node_budget_exceeded")
            position = _record(record, tab_order)
            if previous is not None and position <= previous:
                _invalid()
            previous = position
            byte_count += _size(record["text"])
        if byte_count != view["textBytes"]:
            _invalid()
        total += byte_count
        inline = mode == VIEW_MODES[1]
        documents.append({"extracted_text": _render(view["records"], payload["tabs"], inline=inline),
            "revisionId": revision, "title": payload["title"], "extraction": {
                "format": "google_docs_structured_text", "text_scope": "all_tabs_body_headers_footers_footnotes",
                "partial_text": True, "limitations": sorted(limitations), "tab_ids": list(tab_order),
                "tab_hierarchy": payload["tabs"], "view_mode": mode,
                "view_role": "suggestions_inline_context" if inline else "base_without_suggestions",
                "alternative_view_not_independent_evidence": True, "view_records": view["records"],
                "index_units": "google_docs_utf16_code_units", "indices_are_view_specific": True,
                "indices_are_source_body_spans": False,
                "view_text_bytes": byte_count, "docs_revision_and_tabs_rechecked": bool(revision),
                "tab_hierarchy_rechecked": True, "revision_id_available": bool(revision), "structure_schema_version": 1}})
    if total != payload["textBytes"]:
        _invalid()
    if sum(_size(doc["extracted_text"]) for doc in documents) > _DOCUMENT_BYTES:
        _invalid("document_text_budget_exceeded")
    documents[0]["context_views"] = [documents[1]]
    return documents[0]
