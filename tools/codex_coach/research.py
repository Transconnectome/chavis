"""Bounded primary-source refresh for Codex Coach (standard library only).

Fetching a source verifies availability, not its claims or local effectiveness.
This module never changes coaching policy and never executes fetched content.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET


MAX_BYTES = 2 * 1024 * 1024
MAX_EXCERPT = 6000
MAX_SOURCES = 6
MAX_RECENT_PAPERS = 2
MAX_CANDIDATES = 5
TIMEOUT_SECONDS = 12
SOURCE_FILE = Path(__file__).with_name("sources.json")
ALLOWED_HOSTS = frozenset({
    "learn.chatgpt.com", "developers.openai.com", "platform.openai.com",
    "arxiv.org", "export.arxiv.org", "rss.arxiv.org", "metr.org",
})
ATOM = {"atom": "http://www.w3.org/2005/Atom"}
ARXIV_QUERY = (
    '(ti:agent OR ti:agents OR ti:agentic) AND '
    '(ti:workflow OR ti:evaluation OR ti:context OR '
    'abs:"human-AI collaboration" OR abs:"coding agents")'
)
RSS_URL = "https://rss.arxiv.org/rss/cs.AI"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("source URL is outside the fixed HTTPS host allowlist")


class _SafeRedirect(HTTPRedirectHandler):
    max_redirections = 4
    max_repeats = 2

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> Request | None:
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str) -> tuple[str, str, str]:
    """Return decoded body, content type, final URL; cap transfer and redirects."""
    _validate_url(url)
    request = Request(url, headers={
        "User-Agent": "Chavis-Codex-Coach/0.1 (primary-source evidence refresh)",
        "Accept": "text/markdown,text/html,application/atom+xml,text/plain;q=0.9",
        "Accept-Encoding": "identity",
    })
    started = time.monotonic()
    with build_opener(_SafeRedirect()).open(request, timeout=TIMEOUT_SECONDS) as response:
        _validate_url(response.geturl())
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ValueError("unexpected compressed response")
        length = response.headers.get("Content-Length")
        if length and int(length) > MAX_BYTES:
            raise ValueError("response exceeds 2 MiB limit")
        body = bytearray()
        while len(body) <= MAX_BYTES:
            if time.monotonic() - started > TIMEOUT_SECONDS:
                raise TimeoutError("source fetch exceeded time budget")
            chunk = response.read(min(65536, MAX_BYTES + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise ValueError("response exceeds 2 MiB limit")
        content_type = response.headers.get_content_type()
        if content_type not in {
            "text/plain", "text/markdown", "text/html", "application/xhtml+xml",
            "application/atom+xml", "application/rss+xml", "application/xml", "text/xml",
        }:
            raise ValueError("unexpected non-text content type")
        charset = response.headers.get_content_charset() or "utf-8"
        return bytes(body).decode(charset, errors="replace"), content_type, response.geturl()


class _ArticleText(HTMLParser):
    """Extract readable text, preferring main/article over navigation chrome."""

    ignored = {"script", "style", "nav", "footer", "header", "svg", "noscript", "form", "button"}
    void = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    blocks = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "br", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.all_text: list[str] = []
        self.article_text: list[str] = []

    def _append(self, data: str) -> None:
        if any(tag in self.ignored for tag in self.stack):
            return
        self.all_text.append(data)
        if "main" in self.stack or "article" in self.stack:
            self.article_text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.void:
            self.stack.append(tag)
        if tag in self.blocks:
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.blocks:
            self._append("\n")
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]

    def handle_data(self, data: str) -> None:
        self._append(data)

    def text(self) -> str:
        return "".join(self.article_text or self.all_text)


def _clean(body: str, content_type: str) -> str:
    if "html" in content_type:
        parser = _ArticleText()
        parser.feed(body)
        body = parser.text()
    lines = [re.sub(r"[\t \xa0]+", " ", line).strip() for line in body.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _excerpt_details(content: str, source: dict[str, Any]) -> dict[str, Any]:
    """Keep bounded excerpts from relevant sections, without generated summaries."""
    cues = source.get("excerpt_sections", [])[:3]
    if len(content) <= MAX_EXCERPT:
        return {"excerpt": content, "excerpt_scope": "complete_extracted_text",
                "excerpt_truncated": False, "excerpt_sections_used": ["complete"]}
    pieces = [content[:1200]]
    used = ["opening"]
    if cues:
        patterns = [re.escape(cue) for cue in cues]
    else:
        patterns = [
            r"(?:Methods?|Methodology|Experimental Setup|Experimental Design|Study Design|Evaluation Setup)",
            r"(?:Results?|Experiments?|Empirical Evaluation)",
            r"(?:Limitations?(?: and Future Work)?|Discussion(?: and Limitations)?|Conclusion(?:s)?)",
        ]
    for pattern in patterns:
        # Heading-length lines only; avoid selecting words inside paragraphs.
        match = re.search(r"(?im)^\s*(?:#+\s*|\d[\d.]*\s*)?" + pattern + r"[^\n]{0,100}$", content)
        if match:
            pieces.append(content[match.start():match.start() + 1500])
            used.append(match.group(0).strip())
    if len(pieces) == 1:
        return {"excerpt": content[:MAX_EXCERPT], "excerpt_scope": "opening_only_sections_not_found",
                "excerpt_truncated": True, "excerpt_sections_used": ["opening only"]}
    return {
        "excerpt": "\n\n[... separate source excerpt ...]\n\n".join(pieces)[:MAX_EXCERPT],
        "excerpt_scope": "selected_noncontiguous_sections",
        "excerpt_truncated": True,
        "excerpt_sections_used": used,
    }


def _error(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error)[:220]}"


def _refresh_source(source: dict[str, Any], old: dict[str, Any], stamp: str) -> dict[str, Any]:
    record = {**old, **{key: value for key, value in source.items() if key != "fetch_urls"}}
    record.update({"checked_at": stamp, "changed": False, "error": None})
    try:
        attempts = source.get("fetch_urls", [source["url"]])[:2]
        for index, url in enumerate(attempts):
            try:
                body, content_type, fetched_url = _fetch(url)
                break
            except HTTPError as error:
                # A published Markdown representation may not exist yet.
                if error.code not in (404, 406) or index + 1 == len(attempts):
                    raise
        content = _clean(body, content_type)
        if len(content) < 150:
            raise ValueError("source body is too short to provide evidence")
        digest = sha256(content.encode("utf-8")).hexdigest()
        old_digest = old.get("content_hash")
        needs_review = bool(old.get("needs_review") or not source.get("reviewed_at")
                            or (old_digest and digest != old_digest))
        record.update({
            "retrieved_at": stamp,
            "status": "current",
            "content_hash": digest,
            **_excerpt_details(content, source),
            "fetch_url": fetched_url,
            "changed": bool(not old_digest or digest != old_digest or old.get("learning_pending")),
            "content_changed": bool(old_digest and digest != old_digest),
            "new": not bool(old_digest),
            "needs_review": needs_review,
            "note_status": ("primary_text_fetched_review_pending" if not source.get("reviewed_at") else
                            "source_changed_recheck_notes" if needs_review else "initial_source_review"),
        })
    except Exception as error:
        record.update({
            "status": "stale" if old.get("excerpt") else "unavailable",
            "retrieved_at": old.get("retrieved_at"),
            "content_hash": old.get("content_hash"),
            "excerpt": old.get("excerpt", "")[:MAX_EXCERPT],
            "error": _error(error),
            "new": False,
            "content_changed": False,
            "note_status": "fetch_failed_no_new_verification",
        })
    return record


def _discover_atom(stamp: str) -> list[dict[str, Any]]:
    query = urlencode({
        "search_query": ARXIV_QUERY, "start": 0, "max_results": MAX_CANDIDATES,
        "sortBy": "lastUpdatedDate", "sortOrder": "descending",
    })
    body, _, _ = _fetch("https://export.arxiv.org/api/query?" + query)
    if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
        raise ValueError("unexpected XML entity declaration")
    root = ET.fromstring(body)
    if root.tag != "{http://www.w3.org/2005/Atom}feed":
        raise ValueError("arXiv returned a non-Atom feed")
    candidates = []
    for entry in root.findall("atom:entry", ATOM)[:MAX_CANDIDATES]:
        identifier = entry.findtext("atom:id", "", ATOM).rstrip("/").rsplit("/", 1)[-1]
        if not re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", identifier):
            continue
        candidates.append({
            "id": identifier,
            "url": "https://arxiv.org/abs/" + identifier,
            "title": " ".join(entry.findtext("atom:title", "", ATOM).split())[:500],
            "abstract": " ".join(entry.findtext("atom:summary", "", ATOM).split())[:4000],
            "published_at": entry.findtext("atom:published", "", ATOM),
            "updated_at": entry.findtext("atom:updated", "", ATOM),
            "retrieved_at": stamp,
            "status": "candidate",
            "verification": "title_and_abstract_only",
            "discovery_method": "arxiv_atom",
        })
    if root.findall("atom:entry", ATOM) and not candidates:
        raise ValueError("arXiv returned entries without valid paper identifiers")
    return candidates


def _paper_id(url: str) -> str | None:
    """Read identifiers from metadata; never fetch a URL supplied by a feed."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != "arxiv.org":
        return None
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        return None
    match = re.fullmatch(r"/abs/(\d{4}\.\d{4,5}(?:v\d+)?)", parsed.path.rstrip("/"))
    return match.group(1) if match else None


