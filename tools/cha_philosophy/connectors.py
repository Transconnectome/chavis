"""Read-only source adapters. No store, credentials discovery, or model calls.

Iterators expose partial progress and never advance a persistent checkpoint.
The caller commits checkpoints only after successfully persisting yielded rows.
"""
from __future__ import annotations

import base64
import email
import email.header
import email.policy
import email.utils
import hashlib
import json
import os
import posixpath
import re
import resource
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterator
import zipfile
from contextlib import nullcontext


class ConnectorError(RuntimeError):
    """Safe error text; never include HTTP bodies, subprocess stderr, or tokens."""

    def __init__(self, code: str, *, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


@dataclass
class SyncProgress:
    pages: int = 0
    items: int = 0
    complete: bool = False
    remaining_page_token: str | None = field(default=None, repr=False)
    pending_ids: list[str] = field(default_factory=list)
    failure: str | None = None
    limitations: list[str] = field(default_factory=list)


READABLE_DRIVE_MIME_TYPES = frozenset({
    "application/vnd.google-apps.document", "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.spreadsheet", "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "text/plain", "text/markdown", "text/csv", "text/tab-separated-values",
})
# Local resource budgets, not claims about any provider quota. Exceeding a
# budget fails the read explicitly; it never turns a truncated body into a source.
_DOCUMENT_BYTES = 32 * 1024 * 1024
_DOCUMENT_UNPACKED_BYTES = 64 * 1024 * 1024
_OFFICE_DOWNLOAD_BYTES = 128 * 1024 * 1024
_OFFICE_MIME_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.presentation",
})
_SLIDES_TEXT_LIMITATIONS = frozenset({
    "images_and_ocr_not_extracted", "charts_and_embedded_media_not_extracted",
    "layouts_masters_and_notes_master_not_extracted",
    "rendered_reading_order_and_table_geometry_not_verified",
    "text_style_and_hyperlink_targets_not_extracted", "source_text_requires_consumer_sanitization",
})
_SLIDES_TEXT_ERRORS = {
    "auth_required": "gog_auth_required", "auth_unavailable": "gog_auth_required",
    "access_denied": "source_access_denied", "not_found": "source_not_found",
    "rate_limited": "gog_rate_limited", "quota_exceeded": "gog_quota_exceeded",
    "read_failed": "gog_read_failed", "output_failed": "gog_read_failed",
    "identity_or_revision_missing": "document_slide_response_invalid",
    "inventory_invalid": "document_slide_inventory_invalid",
    "response_invalid": "document_slide_response_invalid",
    "changed_during_read": "document_changed_during_read",
    "byte_budget_exceeded": "document_response_budget_exceeded",
    "node_budget_exceeded": "document_slide_node_budget_exceeded",
    "group_depth_exceeded": "document_slide_group_depth_exceeded",
    "slide_budget_exceeded": "document_slide_count_budget_exceeded",
}
_DOCS_TEXT_ERRORS = {
    "auth_required": "gog_auth_required", "auth_unavailable": "gog_auth_required",
    "access_denied": "source_access_denied", "not_found": "source_not_found",
    "rate_limited": "gog_rate_limited", "quota_exceeded": "gog_quota_exceeded",
    "read_failed": "gog_read_failed", "output_failed": "gog_read_failed",
    "preview_access_denied": "document_docs_preview_access_denied",
    "inline_access_denied": "document_docs_inline_access_denied",
    "verification_access_denied": "document_docs_verification_access_denied",
    "identity_or_view_invalid": "document_docs_response_invalid",
    "tab_inventory_invalid": "document_docs_tab_inventory_invalid",
    "response_invalid": "document_docs_response_invalid",
    "changed_during_read": "document_changed_during_read",
    "byte_budget_exceeded": "document_response_budget_exceeded",
    "node_budget_exceeded": "document_docs_node_budget_exceeded",
    "tab_depth_exceeded": "document_docs_tab_depth_exceeded",
    "content_depth_exceeded": "document_docs_content_depth_exceeded",
    "tab_budget_exceeded": "document_docs_tab_budget_exceeded",
}


def _file_size_limit(budget):
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    limit = min(budget, hard) if hard != resource.RLIM_INFINITY else budget
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard))


def _document_file_limit():
    _file_size_limit(_DOCUMENT_BYTES)


def _office_download_file_limit():
    _file_size_limit(_OFFICE_DOWNLOAD_BYTES)


def _extracted_document(text: str, format: str, scope: str, **metadata) -> dict:
    if len(text.encode("utf-8")) > _DOCUMENT_BYTES:
        raise ConnectorError("document_text_budget_exceeded")
    return {"extracted_text": text, "extraction": {"format": format, "text_scope": scope, **metadata}}


def _office_text(path: Path, *, presentation: bool) -> dict:
    """Read Office XML without extracting files or following external relations."""
    try:
        from defusedxml import ElementTree as ET
    except ImportError:
        raise ConnectorError("document_xml_parser_unavailable") from None
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 4096:
                raise ConnectorError("document_archive_budget_exceeded")
            # Some producers write Windows separators into ZIP member names.
            # Resolve those names in memory only; never extract archive paths.
            # Reject aliases before mapping so two parts cannot impersonate one.
            names = [x.filename.replace("\\", "/") for x in entries]
            if len(names) != len(set(names)):
                raise ConnectorError("document_archive_duplicate_parts")
            members = dict(zip(names, entries))
            archive_metadata = ({"archive_path_separators_normalized": True}
                                if any("\\" in x.filename for x in entries) else {})
            xml_bytes = 0
            def xml(name):
                nonlocal xml_bytes
                # Inflate only requested XML/relationship parts. Image/media
                # entries are never opened and do not consume the XML budget.
                # Enforce the cumulative limit on every read, including repeated
                # references and a relationship to a non-.xml member.
                info = members[name]
                remaining = _DOCUMENT_UNPACKED_BYTES - xml_bytes
                if info.file_size < 0 or info.file_size > remaining:
                    raise ConnectorError("document_archive_budget_exceeded")
                with archive.open(info) as member:
                    data = member.read(remaining + 1)
                xml_bytes += len(data)
                if xml_bytes > _DOCUMENT_UNPACKED_BYTES:
                    raise ConnectorError("document_archive_budget_exceeded")
                return ET.fromstring(data)
            def paragraph_text(root, namespace):
                parts = []
                for paragraph in root.iter("{" + namespace + "}p"):
                    parts.append("".join((node.text or "") if node.tag == "{" + namespace + "}t" else "\t" if node.tag.endswith("}tab") else "\n" if node.tag.endswith("}br") else "" for node in paragraph.iter()))
                return "\n".join(parts)
            if not presentation:
                selected = ["word/document.xml"] + sorted(n for n in names if re.fullmatch(r"word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml", n))
                namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                text = "\n\n".join("[" + n + "]\n" + paragraph_text(xml(n), namespace) for n in selected)
                return _extracted_document(text, "docx", "body_tables_headers_footers_footnotes_endnotes_embedded_comments", part_ids=selected,
                                           limitations=["images_and_embedded_files_not_extracted", "document_layout_not_preserved", "tracked_changes_not_adjudicated"], **archive_metadata)
            relationship_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
            office_rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            presentation_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
            drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
            def relations(source):
                folder, name = posixpath.split(source)
                relpath = folder + "/_rels/" + name + ".rels"
                if relpath not in names:
                    return {}
                return {r.attrib["Id"]: (posixpath.normpath(posixpath.join(folder, r.attrib["Target"])), r.attrib.get("Type", ""))
                        for r in xml(relpath).findall("{" + relationship_ns + "}Relationship")
                        if r.attrib.get("TargetMode") != "External" and "Id" in r.attrib and "Target" in r.attrib}
            rels = relations("ppt/presentation.xml")
            slides = []
            parts = []
            for slide in xml("ppt/presentation.xml").iter("{" + presentation_ns + "}sldId"):
                rid = slide.attrib.get("{" + office_rel_ns + "}id")
                if rid not in rels:
                    raise ConnectorError("document_slide_relationship_missing")
                target, _ = rels[rid]
                if not target.startswith("ppt/slides/"):
                    raise ConnectorError("document_slide_relationship_invalid")
                slides.append(target)
                parts.append("[slide " + str(len(slides)) + "]\n" + paragraph_text(xml(target), drawing_ns))
                for note, kind in relations(target).values():
                    if kind.endswith("/notesSlide"):
                        if not note.startswith("ppt/notesSlides/"):
                            raise ConnectorError("document_notes_relationship_invalid")
                        parts.append("[speaker notes]\n" + paragraph_text(xml(note), drawing_ns))
            return _extracted_document("\n\n".join(parts), "pptx", "all_slides_shape_group_table_text_and_notes", part_ids=slides, slide_count=len(slides),
                                       limitations=["images_and_embedded_files_not_extracted", "master_layout_text_not_extracted", "presentation_layout_not_preserved"], **archive_metadata)
    except ConnectorError:
        raise
    except Exception:
        # XML/archive exceptions may quote document content or embedded paths.
        raise ConnectorError("document_office_parse_failed") from None


def source_key(*parts: object) -> str:
    return "/".join(urllib.parse.quote(str(p), safe="") for p in parts)