def _discover_rss(stamp: str, reason: str) -> list[dict[str, Any]]:
    body, _, _ = _fetch(RSS_URL)
    if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
        raise ValueError("unexpected XML entity declaration")
    root = ET.fromstring(body)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("arXiv returned a non-RSS feed")
    candidates = []
    seen = set()
    for item in root.findall("./channel/item"):
        identifier = _paper_id(item.findtext("link", ""))
        if not identifier:
            continue
        description = _clean(item.findtext("description", ""), "text/html")
        base_id = re.sub(r"v\d+$", "", identifier)
        # A version explicitly announced in the primary feed is usable evidence;
        # do not infer v1 from an unversioned URL or its publication date.
        announced = re.search(r"\barXiv:" + re.escape(base_id) + r"(v\d+)\b", description)
        if announced:
            identifier = base_id + announced.group(1)
        if identifier in seen:
            continue
        seen.add(identifier)
        date_text = item.findtext("pubDate", "")
        try:
            announced_at = _iso(parsedate_to_datetime(date_text)) if date_text else None
        except (TypeError, ValueError):
            announced_at = None
        candidate = {
            "id": identifier,
            "url": "https://arxiv.org/abs/" + identifier,
            "title": " ".join(item.findtext("title", "").split())[:500],
            "abstract": re.sub(r"^.*?Abstract:\s*", "", description, count=1, flags=re.S)[:4000],
            "announced_at": announced_at,
            "published_at": None,
            "updated_at": None,
            "retrieved_at": stamp,
            "status": "candidate",
            "verification": "title_and_abstract_only",
            "version_evidence": "primary_rss_announcement" if announced else "unresolved",
            "discovery_method": "arxiv_rss_fallback",
            "discovery_url": RSS_URL,
            "fallback_reason": reason,
        }
        if _relevant_candidate(candidate, require_version=False):
            candidates.append(candidate)
    # RSS is a recent-announcement feed, not the API's global lastUpdatedDate
    # search. Its timestamp must not be presented as a paper revision timestamp.
    candidates.sort(key=lambda candidate: candidate.get("announced_at") or "", reverse=True)
    return candidates[:MAX_CANDIDATES]


def _discover(stamp: str) -> list[dict[str, Any]]:
    try:
        return _discover_atom(stamp)
    except Exception as error:
        # One different primary endpoint, never an API retry loop. An RSS failure
        # is returned to the caller, which preserves prior discovery evidence.
        try:
            return _discover_rss(stamp, _error(error))
        except Exception as fallback_error:
            raise RuntimeError("Atom failed: " + _error(error) + "; RSS failed: " + _error(fallback_error)) from fallback_error


def _relevant_candidate(candidate: dict[str, Any], *, require_version: bool = True) -> bool:
    """Conservative relevance gate; no claim validation is inferred from this."""
    title = candidate.get("title", "").lower()
    text = title + " " + candidate.get("abstract", "").lower()
    if re.search(r"\b(swarm|robotics?|uavs?|wireless|protein|molecular|drug)\b", title):
        return False
    agent = re.search(r"\b(agents?|agentic)\b", text)
    work = re.search(r"coding|software|developer|repository|human.ai collaboration|human.ai oversight|tool.use|tool interaction|reusable skills", text)
    topic = re.search(r"prompt|context|workflow|evaluat|benchmark|collaborat|oversight|productivity|instruction|planning", title)
    identifier_pattern = r"\d{4}\.\d{4,5}v\d+" if require_version else r"\d{4}\.\d{4,5}(?:v\d+)?"
    return bool(agent and work and topic and re.fullmatch(identifier_pattern, candidate.get("id", "")))