def _required_id(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("-") or "\x00" in value:
        raise ConnectorError("invalid_identifier")
    return value


def _iso_millis(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return ""


class _HTMLText(HTMLParser):
    def __init__(self, omit_quotes: bool = False):
        super().__init__(convert_charrefs=True)
        self.omit_quotes = omit_quotes
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.quotes_found = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        quote = tag == "blockquote" or any(
            name in (attrs.get("class") or "").split()
            for name in ("gmail_quote", "gmail_signature", "yahoo_quoted")
        )
        self.quotes_found |= quote
        hidden = tag in ("script", "style") or (self.omit_quotes and quote)
        inherited = self.stack[-1][1] if self.stack else False
        if tag in ("p", "div", "br", "li", "tr", "blockquote"):
            if not inherited:
                self.parts.append("\n")
        if tag not in ("br", "img", "hr", "meta", "link", "input", "wbr", "source"):
            self.stack.append((tag, inherited or hidden))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break
        if tag in ("p", "div", "li", "tr", "blockquote"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)

    def text(self):
        return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", "".join(self.parts)).strip()


def _html_text(value: str, omit_quotes=False) -> tuple[str, bool]:
    parser = _HTMLText(omit_quotes)
    parser.feed(value)
    parser.close()
    return parser.text(), parser.quotes_found


_TAIL_QUOTE = re.compile(
    r"^(?:On\s.+wrote:|[-_]{2,}\s*(?:Original Message|Forwarded message|원본 메시지|전달된 메시지)"
    r".*|.+(?:님이 작성|님이 쓴 글|작성함):?)$", re.I
)


def authored_text(value: str, *, is_html=False) -> tuple[str, dict]:
    """Conservative extraction: ambiguous unprefixed quote tails stay in body only."""
    text, html_quotes = _html_text(value, True) if is_html else (value, False)
    output = []
    uncertain = False
    excluded = 0
    in_tail = False
    for line in text.splitlines():
        stripped = line.strip()
        if _TAIL_QUOTE.match(stripped):
            in_tail = True
            uncertain = True
            excluded += 1
        elif in_tail or stripped.startswith(">"):
            excluded += 1
        elif stripped in ("--", "-- "):
            in_tail = True
            excluded += 1
        else:
            output.append(line)
    return "\n".join(output).strip(), {
        "quote_parse_uncertain": uncertain,
        "excluded_quote_lines": excluded,
        "html_quotes_detected": html_quotes,
        "parser_version": "conservative-quotes-v1",
    }


def _decode64(value: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ConnectorError("invalid_base64_body") from exc


def _headers(message: dict) -> dict[str, str]:
    payload = message.get("payload") or {}
    result: dict[str, str] = {}
    needed = {"from", "sender", "to", "cc", "bcc", "subject", "date", "message-id", "in-reply-to", "references", "reply-to"}
    for h in payload.get("headers", message.get("headers", [])):
        name = str(h.get("name", "")).lower()
        if name in needed:
            result[name] = str(email.header.make_header(email.header.decode_header(str(h.get("value", "")))))
    for name in ("from", "to", "cc", "bcc", "sender", "subject", "date"):
        if name not in result and isinstance(message.get(name), str):
            result[name] = message[name]
    return result


def _addresses(value: str) -> set[str]:
    return {addr.strip().lower() for _, addr in email.utils.getaddresses([value]) if addr}


def _decode_charset(data: bytes, charset: str, decoding_meta: dict, part_path: str) -> str:
    """Honor the declaration first; the sole fallback must decode all UTF-8 bytes."""
    try:
        return data.decode(charset, errors="strict")
    except (LookupError, UnicodeError):
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeError:
            raise ConnectorError("undecodable_message_charset") from None
        decoding_meta.setdefault("charset_discrepancies", []).append({
            "declared_charset": charset, "decoded_charset": "utf-8", "part_path": part_path,
            "basis": "declared_decode_failed_strict_utf8_succeeded",
        })
        return text


def _mime_text(part: dict, decoding_meta: dict | None = None, part_path="payload") -> tuple[str, bool]:
    decoding_meta = decoding_meta if decoding_meta is not None else {}
    if not isinstance(part, dict):
        raise ConnectorError("malformed_mime_part")
    mime = str(part.get("mimeType", "")).lower()
    # Do not inspect a non-text attachment's children, transfer encoding, or body.
    if part.get("filename") or not (mime in ("text/plain", "text/html") or mime.startswith("multipart/")):
        decoding_meta["excluded_nonbody_mime_parts"] = decoding_meta.get("excluded_nonbody_mime_parts", 0) + 1
        return "", False  # Attachments are independent evidence, never authored mail text.
    disposition = next((h.get("value", "") for h in part.get("headers", []) if h.get("name", "").lower() == "content-disposition"), "")
    if disposition.lower().split(";", 1)[0].strip() == "attachment":
        decoding_meta["excluded_nonbody_mime_parts"] = decoding_meta.get("excluded_nonbody_mime_parts", 0) + 1
        return "", False
    children = part.get("parts") or []
    if children:
        if not isinstance(children, list):
            raise ConnectorError("malformed_mime_children")
        parsed = [_mime_text(p, decoding_meta, f"{part_path}.parts[{i}]") for i, p in enumerate(children)]
        if mime == "multipart/alternative":
            # Prefer HTML when available: structural quotation is more reliable.
            return next((p for p in parsed if p[0] and p[1]), next((p for p in parsed if p[0]), ("", False)))
        if any(html for text, html in parsed if text):
            # Preserve all mixed parts; plain parts are escaped before HTML parsing.
            import html
            return "\n".join(text if is_html else "<pre>" + html.escape(text) + "</pre>" for text, is_html in parsed if text), True
        return "\n".join(text for text, _ in parsed if text), False
    data = (part.get("body") or {}).get("data")
    if data is None:
        if mime in ("text/plain", "text/html") and (part.get("body") or {}).get("attachmentId"):
            raise ConnectorError("text_body_requires_attachment_fetch")
        return "", False
    if mime not in ("text/plain", "text/html"):
        return "", False
    content_type = next((h.get("value", "") for h in part.get("headers", []) if h.get("name", "").lower() == "content-type"), "")
    charset_match = re.search(r'charset=["\']?([^;\s"\']+)', content_type, re.I)
    charset = charset_match.group(1) if charset_match else "utf-8"
    return _decode_charset(_decode64(data), charset, decoding_meta, part_path), mime == "text/html"


def _message_text(message: dict, decoding_meta: dict | None = None) -> tuple[str, bool, dict]:
    decoding_meta = decoding_meta if decoding_meta is not None else {}
    headers = _headers(message)
    if message.get("raw"):
        parsed = email.message_from_bytes(_decode64(message["raw"]), policy=email.policy.default)
        headers = _headers({"headers": [{"name": k, "value": str(v)} for k, v in parsed.items()]})
        body = parsed.get_body(preferencelist=("html", "plain"))
        text = _decode_charset(body.get_payload(decode=True) or b"", body.get_content_charset() or "utf-8", decoding_meta, "raw.body") if body else ""
        return text, bool(body and body.get_content_type() == "text/html"), headers
    if "payload" in message:
        text, is_html = _mime_text(message["payload"], decoding_meta)
        return text, is_html, headers
    if isinstance(message.get("body"), str):
        return message["body"], message.get("mimeType") == "text/html", headers
    raise ConnectorError("message_full_body_missing")


def validate_lab_project_routes(value=None) -> list[str]:
    """Accept curated bare addresses only, never groups expanded into members.

    A conservative ASCII address form keeps Gmail query operators, display names,
    multiple addresses and ambiguous quoted local parts out of configuration.
    First occurrence order is retained; existing recipient ordering is untouched.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConnectorError("gmail_lab_project_routes_invalid")
    result = []
    seen = set()
    for address in value:
        if (not isinstance(address, str) or len(address) > 254
                or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", address)):
            raise ConnectorError("gmail_lab_project_routes_invalid")
        local, domain = address.split("@")
        labels = domain.split(".")
        if (len(local) > 64 or local.startswith(".") or local.endswith(".") or ".." in local
                or len(labels) < 2 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                          for label in labels)):
            raise ConnectorError("gmail_lab_project_routes_invalid")
        address = address.lower()
        if address not in seen:
            result.append(address)
            seen.add(address)
    return result


def normalize_gmail(raw_thread: dict, identities: set[str], lab_recipients: set[str], *,
                    lab_project_routes=None) -> list[dict]:
    identities = {x.lower() for x in identities}
    recipients = {x.lower() for x in lab_recipients}
    routes = set(validate_lab_project_routes(lab_project_routes))
    thread = raw_thread.get("thread", raw_thread)
    account = str(raw_thread.get("_account_id") or raw_thread.get("account_id") or "unknown-account")
    tid = _required_id(thread.get("id"))
    prepared = []
    for msg in thread.get("messages", []):
        decoding_meta: dict = {}
        text, html, headers = _message_text(msg, decoding_meta)
        senders = _addresses(headers.get("from", ""))
        addressed = set().union(*(_addresses(headers.get(k, "")) for k in ("to", "cc", "bcc")))
        labels = set(msg.get("labelIds", []))
        direct = len(senders) == 1 and bool(senders & identities) and "SENT" in labels and bool(addressed & (recipients | routes))
        prepared.append((msg, text, html, headers, senders, addressed, labels, decoding_meta, direct))
    if not any(p[-1] for p in prepared):
        return []
    rows = []
    for msg, text, html, headers, senders, addressed, labels, decoding_meta, direct in prepared:
        mid = _required_id(msg.get("id"))
        full = _html_text(text)[0] if html else text
        own, quote_meta = authored_text(text, is_html=html)
        status = "deleted" if msg.get("deleted") else "active"
        if "TRASH" in labels or "SPAM" in labels:
            status = "unavailable"
        rows.append({
            "source_id": source_key("gmail", account, tid, "", mid), "platform": "gmail",
            "native_id": mid, "author_id": next(iter(senders)) if len(senders) == 1 else "",
            "author_name": email.utils.parseaddr(headers.get("from", ""))[0],
            "authorship": "direct" if direct else ("context" if senders else "unverified"),
            "title": headers.get("subject", ""), "body": full if status == "active" else "",
            "authored_text": own if direct and status == "active" else "",
            "created_at": _iso_millis(msg.get("internalDate")) or headers.get("date", ""),
            "modified_at": "", "url": "https://mail.google.com/mail/u/?authuser=" + urllib.parse.quote(account, safe="") + "#all/" + urllib.parse.quote(tid, safe=""),
            "scope": "connectome_lab_correspondence", "status": status,
            "metadata": {"account_id": account, "thread_id": tid, "parent_id": "",
                         "authorship_basis": ("sent_verified_from_exact_lab_recipient" if addressed & recipients else
                                              "sent_verified_from_exact_lab_project_route") if direct else "thread_context",
                         "lab_recipients": sorted(addressed & recipients), "headers": headers,
                         **({"lab_project_routes": sorted(addressed & routes)} if routes else {}),
                         "label_ids": sorted(labels), "native_revision": msg.get("historyId", ""),
                         "body_sha256": hashlib.sha256(full.encode()).hexdigest(), **quote_meta, **decoding_meta},
        })
    return rows


def normalize_teams(raw: dict, tenant_id: str, container_id: str, professor_ids: set[str], parent_id: str = "") -> dict:
    mid = _required_id(raw.get("id"))
    parent_id = parent_id or raw.get("replyToId") or raw.get("_parent_id") or ""
    sender = raw.get("from") or {}
    user = sender.get("user") or {}
    author_id = str(user.get("id") or "")
    direct = author_id in professor_ids and bool(author_id) and not sender.get("application")
    content = raw.get("body") or {}
    is_html = content.get("contentType") == "html"
    text = str(content.get("content") or "")
    full = _html_text(text)[0] if is_html else text
    own, quote_meta = authored_text(text, is_html=is_html)
    deleted = bool(raw.get("deletedDateTime") or raw.get("@removed"))
    system = raw.get("messageType") not in (None, "message")
    if system:
        direct = False
    attachments = []
    for attachment in raw.get("attachments", []):
        safe = {key: attachment[key] for key in ("id", "contentType", "name") if key in attachment}
        if attachment.get("contentUrl"):
            parsed_url = urllib.parse.urlsplit(attachment["contentUrl"])
            if parsed_url.scheme == "https" and not parsed_url.username and not parsed_url.password:
                # Signed query strings and adaptive-card payloads can contain secrets.
                safe["contentUrl"] = urllib.parse.urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", ""))
        attachments.append(safe)
    return {
        "source_id": source_key("teams", tenant_id, container_id, parent_id, mid), "platform": "teams",
        "native_id": mid, "author_id": author_id, "author_name": user.get("displayName", ""),
        "authorship": "direct" if direct else ("context" if author_id else "unverified"),
        "title": raw.get("subject") or "", "body": "" if deleted else full,
        "authored_text": own if direct and not deleted else "", "created_at": raw.get("createdDateTime") or "",
        "modified_at": raw.get("lastModifiedDateTime") or "", "url": raw.get("webUrl") or "",
        "scope": "teams:" + container_id, "status": "deleted" if deleted else "active",
        "metadata": {"tenant_id": tenant_id, "container_id": container_id, "parent_id": parent_id,
                     "authorship_basis": "verified_graph_user_id" if direct else "system_event" if system else "graph_context_or_unknown",
                     "native_revision": raw.get("etag") or "", "last_edited_at": raw.get("lastEditedDateTime"),
                     "deleted_at": raw.get("deletedDateTime"), "body_sha256": hashlib.sha256(full.encode()).hexdigest(),
                     "mentions": raw.get("mentions", []), "attachments": attachments, **quote_meta},
    }


def normalize_teams_native(raw: dict, tenant_id: str, professor_ids: set[str]) -> dict:
    """Normalize a native Teams *message list* row, never a search summary.

    Native list responses omit edit timestamps and HTML quotation structure. The
    resulting evidence explicitly carries those limits. Display names never
    establish direct authorship, and a chat transcript is not a message row.
    """
    if not isinstance(raw, dict) or "message_id" not in raw or "content" not in raw:
        raise ConnectorError("teams_native_message_required")
    container = raw.get("channel_id") or raw.get("chat_id")
    container = _required_id(container)
    sender = {"user": {"id": raw.get("author_user_id") or "", "displayName": raw.get("author_name") or ""}}
    if raw.get("author_application_id"):
        sender["application"] = {"id": raw["author_application_id"]}
    row = normalize_teams({
        "id": raw["message_id"], "from": sender,
        "body": {"contentType": "text", "content": raw["content"] or ""},
        "createdDateTime": raw.get("created_at"), "deletedDateTime": raw.get("deleted_at"),
        "lastModifiedDateTime": raw.get("modified_at"),
        "messageType": raw.get("message_type") or "message", "subject": raw.get("title") or "",
        "replyToId": raw.get("parent_message_id") or "", "webUrl": raw.get("web_link") or "",
        "mentions": raw.get("mentions") or [],
    }, tenant_id, container, professor_ids)
    row["metadata"].update({
        "source_adapter": "native_teams_message_list", "native_path": raw.get("path") or "",
        "team_id": raw.get("team_id") or "", "container_type": raw.get("container_type") or ("channel" if raw.get("channel_id") else "chat"),
        "has_attachments": bool(raw.get("has_attachments")),
        "text_scope": "connector_plaintext_message",
        "limitations": ["native_plaintext_quote_structure_unavailable", "native_edit_timestamp_unavailable", "attachments_not_fetched"],
    })
    return row


_SECRET_REDACTION = "[REDACTED_SECRET]"
_MEETING_SECRET_ENGLISH = r"(?:pass[ _-]?code|(?:meeting|zoom|webex|teams)[ _-]*(?:pass[ _-]?code|password))"
_MEETING_SECRET_KOREAN = r"(?:패스[ _-]?코드|(?:회의|미팅|줌|접속|참가)[ _-]*(?:비밀번호|비번|암호|패스[ _-]?코드))"
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    re.compile(r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{15,}|ya29\.[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\bBearer[ \t]+(?!\[REDACTED_SECRET\])[A-Za-z0-9._~+/=-]{12,}"),
    # One-tap dial suffix: keep the telephone/meeting ID and DTMF delimiters,
    # but remove the access code after comma/semicolon pauses and '*'.
    re.compile(r"(?P<dial_prefix>#[,;]+\*)(?P<dial_code>\d+)(?P<dial_suffix>#)"),
    re.compile(r"(?im)(?P<label>\b(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|client[ _-]?secret|password|passwd|pwd|token|secret|" + _MEETING_SECRET_ENGLISH + r")|비밀번호|비번|암호|" + _MEETING_SECRET_KOREAN + r")(?P<sep>[\"']?[^\S\r\n]*(?:=|:|：)[^\S\r\n]*)(?P<value>(?![^\S\r\n]*\[REDACTED_SECRET\])(?:\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s<>&;,]+))"),
)
_SECRET_FIELD = re.compile(r"^(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|client[ _-]?secret|private[ _-]?key|authorization|password|passwd|pwd|token|secret|비밀번호|비번|암호|" + _MEETING_SECRET_ENGLISH + "|" + _MEETING_SECRET_KOREAN + r")$", re.I)


def sanitize_source(row: dict) -> dict:
    """Redact recognizable credential spans in a copy before persistence/model use.

    This is a bounded heuristic, not a guarantee that prose contains no secrets.
    Counts are replacements across stored fields (body/authored_text may repeat).
    Service originals remain untouched; quotations thereafter address sanitized
    text. No original credential or reversibility mapping is retained.
    """
    count = 0

    def clean(value):
        nonlocal count
        if isinstance(value, str):
            for pattern in _SECRET_PATTERNS:
                def replace(match):
                    nonlocal count
                    count += 1
                    if "dial_prefix" in match.groupdict():
                        return match["dial_prefix"] + _SECRET_REDACTION + match["dial_suffix"]
                    if "label" in match.groupdict():
                        return match["label"] + match["sep"] + _SECRET_REDACTION
                    return _SECRET_REDACTION
                value = pattern.sub(replace, value)
            return value
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if isinstance(key, str) and _SECRET_FIELD.fullmatch(key) and isinstance(item, (str, int, float)) and item not in ("", _SECRET_REDACTION):
                    count += 1
                    result[key] = _SECRET_REDACTION
                else:
                    result[key] = clean(item)
            return result
        return value

    result = clean(row)
    metadata = result.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ConnectorError("source_metadata_invalid")
    if count:
        metadata["secret_redaction_count"] = int(metadata.get("secret_redaction_count", 0)) + count
        metadata["text_transform"] = "credential_redaction_v1"
        metadata["body_sha256"] = hashlib.sha256(str(result.get("body") or "").encode()).hexdigest()
    return result


def normalize_drive_comment(file: dict, comment: dict, account_is_professor: bool) -> list[dict]:
    fid = _required_id(file.get("id"))
    cid = _required_id(comment.get("id"))
    account = str(file.get("_account_id") or file.get("account_id") or "unknown-account")
    rows = []
    for obj, parent in [(comment, ""), *((reply, cid) for reply in comment.get("replies", []))]:
        oid = _required_id(obj.get("id"))
        author = obj.get("author") or {}
        direct = account_is_professor and author.get("me") is True
        deleted = bool(comment.get("deleted") or obj.get("deleted"))
        unavailable = deleted or bool(file.get("trashed"))
        text = str(obj.get("content") or "")
        if not text and obj.get("htmlContent"):
            text = _html_text(obj["htmlContent"])[0]
        own, quote_meta = authored_text(text)
        rows.append({
            "source_id": source_key("drive", account, fid, parent, oid), "platform": "drive",
            "native_id": oid, "author_id": account if direct else "", "author_name": author.get("displayName", ""),
            "authorship": "direct" if direct else "unverified", "title": file.get("name", ""),
            "body": "" if unavailable else text, "authored_text": own if direct and not unavailable else "",
            "created_at": obj.get("createdTime") or "", "modified_at": obj.get("modifiedTime") or "",
            "url": (file.get("webViewLink") or "https://drive.google.com/open?id=" + urllib.parse.quote(fid, safe="")),
            "scope": "drive:" + fid, "status": "deleted" if deleted else "unavailable" if file.get("trashed") else "active",
            "metadata": {"account_id": account, "file_id": fid, "comment_id": cid, "parent_id": parent,
                         "source_kind": "reply" if parent else "comment", "authorship_basis": "verified_requesting_account_me" if direct else "display_name_insufficient",
                         "anchor": comment.get("anchor"), "quoted_file_content": comment.get("quotedFileContent"),
                         "resolved": comment.get("resolved"), "action": obj.get("action"), **quote_meta},
        })
    return rows


def normalize_drive_document(file: dict, document: dict) -> dict:
    """A shared document's ownership/last editor never establishes authorship."""
    fid = _required_id(file.get("id"))
    account = str(file.get("_account_id") or file.get("account_id") or "unknown-account")
    segments: list[str] = []
    tab_ids: list[str] = []

    def elements(value):
        if isinstance(value, list):
            for item in value:
                elements(item)
        elif isinstance(value, dict):
            if "textRun" in value:
                segments.append(value["textRun"].get("content", ""))
            for key in ("content", "elements", "paragraph", "table", "tableRows", "tableCells", "tableOfContents"):
                if key in value and isinstance(value[key], (list, dict)):
                    elements(value[key])

    def visit(tab):
        props = tab.get("tabProperties", {})
        tab_ids.append(props.get("tabId", ""))
        segments.append("\n[" + props.get("title", "") + "]\n")
        dt = tab.get("documentTab") or {}
        elements(dt.get("body", {}))
        for key in ("headers", "footers", "footnotes"):
            for item in dt.get(key, {}).values():
                elements(item)
        for child in tab.get("childTabs", []):
            visit(child)

    extraction = document.get("extraction")
    if "extracted_text" in document:
        if not isinstance(document["extracted_text"], str) or not isinstance(extraction, dict):
            raise ConnectorError("document_extraction_invalid")
        body = document["extracted_text"]
        tab_ids = extraction.get("tab_ids", [])
    else:
        tabs = document.get("tabs")
        if not isinstance(tabs, list):
            raise ConnectorError("document_all_tabs_not_verified")
        for tab in tabs:
            visit(tab)
        body = "".join(segments).strip()
        extraction = {"format": "google_docs", "text_scope": "all_tabs_body_headers_footers_footnotes", "limitations": ["images_and_embedded_files_not_extracted"]}
    return {"source_id": source_key("drive", account, fid, "", "document"), "platform": "drive",
            "native_id": fid, "author_id": "", "author_name": "", "authorship": "unverified",
            "title": file.get("name") or document.get("title", ""), "body": "" if file.get("trashed") else body, "authored_text": "",
            "created_at": file.get("createdTime", ""), "modified_at": file.get("modifiedTime", ""),
            "url": file.get("webViewLink") or "https://drive.google.com/open?id=" + urllib.parse.quote(fid, safe=""),
            "scope": "drive:" + fid, "status": "unavailable" if file.get("trashed") else "active",
            "metadata": {"account_id": account, "file_id": fid, "source_kind": "document", "tab_ids": tab_ids,
                         "authorship_basis": "document_authorship_unverified", "native_revision": document.get("revisionId", ""),
                         "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "mime_type": file.get("mimeType", ""),
                         "extraction": extraction}}


def normalize_drive_document_views(file: dict, document: dict) -> list[dict]:
    """Keep base and suggestions-inclusive context as linked, unverified views."""
    canonical = normalize_drive_document(file, document)
    if "context_views" not in document:
        return [canonical]
    contexts = document["context_views"]
    if (canonical["metadata"]["extraction"].get("format") != "google_docs_structured_text"
            or canonical["metadata"]["extraction"].get("view_mode") != "PREVIEW_WITHOUT_SUGGESTIONS"
            or not isinstance(contexts, list) or len(contexts) != 1 or not isinstance(contexts[0], dict)
            or contexts[0].get("extraction", {}).get("view_mode") != "SUGGESTIONS_INLINE"
            or contexts[0].get("revisionId") != document.get("revisionId")):
        raise ConnectorError("document_docs_response_invalid")
    inline = normalize_drive_document(file, contexts[0])
    inline["source_id"] = source_key("drive", canonical["metadata"]["account_id"],
                                     canonical["native_id"], "", "document_suggestions_inline")
    for row, role in ((canonical, "base_without_suggestions"), (inline, "suggestions_inline_context")):
        row["metadata"].update(view_role=role, view_mode=row["metadata"]["extraction"]["view_mode"],
            canonical_document_source_id=canonical["source_id"], alternative_view_not_independent_evidence=True,
            paired_view_status="verified_together")
    return [canonical, inline]


class GogClient:
    """Existing gog credentials, narrowly allowed read commands, shell=False."""

    _ALLOWED = {
        ("gmail", "search"): {"--max", "--page"},
        ("gmail", "thread", "get"): {"--full"},
        ("gmail", "attachment"): {"--out"},
        ("gmail", "history"): {"--since", "--max", "--page"},
        ("drive", "ls"): {"--all", "--parent", "--query", "--max", "--page"},
        ("drive", "search"): {"--raw-query", "--max", "--page"},
        ("drive", "get"): set(),
        ("drive", "comments", "list"): {"--max", "--page", "--include-quoted"},
        ("docs", "cat"): {"--raw", "--all-tabs", "--max-bytes"},
        ("drive", "download"): {"--out", "--format"},
        ("slides", "list-slides"): set(),
        ("slides", "read-slide"): set(),
        ("sheets", "metadata"): set(),
        ("sheets", "get"): {"--dimension", "--render"},
        ("sheets", "notes"): set(),
    }

    def __init__(self, account: str, executable="/home/juke/bin/gog", *, runner=None, timeout=90,
                 document_cache_home=None, slides_text_executable=None, docs_text_executable=None,
                 pdf_ocr_tessdata=None):
        if slides_text_executable is not None and (
                not isinstance(slides_text_executable, str) or "\x00" in slides_text_executable
                or not Path(slides_text_executable).is_absolute()):
            raise ConnectorError("gog_slides_text_executable_invalid")
        if docs_text_executable is not None and (
                not isinstance(docs_text_executable, str) or "\x00" in docs_text_executable
                or not Path(docs_text_executable).is_absolute()):
            raise ConnectorError("gog_docs_text_executable_invalid")
        if pdf_ocr_tessdata is not None and (
                not isinstance(pdf_ocr_tessdata, str) or "\x00" in pdf_ocr_tessdata
                or not Path(pdf_ocr_tessdata).is_absolute()):
            raise ConnectorError("document_pdf_ocr_tessdata_invalid")
        self.account = _required_id(account)
        self.executable = executable
        self.runner = runner or subprocess.run
        self.timeout = timeout
        self.document_cache_home = document_cache_home
        self.slides_text_executable = slides_text_executable
        self.docs_text_executable = docs_text_executable
        self.pdf_ocr_tessdata = pdf_ocr_tessdata

    def discard_document_cache(self, file_id):
        """Discard only this account/file's incomplete native-Slides progress."""
        if self.document_cache_home is None:
            return {"state": "disabled"}
        from .document_resume import NativeSlidesResume, ResumeError
        try:
            return NativeSlidesResume(self.document_cache_home, self.account, _required_id(file_id),
                                      text_budget=_DOCUMENT_BYTES).discard()
        except ResumeError as exc:
            raise ConnectorError(exc.code) from None

    @staticmethod
    def _raise_read_failure(command, args, stderr):
        error = (stderr or "").lower()
        drive_read = command[0] in {"drive", "docs", "slides", "sheets"}
        native_export = command == ("drive", "download") and "--format=pptx" in args
        if (any(s in error for s in ("invalid_grant", "unauthorized", "reauth", "invalid credentials"))
                or (drive_read and re.search(r"\b(?:401|autherror)\b", error))):
            code = "gog_auth_required"
        elif drive_read and (re.search(r"\b(?:429|ratelimitexceeded|userratelimitexceeded)\b", error)
                             or "rate limit exceeded" in error):
            code = "gog_rate_limited"
        elif drive_read and re.search(r"\b(?:dailylimitexceeded|quotaexceeded|storagequotaexceeded)\b", error):
            code = "gog_quota_exceeded"
        elif drive_read and re.search(r"\b(?:insufficientfilepermissions|appnotauthorizedtofile|domainpolicy)\b", error):
            code = "source_access_denied"
        elif drive_read and re.search(r"\b(?:download_restricted_for_revision|downloadrestrictedforrevision)\b", error):
            code = "source_download_restricted"
        elif native_export and re.search(r"\bexportsizelimitexceeded\b", error):
            # Drive files.export can return 403 for export size while the
            # authorized Slides API still permits native content reads.
            code = "gog_native_export_size_limit"
        elif command == ("drive", "download") and re.search(r"\bfilenotexportable\b", error):
            code = "gog_export_unsupported"
        elif re.search(r"\b(?:403|forbidden|permissiondenied)\b", error):
            code = "source_access_denied"
        elif re.search(r"\b(?:404|notfound)\b|\bnot found\b", error):
            code = "source_not_found"
        elif ("dial tcp" in error and ("client.timeout exceeded" in error or "i/o timeout" in error)
                and "rate limit" not in error and "quota" not in error
                and not re.search(r"\b(?:401|403|404|429|ratelimitexceeded|userratelimitexceeded|dailylimitexceeded)\b", error)):
            # Observed during a Drive download, including a failed OAuth HTTP
            # connection. This describes transport, not rejected credentials.
            code = "gog_connect_timeout"
        elif command == ("docs", "cat") and "timeout awaiting response headers" in error:
            # Reproduced for one native Doc while its metadata remained
            # readable. Record a retryable document gap, independently of
            # comment access; other timeout/error types retain retry behavior.
            code = "document_native_doc_timeout"
        elif native_export and "timeout awaiting response headers" in error:
            # Observed on large native decks while Slides GETs still work.
            # Keep this separate from document gaps and generic network errors.
            code = "gog_native_export_timeout"
        else:
            code = "gog_read_failed"
        failure = ConnectorError(code)
        if drive_read:
            # Only fixed allowlisted command words, never argv or stderr.
            failure.operation = ".".join(command)
        raise failure

    def _call(self, command: tuple[str, ...], args: list[str], *, _download_root: str | None = None, _office_download: bool = False) -> dict:
        if command not in self._ALLOWED:
            raise ConnectorError("gog_command_not_readonly")
        if _office_download and command != ("drive", "download"):
            raise ConnectorError("document_download_budget_invalid")
        for arg in args:
            if "\x00" in arg or (arg.startswith("-") and arg.split("=", 1)[0] not in self._ALLOWED[command]):
                raise ConnectorError("gog_argument_not_allowed")
        extra = {}
        if command in {("drive", "download"),("gmail","attachment")}:
            # Only this adapter's private temporary path is a valid local target.
            if not _download_root or [a for a in args if a.startswith("--out=")] != ["--out=" + _download_root + "/source.bin"]:
                raise ConnectorError("document_private_download_required")
            root = Path(_download_root)
            if root.is_symlink() or root.stat().st_mode & 0o077:
                raise ConnectorError("document_private_download_required")
            extra["preexec_fn"] = _office_download_file_limit if _office_download else _document_file_limit
        argv = [self.executable, "--account=" + self.account, "--json", "--no-input", *command, *args]
        for attempt in range(2):
            try:
                result = self.runner(argv, shell=False, capture_output=True, text=True, timeout=self.timeout, check=False, **extra)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ConnectorError("gog_execution_failed") from exc
            if not result.returncode:
                break
            try:
                self._raise_read_failure(command, args, result.stderr)
            except ConnectorError as failure:
                # One retry for the confirmed connection-timeout shape only.
                # Never overwrite a partial download or delete caller files.
                if attempt or failure.code != "gog_connect_timeout":
                    raise
                empty_download = (_download_root is None or
                                  next(Path(_download_root).iterdir(), None) is None)
                if not empty_download:
                    raise
                time.sleep(2)
        if len(result.stdout.encode("utf-8")) > _DOCUMENT_BYTES:
            raise ConnectorError("document_response_budget_exceeded")
        try:
            data = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise ConnectorError("gog_invalid_json") from exc
        if not isinstance(data, dict):
            raise ConnectorError("gog_pagination_envelope_missing")
        return data

    def _pages(self, command, args, key, *, progress=None, start_page=None, max_pages=None):
        progress = progress or SyncProgress()
        cursor = start_page
        seen = set()
        while True:
            if cursor in seen:
                progress.failure = "pagination_cycle"
                raise ConnectorError("pagination_cycle")
            seen.add(cursor)
            progress.remaining_page_token = cursor
            try:
                page = self._call(command, [*args, *(["--page=" + cursor] if cursor else [])])
                if page.get("incompleteSearch"):
                    raise ConnectorError("drive_incomplete_search")
                if key not in page:
                    raise ConnectorError("unexpected_collection_shape")
                items = page.get(key)
                if items is None:
                    items = []  # Empty API collections may be omitted.
                if not isinstance(items, list):
                    raise ConnectorError("unexpected_collection_shape")
                next_cursor = page.get("nextPageToken") or page.get("next_page_token")
                if next_cursor is not None and not isinstance(next_cursor, str):
                    raise ConnectorError("invalid_page_token")
            except ConnectorError as exc:
                progress.failure = exc.code
                raise
            progress.pages += 1
            yield items, next_cursor
            progress.items += len(items)
            progress.remaining_page_token = next_cursor
            if not next_cursor:
                progress.complete = True
                return
            if max_pages is not None and progress.pages >= max_pages:
                progress.limitations.append("page_cap_reached")
                return
            cursor = next_cursor

    def get_thread(self, thread_id):
        data = self._call(("gmail", "thread", "get"), [_required_id(thread_id), "--full"])
        data["_account_id"] = self.account
        budget=[0]
        for message in data.get("thread",data).get("messages",[]):
            if isinstance(message,dict) and isinstance(message.get("payload"),dict):
                self._hydrate_inline_body(message["payload"],_required_id(message.get("id")),budget)
        return data

    def _hydrate_inline_body(self,part,message_id,budget):
        """Fetch Gmail's detached text body, never ordinary file attachments."""
        mime=str(part.get("mimeType","")).lower()
        disposition=next((h.get("value","") for h in part.get("headers",[])
                          if h.get("name","").lower()=="content-disposition"),"")
        if part.get("filename") or disposition.lower().split(";",1)[0].strip()=="attachment":return
        if not (mime in {"text/plain","text/html"} or mime.startswith("multipart/")):return
        children=part.get("parts") or []
        if not isinstance(children,list):raise ConnectorError("malformed_mime_children")
        for child in children:
            if not isinstance(child,dict):raise ConnectorError("malformed_mime_part")
            self._hydrate_inline_body(child,message_id,budget)
        body=part.get("body") or {}
        if mime not in {"text/plain","text/html"} or body.get("data") is not None or not body.get("attachmentId"):return
        size=body.get("size")
        if type(size) is not int or size<0:raise ConnectorError("gmail_body_size_missing")
        if budget[0]+size>_DOCUMENT_BYTES:raise ConnectorError("gmail_body_budget_exceeded")
        with tempfile.TemporaryDirectory(prefix="cha-gmail-body-") as directory:
            os.chmod(directory,0o700);path=Path(directory)/"source.bin"
            self._call(("gmail","attachment"),[message_id,_required_id(body["attachmentId"]),"--out="+str(path)],
                       _download_root=directory)
            if not path.is_file() or path.is_symlink() or path.stat().st_size!=size:
                raise ConnectorError("gmail_body_download_mismatch")
            raw=path.read_bytes()
        budget[0]+=len(raw)
        body["data"]=base64.urlsafe_b64encode(raw).decode("ascii")
        part["body"]=body

    def gmail_page(self, query, page_token=""):
        args = [query, "--max=100"] + (["--page=" + page_token] if page_token else [])
        return self._call(("gmail", "search"), args)

    def get_gmail_thread(self, thread_id):
        return self.get_thread(thread_id)

    def drive_page(self, query, page_token=""):
        args = [query, "--raw-query", "--max=100"] + (["--page=" + page_token] if page_token else [])
        return self._call(("drive", "search"), args)

    def drive_comments_page(self, file_id, page_token=""):
        args = [_required_id(file_id), "--max=100", "--include-quoted"] + (["--page=" + page_token] if page_token else [])
        return self._call(("drive", "comments", "list"), args)

    def iter_sent_threads(self, identities: set[str], lab_recipients: set[str], *, additional_query="", query=None, max_threads=None, progress=None, start_page=None,
                          lab_project_routes=None):
        progress = progress or SyncProgress()
        routes = validate_lab_project_routes(lab_project_routes)
        if not identities or not (lab_recipients or routes):
            raise ConnectorError("gmail_identity_and_lab_scope_required")
        identity_terms = " ".join("from:" + _required_id(x) for x in sorted(identities))
        recipients = [*sorted(lab_recipients), *(address for address in routes if address not in {x.lower() for x in lab_recipients})]
        lab_terms = " ".join(f"{field}:{_required_id(addr)}" for addr in recipients for field in ("to", "cc", "bcc"))
        search = query or f"in:sent {{{identity_terms}}} {{{lab_terms}}} {additional_query}".strip()
        yielded = 0
        for threads, next_cursor in self._pages(("gmail", "search"), [search, "--max=100"], "threads", progress=progress, start_page=start_page):
            ids = [_required_id(t.get("id")) for t in threads]
            progress.pending_ids = ids.copy()
            for tid in ids:
                if max_threads is not None and yielded >= max_threads:
                    progress.limitations.append("thread_cap_reached")
                    # Current page cursor plus remaining IDs permits explicit replay/resume.
                    return
                try:
                    thread = self.get_thread(tid)
                    rows = normalize_gmail(thread, identities, lab_recipients, lab_project_routes=routes)
                except ConnectorError as exc:
                    progress.failure = exc.code
                    raise
                if rows:
                    yielded += 1
                    yield thread
                progress.pending_ids.pop(0)

    def iter_drive_inventory(self, *, query=None, folder_ids=None, progress=None, start_page=None, max_pages=None):
        progress = progress or SyncProgress()
        if folder_ids:
            if start_page:
                raise ConnectorError("folder_resume_requires_per_folder_cursor")
            # Explicit recursion; shortcut targets do not silently extend scope.
            queue = list(folder_ids)
            visited = set()
            while queue:
                fid = _required_id(queue.pop(0))
                if fid in visited:
                    continue
                visited.add(fid)
                local = SyncProgress()
                # Always discover folders even when the requested content query is Docs-only.
                traversal_query = f"({query}) or mimeType = 'application/vnd.google-apps.folder'" if query else None
                args = ["--parent=" + fid, "--max=100"] + (["--query=" + traversal_query] if traversal_query else [])
                for files, _ in self._pages(("drive", "ls"), args, "files", progress=local, max_pages=max_pages):
                    for file in files:
                        file["_account_id"] = self.account
                        if file.get("mimeType") == "application/vnd.google-apps.folder" and not file.get("trashed"):
                            queue.append(file["id"])
                        yield file
                        progress.items += 1
                progress.pages += local.pages
                if not local.complete:
                    progress.remaining_page_token = local.remaining_page_token
                    progress.pending_ids = [fid, *queue]
                    progress.limitations.extend(local.limitations)
                    return
            progress.complete = True
            return
        command, args = (("drive", "search"), [query, "--raw-query", "--max=100"]) if query else (("drive", "ls"), ["--all", "--max=100"])
        for files, _ in self._pages(command, args, "files", progress=progress, start_page=start_page, max_pages=max_pages):
            for file in files:
                file["_account_id"] = self.account
                yield file

    def get_drive_file(self, file_id):
        data = self._call(("drive", "get"), [_required_id(file_id)])
        data = data.get("file", data)
        data["_account_id"] = self.account
        return data

    def get_drive_metadata(self, file_id):
        return self.get_drive_file(file_id)

    def get_drive_document(self, file_id):
        data = self._call(("docs", "cat"), [_required_id(file_id), "--raw", "--all-tabs", "--max-bytes=0"])
        document = data.get("document", data)
        if "tabs" not in document:
            raise ConnectorError("document_all_tabs_not_verified")
        return document

    def read_document(self, filemeta: dict) -> dict:
        try:
            return self._read_document(filemeta)
        except ConnectorError as exc:
            if (self.document_cache_home is not None
                    and filemeta.get("mimeType") == "application/vnd.google-apps.presentation"
                    and exc.code in {"source_access_denied", "source_not_found"}):
                # Access can be lost in the initial inventory/export before the
                # fallback has opened its progress cache.
                self.discard_document_cache(filemeta.get("id"))
            raise

    def _read_document(self, filemeta: dict) -> dict:
        """Read supported scoped Drive metadata; no format guessing or authorship inference."""
        fid = _required_id(filemeta.get("id"))
        mime = filemeta.get("mimeType")
        if mime not in READABLE_DRIVE_MIME_TYPES:
            raise ConnectorError("document_format_unsupported")
        if filemeta.get("trashed"):
            raise ConnectorError("source_access_denied")
        if mime == "application/vnd.google-apps.document":
            if self.docs_text_executable is not None:
                return self._read_structured_docs_text(filemeta)
            return self.get_drive_document(fid)
        if mime == "application/vnd.google-apps.spreadsheet":
            return self._read_sheet_document(fid)
        native_slides = mime == "application/vnd.google-apps.presentation"
        if native_slides and self.slides_text_executable is not None:
            return self._read_structured_slide_text(filemeta)
        office_download = mime in _OFFICE_MIME_TYPES
        download_budget = _OFFICE_DOWNLOAD_BYTES if office_download else _DOCUMENT_BYTES
        inventory = self._call(("slides", "list-slides"), [fid]) if native_slides else None
        if inventory is not None and (not isinstance(inventory.get("slides"), list) or inventory.get("slideCount") != len(inventory["slides"])):
            raise ConnectorError("document_slide_inventory_invalid")
        missing_text_size = (mime.startswith("text/") and filemeta.get("size") is None
                             and isinstance(filemeta.get("modifiedTime"), str)
                             and bool(filemeta["modifiedTime"].strip()))
        if not native_slides and not missing_text_size:
            try:
                size = int(filemeta["size"])
            except (KeyError, ValueError, TypeError):
                raise ConnectorError("document_size_metadata_required") from None
            if size < 0 or size > download_budget:
                raise ConnectorError("document_download_budget_exceeded")
        with tempfile.TemporaryDirectory(prefix="cha-philosophy-document-") as folder:
            path = Path(folder) / "source.bin"
            try:
                downloaded = self._call(("drive", "download"), [fid, "--out=" + str(path), *(["--format=pptx"] if native_slides else [])], _download_root=folder, _office_download=office_download)
            except ConnectorError as exc:
                if not native_slides or exc.code not in {"gog_native_export_timeout", "gog_native_export_size_limit"}:
                    raise
                return self._read_native_slide_text(filemeta, inventory, fallback_reason=exc.code)
            # gog replaces an export target's extension (source.bin -> source.pptx).
            if "path" in downloaded:
                if not isinstance(downloaded["path"], str):
                    raise ConnectorError("document_download_invalid")
                candidate = Path(downloaded["path"])
                if candidate.parent != Path(folder) or candidate.name not in ({"source.bin", "source.pptx"} if native_slides else {"source.bin"}):
                    raise ConnectorError("document_download_invalid")
                path = candidate
            if not path.is_file() or path.is_symlink() or path.stat().st_size > download_budget:
                raise ConnectorError("document_download_invalid")
            os.chmod(path, 0o600)
            if missing_text_size:
                # gog may omit size for a zero-byte stored text file. Do not
                # assume missing means empty: download under the same OS file
                # cap, observe actual bytes and recheck the document revision.
                current=self.get_drive_metadata(fid)
                if current.get("trashed"):
                    raise ConnectorError("source_access_denied")
                if any(current.get(key)!=filemeta.get(key) for key in ("id","mimeType","modifiedTime")):
                    raise ConnectorError("document_changed_during_read")
                final_size=current.get("size")
                if final_size is not None:
                    # A later size can contradict a supposedly empty/truncated
                    # download even when the revision timestamp is unchanged.
                    if (isinstance(final_size,bool) or not isinstance(final_size,(int,str))
                            or not re.fullmatch(r"[0-9]+",str(final_size))):
                        raise ConnectorError("document_size_metadata_required")
                    final_size=int(final_size)
                    if final_size>download_budget:
                        raise ConnectorError("document_download_budget_exceeded")
                    if final_size!=path.stat().st_size:
                        raise ConnectorError("document_download_invalid")
            if mime in ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/vnd.google-apps.presentation"):
                document = _office_text(path, presentation=True)
                if inventory is not None:
                    if document["extraction"]["slide_count"] != inventory["slideCount"]:
                        raise ConnectorError("document_slide_count_changed")
                    document["extraction"].update(format="google_slides_pptx_export", native_slide_ids=[_required_id(s.get("objectId")) for s in inventory["slides"]])
            elif mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                document = _office_text(path, presentation=False)
            elif mime == "application/pdf":
                output = Path(folder) / "text.txt"
                try:
                    converted = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), str(output)], shell=False,
                                               capture_output=True, text=True, timeout=self.timeout, check=False, preexec_fn=_document_file_limit)
                except FileNotFoundError:
                    raise ConnectorError("document_pdf_parser_unavailable") from None
                except (OSError, subprocess.TimeoutExpired):
                    raise ConnectorError("document_pdf_parse_failed") from None
                if converted.returncode or not output.is_file() or output.stat().st_size > _DOCUMENT_BYTES:
                    raise ConnectorError("document_pdf_parse_failed")
                text = output.read_text(encoding="utf-8")
                if not text.strip():
                    if self.pdf_ocr_tessdata is None:
                        raise ConnectorError("document_pdf_requires_ocr")
                    if path.stat().st_size != size:
                        raise ConnectorError("document_download_invalid")
                    if not isinstance(filemeta.get("modifiedTime"), str) or not filemeta["modifiedTime"].strip():
                        raise ConnectorError("document_pdf_ocr_revision_required")
                    from .pdf_ocr import PdfOcrError, extract_pdf_ocr
                    try:
                        document = extract_pdf_ocr(path, self.pdf_ocr_tessdata)
                    except PdfOcrError as exc:
                        raise ConnectorError("document_" + exc.code) from None
                    try:
                        current = self.get_drive_metadata(fid)
                    except ConnectorError as exc:
                        if exc.code in {"source_access_denied", "source_not_found"}:
                            exc.file_access_withdrawn = True
                        raise
                    if current.get("trashed"):
                        failure = ConnectorError("source_access_denied")
                        failure.file_access_withdrawn = True
                        raise failure
                    if any(current.get(key) != filemeta.get(key) for key in ("id", "mimeType", "modifiedTime")):
                        raise ConnectorError("document_changed_during_read")
                    if current.get("size") is not None and str(current["size"]) != str(size):
                        raise ConnectorError("document_changed_during_read")
                    document["extraction"].update(
                        drive_metadata_rechecked=True, fallback_reason="document_pdf_requires_ocr")
                else:
                    document = _extracted_document(text, "pdf", "extractable_text_all_pages", limitations=["images_and_scanned_text_not_extracted", "reading_order_not_verified"])
            else:
                try:
                    text = path.read_bytes().decode("utf-8-sig", errors="strict")
                except UnicodeError:
                    raise ConnectorError("document_text_encoding_unsupported") from None
                document = _extracted_document(text, mime, "stored_utf8_text", limitations=[])
            if missing_text_size:
                document["extraction"].update(size_metadata_available=False,
                    downloaded_bytes=path.stat().st_size,
                    empty_file=path.stat().st_size==0,
                    revision_rechecked_after_download=True)
            return document

    def _call_document_text(self, fid):
        """Only the two-view Docs command gets the longer, fixed outer timeout."""
        command = ("docs", "read-document-text")
        args = [_required_id(fid), "--max-bytes=" + str(_DOCUMENT_BYTES)]
        argv = [self.docs_text_executable, "--account=" + self.account, "--no-input", "--json", *command, *args]
        try:
            result = self.runner(argv, shell=False, capture_output=True, text=True, timeout=270, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ConnectorError("gog_execution_failed") from None
        if result.returncode:
            marker = re.search(r"\bdocs_text_([a-z_]+)\b", result.stderr or "")
            if marker and marker[1] in _DOCS_TEXT_ERRORS:
                failure = ConnectorError(_DOCS_TEXT_ERRORS[marker[1]])
                failure.operation = ".".join(command)
                raise failure
            self._raise_read_failure(command, args, result.stderr)

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON field")
                value[key] = item
            return value

        def invalid_constant(value):
            raise ValueError("nonfinite JSON value")

        try:
            if len(result.stdout.encode("utf-8")) > _DOCUMENT_BYTES:
                raise ConnectorError("document_response_budget_exceeded")
            return json.loads(result.stdout, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ConnectorError("gog_invalid_json") from None

    def _read_structured_docs_text(self, filemeta):
        from .docs_text import parse_document_views
        fid = _required_id(filemeta.get("id"))
        if not isinstance(filemeta.get("modifiedTime"), str) or not filemeta["modifiedTime"].strip():
            raise ConnectorError("document_docs_revision_required")
        document = parse_document_views(fid, self._call_document_text(fid))
        try:
            current = self.get_drive_metadata(fid)
        except ConnectorError as exc:
            if exc.code in {"source_access_denied", "source_not_found"}:
                exc.file_access_withdrawn = True
            raise
        if current.get("trashed"):
            failure = ConnectorError("source_access_denied")
            failure.operation = "drive.get"
            failure.file_access_withdrawn = True
            raise failure
        if any(current.get(key) != filemeta.get(key) for key in ("id", "mimeType", "modifiedTime")):
            raise ConnectorError("document_changed_during_read")
        for view in [document, *document["context_views"]]:
            view["extraction"]["drive_metadata_rechecked"] = True
        return sanitize_source(document)

    def _call_presentation_text(self, fid):
        """Only this fixed read command may use the explicitly selected binary."""
        command = ("slides", "read-presentation-text")
        args = [_required_id(fid), "--max-bytes=" + str(_DOCUMENT_BYTES)]
        argv = [self.slides_text_executable, "--account=" + self.account, "--no-input", "--json", *command, *args]
        try:
            result = self.runner(argv, shell=False, capture_output=True, text=True,
                                 timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise ConnectorError("gog_execution_failed") from None
        if result.returncode:
            marker = re.search(r"\bslides_text_([a-z_]+)\b", result.stderr or "")
            if marker and marker[1] in _SLIDES_TEXT_ERRORS:
                failure = ConnectorError(_SLIDES_TEXT_ERRORS[marker[1]])
                failure.operation = ".".join(command)
                raise failure
            self._raise_read_failure(command, args, result.stderr)

        def unique_object(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate JSON field")
                value[key] = item
            return value

        def invalid_constant(value):
            raise ValueError("nonfinite JSON value")

        try:
            if len(result.stdout.encode("utf-8")) > _DOCUMENT_BYTES:
                raise ConnectorError("document_response_budget_exceeded")
            return json.loads(result.stdout, object_pairs_hook=unique_object, parse_constant=invalid_constant)
        except (ValueError, TypeError, UnicodeError, AttributeError):
            raise ConnectorError("gog_invalid_json") from None

    def _read_structured_slide_text(self, filemeta):
        fid = _required_id(filemeta.get("id"))
        if not isinstance(filemeta.get("modifiedTime"), str) or not filemeta["modifiedTime"].strip():
            raise ConnectorError("document_slide_revision_required")
        try:
            payload = self._call_presentation_text(fid)
            document = self._structured_slide_document(fid, payload)
            current = self.get_drive_metadata(fid)
            if current.get("trashed"):
                raise ConnectorError("source_access_denied")
            if any(current.get(key) != filemeta.get(key) for key in ("id", "mimeType", "modifiedTime")):
                raise ConnectorError("document_changed_during_read")
            # Never combine cached top-level text with the structured result.
            # Only a complete read and final revision check can retire it.
            self.discard_document_cache(fid)
            document["extraction"]["drive_metadata_rechecked"] = True
            return sanitize_source(document)
        except ConnectorError as exc:
            if exc.code in {"document_changed_during_read", "document_slide_inventory_changed"}:
                self.discard_document_cache(fid)
            raise

    @staticmethod
    def _structured_slide_document(fid, payload):
        """Validate the versioned local reader contract; preserve API text order."""
        def invalid():
            raise ConnectorError("document_slide_response_invalid")

        def identifier(value):
            return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,256}", value) is not None

        def text_size(value):
            try:
                return len(value.encode("utf-8"))
            except UnicodeError:
                invalid()

        keys = {"schemaVersion", "presentationId", "title", "revisionId", "slideCount", "slides", "textBytes",
                "revisionAndOrderRechecked", "slideOrderRechecked", "partialText", "limitations"}
        if not isinstance(payload, dict) or set(payload) != keys:
            invalid()
        revision = payload["revisionId"]
        if (type(payload["schemaVersion"]) is not int or payload["schemaVersion"] != 1
                or payload["presentationId"] != fid or not isinstance(payload["title"], str)
                or not isinstance(revision, str) or (revision and not revision.strip())
                or payload["slideOrderRechecked"] is not True or payload["partialText"] is not True
                or payload["revisionAndOrderRechecked"] is not bool(revision)):
            invalid()
        limitations = payload["limitations"]
        expected_limitations = _SLIDES_TEXT_LIMITATIONS | ({"revision_id_unavailable"} if not revision else set())
        if (not isinstance(limitations, list) or any(not isinstance(item, str) for item in limitations)
                or len(limitations) != len(set(limitations)) or set(limitations) != expected_limitations):
            invalid()
        slides = payload["slides"]
        if (not isinstance(slides, list) or type(payload["slideCount"]) is not int
                or payload["slideCount"] != len(slides) or not 0 <= len(slides) <= 4096
                or type(payload["textBytes"]) is not int or not 0 <= payload["textBytes"] <= _DOCUMENT_BYTES):
            invalid()
        ids, parts, text_bytes, records = [], [], text_size(payload["title"]), 0
        for number, slide in enumerate(slides, 1):
            if (not isinstance(slide, dict) or set(slide) != {"objectId", "number", "elements", "notes"}
                    or not identifier(slide["objectId"]) or slide["objectId"] in ids
                    or type(slide["number"]) is not int or slide["number"] != number):
                invalid()
            ids.append(slide["objectId"])
            part = ["[slide " + str(number) + "]"]
            for section in ("elements", "notes"):
                if not isinstance(slide[section], list):
                    invalid()
                seen = set()
                if section == "notes" and slide[section]:
                    part.append("[notes page]")
                for record in slide[section]:
                    records += 1
                    if records > 200000:
                        raise ConnectorError("document_slide_node_budget_exceeded")
                    base = {"objectId", "kind", "groupPath", "text"}
                    if not isinstance(record, dict) or not base <= set(record):
                        invalid()
                    group = record["groupPath"]
                    if (not identifier(record["objectId"]) or not isinstance(record["text"], str)
                            or not isinstance(group, list) or len(group) > 8
                            or any(not identifier(item) for item in group) or len(set(group)) != len(group)):
                        invalid()
                    kind = record["kind"]
                    if kind in ("shape", "word_art"):
                        optional = {"placeholderType"} if kind == "shape" else set()
                        if (set(record) - base - optional or not record["text"]
                                or ("placeholderType" in record and
                                    (not isinstance(record["placeholderType"], str) or not record["placeholderType"]))):
                            invalid()
                        position = (kind, tuple(group), record["objectId"])
                        label = ("shape " if kind == "shape" else "word art ") + record["objectId"]
                    elif kind == "table_cell":
                        if (set(record) != base | {"rowIndex", "cellIndex"}
                                or any(type(record[key]) is not int or record[key] < 0 for key in ("rowIndex", "cellIndex"))):
                            invalid()
                        position = (kind, tuple(group), record["objectId"], record["rowIndex"], record["cellIndex"])
                        label = "table " + record["objectId"] + " row " + str(record["rowIndex"]) + " cell " + str(record["cellIndex"])
                    else:
                        invalid()
                    if position in seen:
                        invalid()
                    seen.add(position)
                    if group:
                        label += " groups " + "/".join(group)
                    part.extend(["[" + label + "]", record["text"]])
                    text_bytes += text_size(record["text"])
            parts.append("\n".join(part))
        if text_bytes != payload["textBytes"]:
            invalid()
        document = _extracted_document("\n\n".join(parts), "google_slides_structured_text",
            "all_slides_shape_group_table_word_art_and_all_notes_page_text", slide_count=len(ids), native_slide_ids=ids,
            partial_text=True, limitations=sorted(limitations), structured_text_bytes=text_bytes,
            slides_revision_and_order_rechecked=bool(revision), slides_order_rechecked=True,
            revision_id_available=bool(revision), structure_schema_version=1)
        document["revisionId"] = revision
        return document

    def _read_native_slide_text(self, filemeta: dict, inventory: dict, *,
                                fallback_reason="gog_native_export_timeout") -> dict:
        """Recover a proven export timeout/size failure via authorized Slides GETs.

        That version reads only top-level shapes and the speaker-notes BODY
        placeholder. Never preserve image URLs or claim group/table coverage.
        Every slide must succeed; transport/auth failures preserve the checkpoint.
        """
        if fallback_reason not in {"gog_native_export_timeout", "gog_native_export_size_limit"}:
            raise ConnectorError("document_slide_fallback_reason_invalid")
        fid = _required_id(filemeta.get("id"))

        def slide_ids(page):
            if (not isinstance(page, dict) or page.get("presentationId", fid) != fid
                    or not isinstance(page.get("slides"), list)
                    or page.get("slideCount") != len(page["slides"])):
                raise ConnectorError("document_slide_inventory_invalid")
            ids = [_required_id(s.get("objectId")) for s in page["slides"] if isinstance(s, dict)]
            if len(ids) != len(page["slides"]) or len(ids) != len(set(ids)):
                raise ConnectorError("document_slide_inventory_invalid")
            return ids

        ids = slide_ids(inventory)
        from .document_resume import NativeSlidesResume, ResumeError
        checkpoint = (NativeSlidesResume(self.document_cache_home, self.account, fid, text_budget=_DOCUMENT_BYTES)
                      if self.document_cache_home is not None else None)

        def verify_revision():
            current = self.get_drive_metadata(fid)
            if current.get("trashed"):
                raise ConnectorError("source_access_denied")
            keys = ("id", "mimeType", "modifiedTime") if checkpoint else ("modifiedTime",)
            if any(current.get(key) != filemeta.get(key) for key in keys):
                raise ConnectorError("document_changed_during_read")

        try:
            with checkpoint if checkpoint is not None else nullcontext():
                try:
                    parts = []
                    if checkpoint is not None:
                        if (filemeta.get("mimeType") != "application/vnd.google-apps.presentation"
                                or not isinstance(filemeta.get("modifiedTime"), str) or not filemeta["modifiedTime"].strip()):
                            raise ConnectorError("document_resume_revision_required")
                        verify_revision()
                        if slide_ids(self._call(("slides", "list-slides"), [fid])) != ids:
                            raise ConnectorError("document_slide_inventory_changed")
                        parts = checkpoint.load({"mime_type": filemeta["mimeType"], "modified_time": filemeta["modifiedTime"],
                            "slide_ids": ids, "slide_order_sha256": hashlib.sha256(json.dumps(ids, ensure_ascii=False,
                                separators=(",", ":")).encode("utf-8")).hexdigest()})
                    resumed = len(parts)
                    byte_count = sum(len(part.encode("utf-8")) + 2 for part in parts)
                    for number in range(resumed + 1, len(ids) + 1):
                        sid = ids[number - 1]
                        slide = self._call(("slides", "read-slide"), [fid, sid])
                        if (slide.get("presentationId") != fid or slide.get("slideObjectId") != sid
                                or slide.get("slideNumber") != number or not isinstance(slide.get("notes"), str)):
                            raise ConnectorError("document_slide_response_invalid")
                        elements = slide.get("textElements")
                        if elements is None:
                            elements = []  # gog serializes its nil slice as JSON null.
                        if not isinstance(elements, list) or any(not isinstance(e, dict) or not isinstance(e.get("text"), str) for e in elements):
                            raise ConnectorError("document_slide_response_invalid")
                        part = "[slide " + str(number) + "]\n" + "\n".join(e["text"] for e in elements)
                        if slide["notes"]:
                            part += "\n[speaker notes]\n" + slide["notes"]
                        byte_count += len(part.encode("utf-8")) + 2
                        if byte_count > _DOCUMENT_BYTES:
                            raise ConnectorError("document_text_budget_exceeded")
                        if checkpoint is not None:
                            checkpoint.append(sid, part)
                        parts.append(part)
                    if slide_ids(self._call(("slides", "list-slides"), [fid])) != ids:
                        raise ConnectorError("document_slide_inventory_changed")
                    if filemeta.get("modifiedTime"):
                        verify_revision()
                    document = _extracted_document("\n\n".join(parts), "google_slides_native_text", "all_slides_top_level_shape_text_and_speaker_notes",
                        slide_count=len(ids), native_slide_ids=ids, partial_text=True, fallback_reason=fallback_reason,
                        limitations=["group_and_table_text_not_extracted", "images_and_embedded_files_not_extracted",
                                     "master_and_layout_text_not_extracted", "non_body_speaker_note_shapes_not_extracted",
                                     "slide_text_formatting_not_preserved"])
                    if checkpoint is not None:
                        checkpoint.finish()
                        document["extraction"].update(resumed_slide_count=resumed, resume_revision_and_order_rechecked=True)
                    return document
                except ConnectorError as exc:
                    if checkpoint is not None and exc.code in {"source_access_denied", "source_not_found",
                            "document_changed_during_read", "document_slide_inventory_changed"}:
                        checkpoint.clear()
                    raise
        except ResumeError as exc:
            raise ConnectorError(exc.code) from None
        except OSError:
            if checkpoint is not None:
                raise ConnectorError("document_resume_write_failed") from None
            raise

    def _read_sheet_document(self, fid: str) -> dict:
        inventory = self._call(("sheets", "metadata"), [fid])
        tabs = inventory.get("sheets")
        if not isinstance(tabs, list):
            raise ConnectorError("document_all_sheets_not_verified")
        cells = 0
        specs = []
        for tab in tabs:
            props = tab.get("properties") or {}
            title = props.get("title")
            grid = props.get("gridProperties") or {}
            if not isinstance(title, str) or not title or "\x00" in title:
                raise ConnectorError("document_sheet_title_invalid")
            try:
                rows, cols = int(grid["rowCount"]), int(grid["columnCount"])
            except (KeyError, ValueError, TypeError):
                raise ConnectorError("document_sheet_grid_unavailable") from None
            if rows < 1 or cols < 1:
                raise ConnectorError("document_sheet_grid_unavailable")
            cells += rows * cols
            if cells > 2_000_000:
                raise ConnectorError("document_sheet_cell_budget_exceeded")
            letters = ""
            number = cols
            while number:
                number, remainder = divmod(number - 1, 26)
                letters = chr(65 + remainder) + letters
            specs.append((props, title, rows, letters))
        pieces = []
        count_bytes = 0
        for props, title, rows, letters in specs:
            pieces.append("[sheet " + title + "]")
            quoted = "'" + title.replace("'", "''") + "'!"
            for start in range(1, rows + 1, 1000):
                range_name = quoted + "A" + str(start) + ":" + letters + str(min(start + 999, rows))
                values = self._call(("sheets", "get"), [fid, range_name, "--dimension=ROWS", "--render=FORMATTED_VALUE"])
                notes = self._call(("sheets", "notes"), [fid, range_name])
                if "values" not in values or not isinstance(values.get("values") or [], list) or "notes" not in notes or not isinstance(notes.get("notes") or [], list):
                    raise ConnectorError("document_sheet_response_invalid")
                for offset, row in enumerate(values.get("values") or []):
                    if not isinstance(row, list):
                        raise ConnectorError("document_sheet_response_invalid")
                    if any(str(v) for v in row):
                        piece = "[row " + str(start + offset) + "] " + "\t".join(str(v) for v in row)
                        pieces.append(piece)
                        count_bytes += len(piece.encode("utf-8"))
                for note in notes.get("notes") or []:
                    if not isinstance(note, dict) or not isinstance(note.get("note"), str):
                        raise ConnectorError("document_sheet_response_invalid")
                    piece = "[cell note " + str(note.get("a1") or "") + "] " + note["note"]
                    pieces.append(piece)
                    count_bytes += len(piece.encode("utf-8"))
                if count_bytes > _DOCUMENT_BYTES:
                    raise ConnectorError("document_text_budget_exceeded")
        return _extracted_document("\n".join(pieces), "google_sheets", "all_grid_tabs_formatted_values_and_cell_notes", tab_ids=[str(p.get("sheetId", "")) for p, _, _, _ in specs],
                                   limitations=["formulas_and_chart_text_not_extracted", "images_and_embedded_files_not_extracted"])

    def iter_drive_comments(self, file_id, *, progress=None, start_page=None, max_pages=None):
        progress = progress or SyncProgress()
        progress.limitations.append("gog_comments_no_include_deleted_flag")
        for comments, _ in self._pages(("drive", "comments", "list"), [_required_id(file_id), "--max=100", "--include-quoted"], "comments", progress=progress, start_page=start_page, max_pages=max_pages):
            yield from comments


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward Authorization to a redirected host.


class TeamsGraphClient:
    BASE = "https://graph.microsoft.com/v1.0"

    def __init__(self, token: str | None = None, *, token_provider=None, opener=None,
                 sleep: Callable = time.sleep, max_retries=3, timeout=45):
        if token_provider is not None and (not callable(token_provider) or token is not None):
            raise ConnectorError("teams_token_provider_invalid")
        if token_provider is None and not self._valid_token(token):
            raise ConnectorError("teams_token_missing_or_invalid")
        self._token = token
        self._token_provider = token_provider
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.sleep = sleep
        self.max_retries = max_retries
        self.timeout = timeout

    @staticmethod
    def _valid_token(value):
        return isinstance(value,str) and bool(value.strip()) and len(value)<=65536 and not any(
            char in value for char in ("\n","\r","\x00"))

    def _get_token(self, *, force_refresh=False):
        if self._token_provider is None:
            return self._token
        try:
            token=self._token_provider(force_refresh=force_refresh)
        except ConnectorError:
            raise
        except Exception:
            raise ConnectorError("teams_auth_provider_failed") from None
        if not self._valid_token(token):
            raise ConnectorError("teams_token_missing_or_invalid")
        return token

    @classmethod
    def from_env(cls, env_name="CHA_TEAMS_ACCESS_TOKEN", **kwargs):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
            raise ConnectorError("invalid_token_env_name")
        return cls(os.environ.get(env_name, ""), **kwargs)

    @classmethod
    def from_config(cls, config, private_home, **kwargs):
        if "teams_auth" not in config:
            return cls.from_env(**kwargs)
        # Selecting a bound MSAL profile must not silently fall back to an
        # unrelated environment token when its configuration/cache fails.
        from .teams_auth import TeamsAuth
        auth=TeamsAuth(config,private_home)
        return cls(token_provider=auth.acquire_silent,**kwargs)

    def _url(self, path):
        if not isinstance(path, str):
            raise ConnectorError("graph_url_not_allowed")
        url = path if path.startswith("https://") else self.BASE + path if path.startswith("/") else ""
        parsed = None
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = parsed.scheme == "https" and parsed.hostname == "graph.microsoft.com" and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment and parsed.path.startswith("/v1.0/")
        except ValueError:
            valid = False
        if not valid or not parsed or "/../" in urllib.parse.unquote(parsed.path):
            raise ConnectorError("graph_url_not_allowed")
        return url

    def get_json(self, path):
        url = self._url(path)
        token=self._get_token(); attempt=0; auth_retry=False
        while True:
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, method="GET")
            try:
                response = self.opener.open(req, timeout=self.timeout)
                try:
                    data = json.loads(response.read())
                finally:
                    response.close()
                if not isinstance(data, dict):
                    raise ConnectorError("graph_invalid_response")
                return data
            except urllib.error.HTTPError as exc:
                if exc.code==401 and self._token_provider is not None:
                    challenge=exc.headers.get("WWW-Authenticate", "").lower() if exc.headers else ""
                    if "insufficient_claims" in challenge or "claims=" in challenge:
                        raise ConnectorError("teams_reauth_required") from None
                    if not auth_retry:
                        token=self._get_token(force_refresh=True); auth_retry=True
                        continue
                if exc.code in (429, 503) and attempt < self.max_retries:
                    retry = exc.headers.get("Retry-After", "") if exc.headers else ""
                    try:
                        delay = max(0, float(retry)) if retry else float(2 ** attempt)
                    except ValueError:
                        try:
                            delay = max(0, email.utils.parsedate_to_datetime(retry).timestamp() - time.time())
                        except (ValueError, TypeError, AttributeError):
                            delay = float(2 ** attempt)
                    if delay > 60:
                        raise ConnectorError("graph_retry_later", retry_after=delay) from None
                    self.sleep(delay)
                    attempt+=1
                    continue
                code = {401: "teams_auth_required", 403: "teams_access_denied", 404: "teams_source_unavailable", 410: "teams_full_resync_required", 429: "graph_throttled"}.get(exc.code, "graph_read_failed")
                raise ConnectorError(code) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise ConnectorError("graph_network_failed") from None
            except (ValueError, TypeError) as exc:
                raise ConnectorError("graph_invalid_json") from None
        raise ConnectorError("graph_read_failed")

    def iter_collection(self, path, *, progress=None, max_pages=None, start_page=None):
        progress = progress or SyncProgress()
        initial_url = self._url(path)
        expected_path = urllib.parse.unquote(urllib.parse.urlsplit(initial_url).path)
        url = self._url(start_page) if start_page else initial_url
        seen = set()
        while url:
            if url in seen:
                progress.failure = "pagination_cycle"
                raise ConnectorError("pagination_cycle")
            seen.add(url)
            progress.remaining_page_token = url
            try:
                if urllib.parse.unquote(urllib.parse.urlsplit(self._url(url)).path) != expected_path:
                    raise ConnectorError("graph_nextlink_scope_changed")
                page = self.get_json(url)
                values = page.get("value")
                if not isinstance(values, list):
                    raise ConnectorError("graph_collection_missing")
                next_url = page.get("@odata.nextLink")
                if next_url is not None and not isinstance(next_url,str):
                    raise ConnectorError("graph_page_token_invalid")
                if next_url:
                    self._url(next_url)
                    if urllib.parse.unquote(urllib.parse.urlsplit(next_url).path) != expected_path:
                        raise ConnectorError("graph_nextlink_scope_changed")
            except ConnectorError as exc:
                progress.failure = exc.code
                raise
            progress.pages += 1
            for value in values:
                yield value
                progress.items += 1
            url = next_url
            progress.remaining_page_token = url
            if max_pages is not None and progress.pages >= max_pages and url:
                progress.limitations.append("page_cap_reached")
                return
        progress.complete = True

    def list_user_chats(self, professor_id, *, progress=None, max_pages=None, start_page=None):
        """Enumerate the verified user's chats without expanding member lists."""
        user = urllib.parse.quote(_required_id(professor_id), safe="")
        yield from self.iter_collection(f"/users/{user}/chats?$top=50", progress=progress,
                                        max_pages=max_pages, start_page=start_page)

    def iter_chat_messages(self, chat_id, *, progress=None, max_pages=None, start_page=None):
        chat = urllib.parse.quote(_required_id(chat_id), safe="")
        yield from self.iter_collection(f"/chats/{chat}/messages?$top=50", progress=progress,
                                        max_pages=max_pages, start_page=start_page)

    def iter_channel_messages(self, team_id, channel_id, *, progress=None, max_pages=None):
        progress = progress or SyncProgress()
        team = urllib.parse.quote(_required_id(team_id), safe="")
        channel = urllib.parse.quote(_required_id(channel_id), safe="")
        path = f"/teams/{team}/channels/{channel}/messages"
        roots_progress = SyncProgress()
        reply_progress = None
        try:
            for root in self.iter_collection(path + "?$top=50&$expand=replies", progress=roots_progress, max_pages=max_pages):
                root_id = _required_id(root.get("id"))
                replies_path = path + "/" + urllib.parse.quote(root_id, safe="") + "/replies"
                yield root
                progress.items += 1
                if "replies" in root:
                    replies = root["replies"]
                    if not isinstance(replies, list):
                        raise ConnectorError("graph_replies_invalid")
                    for reply in replies:
                        yield {**reply, "_parent_id": root_id}
                        progress.items += 1
                    next_reply = root.get("replies@odata.nextLink")
                else:
                    next_reply = self.BASE + replies_path + "?$top=50"
                if next_reply:
                    if urllib.parse.unquote(urllib.parse.urlsplit(self._url(next_reply)).path) != urllib.parse.unquote("/v1.0" + replies_path):
                        raise ConnectorError("graph_reply_nextlink_scope_changed")
                    reply_progress = SyncProgress()
                    progress.pending_ids = [root_id]
                    progress.remaining_page_token = next_reply
                    for reply in self.iter_collection(next_reply, progress=reply_progress):
                        progress.remaining_page_token = reply_progress.remaining_page_token
                        yield {**reply, "_parent_id": root_id}
                        progress.items += 1
                    progress.pages += reply_progress.pages
                    progress.pending_ids = []
                    reply_progress = None
            progress.pages += roots_progress.pages
            progress.complete = roots_progress.complete
            progress.remaining_page_token = roots_progress.remaining_page_token
            progress.limitations.extend(roots_progress.limitations)
        except ConnectorError as exc:
            progress.failure = exc.code
            progress.remaining_page_token = reply_progress.remaining_page_token if reply_progress else roots_progress.remaining_page_token
            raise