def _resolve_version(candidate: dict[str, Any]) -> dict[str, Any]:
    """Confirm an unversioned candidate against its bounded canonical record."""
    candidate = dict(candidate)
    identifier = candidate.get("id", "")
    if re.fullmatch(r"\d{4}\.\d{4,5}v\d+", identifier):
        return candidate
    if not re.fullmatch(r"\d{4}\.\d{4,5}", identifier):
        return candidate
    try:
        body, content_type, _ = _fetch("https://arxiv.org/abs/" + identifier)
        text = _clean(body, content_type)
        # The canonical record contains a version-qualified self-identifier.
        # Constrain the match to this exact paper; references cannot substitute.
        versions = re.findall(r"\barXiv:" + re.escape(identifier) + r"(v\d+)\b", text)
        if not versions:
            raise ValueError("canonical record has no version-qualified self-identifier")
        version = max(versions, key=lambda value: int(value[1:]))
        candidate.update({"id": identifier + version,
                          "url": "https://arxiv.org/abs/" + identifier + version,
                          "version_evidence": "canonical_arxiv_record"})
    except Exception as error:
        candidate["version_error"] = _error(error)
        candidate["version_evidence"] = "unresolved"
    return candidate


def _recent_source(candidate: dict[str, Any]) -> dict[str, Any]:
    identifier = candidate["id"]
    return {
        "id": "arxiv_" + identifier,
        "url": "https://arxiv.org/html/" + identifier,
        "title": candidate["title"],
        "version": "arXiv:" + identifier,
        "published_at": candidate.get("published_at"),
        "updated_at": candidate.get("updated_at"),
        "announced_at": candidate.get("announced_at"),
        "discovery_method": candidate.get("discovery_method", "arxiv_atom"),
        "source_type": "research_preprint",
        "origin": "arxiv_discovery_fulltext",
        "local_status": "not_tested",
        "verification": "primary_text_fetched_not_adopted",
    }


def refresh(previous: dict, *, now: datetime) -> dict:
    """Fetch six fixed sources, five arXiv candidates, and two recent full texts.

    ``checked_at`` is an attempt timestamp. Only successful requests advance a
    source's ``retrieved_at``. The caller persists the returned JSON and decides
    whether an LLM synthesis is needed. Excerpts and abstracts are untrusted data.
    """
    stamp = _iso(now)
    previous = previous if isinstance(previous, dict) else {}
    registry = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    sources = registry["sources"][:MAX_SOURCES]
    old_sources = {item.get("id"): item for item in previous.get("sources", []) if isinstance(item, dict)}
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(
            lambda source: _refresh_source(source, old_sources.get(source["id"], {}), stamp), sources,
        ))
    errors = [{"source_id": row["id"], "error": row["error"]} for row in results if row["error"]]
    discovery = {"checked_at": stamp, "retrieved_at": stamp, "status": "current", "query": ARXIV_QUERY}
    try:
        candidates = _discover(stamp)
        if candidates:
            discovery["method"] = candidates[0].get("discovery_method", "arxiv_atom")
            if candidates[0].get("fallback_reason"):
                discovery["fallback_reason"] = candidates[0]["fallback_reason"]
                discovery["fallback_url"] = RSS_URL
                discovery["scope"] = "recent cs.AI announcements filtered locally; not global lastUpdatedDate search"
    except Exception as error:
        candidates = []
        for old in previous.get("candidates", [])[:MAX_CANDIDATES]:
            if isinstance(old, dict):
                candidates.append({**old, "status": "candidate", "stale": True})
        discovery.update({
            "status": "stale" if candidates else "unavailable",
            "retrieved_at": previous.get("discovery", {}).get("retrieved_at"),
            "error": _error(error),
        })
        errors.append({"source_id": "arxiv_discovery", "error": _error(error)})
    known_urls = {source["url"] for source in sources}
    recent_sources = []
    previous_recent = []
    for old in old_sources.values():
        if (old.get("origin") == "arxiv_discovery_fulltext"
                and re.fullmatch(r"https://arxiv\.org/html/\d{4}\.\d{4,5}v\d+", old.get("url", ""))):
            previous_recent.append(old)
    # A failed synthesis must not be evicted by tomorrow's discoveries.
    for old in previous_recent:
        if old.get("learning_pending") and len(recent_sources) < MAX_RECENT_PAPERS:
            recent_sources.append(old)
            known_urls.add(old["url"])
    resolutions = 0
    for index, candidate in enumerate(candidates):
        if len(recent_sources) >= MAX_RECENT_PAPERS:
            break
        if (_relevant_candidate(candidate, require_version=False)
                and not _relevant_candidate(candidate)
                and resolutions < MAX_RECENT_PAPERS):
            candidate = _resolve_version(candidate)
            candidates[index] = candidate
            resolutions += 1
        if _relevant_candidate(candidate):
            source = _recent_source(candidate)
            if source["url"] not in known_urls:
                recent_sources.append(source)
                known_urls.add(source["url"])
        if len(recent_sources) == MAX_RECENT_PAPERS:
            break
    # Retain pending learning/recent source continuity when discovery fails or
    # produces no new relevant full-text candidate. Do not reset success dates.
    for old in previous_recent:
        if len(recent_sources) >= MAX_RECENT_PAPERS:
            break
        if old.get("url") in known_urls:
            continue
        recent_sources.append({key: old[key] for key in (
            "id", "url", "title", "version", "published_at", "updated_at", "announced_at", "discovery_method", "source_type", "origin",
            "local_status", "verification",
        ) if key in old})
        known_urls.add(old["url"])
    if recent_sources:
        with ThreadPoolExecutor(max_workers=MAX_RECENT_PAPERS) as executor:
            recent_results = list(executor.map(
                lambda source: _refresh_source(source, old_sources.get(source["id"], {}), stamp), recent_sources,
            ))
        results.extend(recent_results)
        errors.extend({"source_id": row["id"], "error": row["error"]} for row in recent_results if row["error"])
        by_url = {row["url"]: row for row in recent_results}
        for candidate in candidates:
            record = by_url.get("https://arxiv.org/html/" + candidate.get("id", ""))
            if record:
                candidate["fulltext_status"] = record["status"]
                candidate["fulltext_source_id"] = record["id"]
    return {
        "checked_at": stamp,
        "sources": results,
        "candidates": candidates,
        "discovery": discovery,
        "errors": errors,
        "coverage": "six curated sources, up to five arXiv candidates and two relevant versioned full texts; not exhaustive",
        "local_effectiveness": "not_tested",
    }
