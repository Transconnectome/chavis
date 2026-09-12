"""Bounded, read-only observation of public messages in local Codex JSONL logs.

The cursor contains offsets and small metadata only, never conversation text.
Unknown record types (including tools and reasoning) are ignored.  This is a
sample of locally available conversations, not evidence that a test was run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any


MAX_LINE_BYTES = 256 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES_PER_PASS = 120
MAX_FINGERPRINTS_PER_SESSION = 128
INTERNAL_MARKER = "CODEX_COACH_INTERNAL"


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def redact(text: str) -> str:
    """Conservative credential/email scrubbing; not a comprehensive DLP system."""
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)(\b(?:[A-Za-z0-9]+[_-])*(?:api[_-]?key|access[_-]?token|auth[_-]?token|bot[_-]?token|secret|password)\b[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+",
        r"\1[REDACTED]", text,
    )
    text = re.sub(r"\b(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{8,}", "[REDACTED_TOKEN]", text)
    text = re.sub(r"\b(?:AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|hf_[A-Za-z0-9]{20,}|[sr]k_(?:live|test)_[A-Za-z0-9]{16,})\b", "[REDACTED_TOKEN]", text)
    text = re.sub(r"\b\d{7,12}:[A-Za-z0-9_-]{25,}\b", "[REDACTED_TOKEN]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED_TOKEN]", text)
    text = re.sub(r"(?<![\w.+-])[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", "[REDACTED_EMAIL]", text)
    text = re.sub(r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{48,}(?![A-Za-z0-9])", "[REDACTED_LONG_TOKEN]", text)
    return text


def _public_text(record: dict) -> tuple[str, str, str] | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    kind = record.get("type")
    phase = payload.get("phase") or payload.get("channel")
    if phase not in (None, "commentary", "final", "final_answer"):
        return None
    if kind == "event_msg" and payload.get("type") in ("user_message", "agent_message"):
        role = "user" if payload["type"] == "user_message" else "assistant"
        text = payload.get("message")
        return ("event", role, text) if isinstance(text, str) else None
    if kind != "response_item" or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    content = payload.get("content", [])
    if not isinstance(content, list):
        return None
    parts = [part["text"] for part in content if isinstance(part, dict)
             and part.get("type") in ("input_text", "output_text")
             and isinstance(part.get("text"), str)]
    return "response", role, "\n".join(parts)


def _strip_injected(text: str) -> str:
    for tag in ("recommended_plugins", "environment_context", "INSTRUCTIONS", "skills_instructions"):
        text = re.sub(r"<" + tag + r"\b[^>]*>.*?</" + tag + r">", "", text, flags=re.S | re.I)
    text = re.sub(r"(?im)^\s*#\s*AGENTS\.md instructions\s*$", "", text)
    # An incomplete injected block is not a user request either.
    if re.match(r"\s*<(?:recommended_plugins|environment_context|INSTRUCTIONS|skills_instructions)\b", text, re.I):
        return ""
    return text.strip()


def _metadata(payload: dict, fallback_id: str) -> dict:
    source = payload.get("source")
    thread_source = payload.get("thread_source")
    subagent = any(
        (isinstance(value, dict) and "subagent" in value)
        or (isinstance(value, str) and value.lower().startswith("subagent"))
        for value in (source, thread_source)
    )
    return {
        "session_id": str(payload.get("session_id") or payload.get("id") or fallback_id),
        "cwd": str(payload.get("cwd") or ""),
        "created_at": payload.get("timestamp"),
        "forked": bool(payload.get("forked_from_id") or payload.get("parent_thread_id")),
        "excluded": "subagent" if subagent else ("ephemeral" if payload.get("ephemeral") is True else ""),
        "metadata_seen": True,
    }


def _read_batch(path: Path, offset: int, discarding: bool) -> tuple[list, int, bool, dict]:
    """Read complete bounded records; retry a normally sized partial last line.

    Oversized records are explicitly excluded. Their continuation is drained in
    bounded chunks across calls, so one huge tool output cannot stall a session.
    """
    records = []
    stats = {"bytes_read": 0, "malformed_lines": 0, "oversized_lines": 0, "partial_lines": 0}
    with path.open("rb") as handle:
        handle.seek(offset)
        while stats["bytes_read"] < MAX_FILE_BYTES:
            start = handle.tell()
            remaining = MAX_FILE_BYTES - stats["bytes_read"]
            line = handle.readline(min(MAX_LINE_BYTES + 1, remaining))
            stats["bytes_read"] += len(line)
            if not line:
                break
            end = handle.tell()
            complete = line.endswith(b"\n")
            if discarding:
                records.append((end, None, not complete))
                discarding = not complete
                continue
            if len(line) > MAX_LINE_BYTES:
                stats["oversized_lines"] += 1
                discarding = not complete
                records.append((end, None, discarding))
                continue
            if not complete:
                # A read-budget boundary and an actively appended line both
                # resume from this record's beginning on the next pass.
                stats["partial_lines"] += 1
                handle.seek(start)
                break
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("record is not an object")
            except (ValueError, UnicodeDecodeError):
                stats["malformed_lines"] += 1
                record = None
            records.append((end, record, False))
        return records, handle.tell(), discarding, stats


def collect_recent(
    roots: list[Path], state: dict, *, now: datetime, lookback_hours: float = 2,
    max_sessions: int = 6, max_messages: int = 24, max_chars: int = 18000,
) -> dict:
    """Return public conversation samples and a persistable incremental cursor.

    Pass the previous result as ``state`` or persist its ``cursor`` under that
    key. Message/session/character caps are global; deferred records and files
    keep their old offsets. A genuine user turn establishes eligibility even
    when a long-running task outlasts the lookback window; only returned message
    timestamps must be recent. Fork history predating fork creation is excluded.
    Bounded message fingerprints suppress exact copies of the same session
    across local roots. Bounds and exclusions are reported under ``coverage``.
    """
    if min(max_sessions, max_messages, max_chars) < 0 or lookback_hours < 0:
        raise ValueError("sampling limits must be nonnegative")
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    cutoff = now - timedelta(hours=lookback_hours)
    original = state.get("cursor", {})
    cursor = {key: dict(value) for key, value in original.items() if isinstance(value, dict)} if isinstance(original, dict) else {}
    seen_by_session: dict[str, list[str]] = {}
    for entry in cursor.values():
        session_id = entry.get("session_id")
        if not isinstance(session_id, str):
            continue
        fingerprints = entry.get("message_fingerprints", [])
        if isinstance(fingerprints, list):
            merged = seen_by_session.get(session_id, []) + [item for item in fingerprints if isinstance(item, str)]
            seen_by_session[session_id] = list(dict.fromkeys(merged))[-MAX_FINGERPRINTS_PER_SESSION:]
    coverage = {
        "scope": "local Codex JSONL files only", "local_files_only": True,
        "sampling": True, "lookback_hours": lookback_hours,
        "roots": [str(Path(root).expanduser()) for root in roots],
        "missing_roots": [], "candidate_files": 0, "scanned_files": 0,
        "deferred_files": 0, "excluded_files": 0, "read_errors": 0,
        "bytes_read": 0, "malformed_lines": 0, "oversized_lines": 0,
        "partial_lines": 0, "backlog_files": 0, "truncated_messages": 0,
        "duplicate_messages": 0,
        "limits": {"sessions": max_sessions, "messages": max_messages, "chars": max_chars,
                   "bytes_per_file": MAX_FILE_BYTES, "bytes_per_line": MAX_LINE_BYTES,
                   "files_per_pass": MAX_FILES_PER_PASS},
        "limitations": ["Other hosts, unsaved and unavailable sessions are not observable.",
                        "Conversation text does not verify artifacts or tool results.",
                        "Fork history with rewritten timestamps and no provenance cannot be identified reliably.",
                        "Cross-file deduplication retains only the latest 128 message fingerprints per session.",
                        "Credential redaction is conservative and cannot identify every secret."],
    }
    candidates = {}
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            coverage["missing_roots"].append(str(root))
            continue
        try:
            for path in root.rglob("*.jsonl"):
                if path.is_symlink():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    coverage["read_errors"] += 1
                    continue
                if stat.st_mtime >= cutoff.timestamp():
                    candidates[str(path.absolute())] = stat
        except OSError:
            coverage["read_errors"] += 1
    coverage["candidate_files"] = len(candidates)
    sessions = []
    used_messages = used_chars = 0
    for path_string, stat in sorted(candidates.items(), key=lambda item: item[1].st_mtime, reverse=True):
        if (len(sessions) >= max_sessions or used_messages >= max_messages or used_chars >= max_chars
                or coverage["scanned_files"] >= MAX_FILES_PER_PASS):
            coverage["deferred_files"] += 1
            continue
        previous = cursor.get(path_string, {})
        if previous.get("inode") != stat.st_ino or previous.get("offset", 0) > stat.st_size:
            previous = {}
        current = dict(previous)
        current.update(inode=stat.st_ino, size=stat.st_size, mtime_ns=stat.st_mtime_ns)
        offset = int(current.get("offset", 0))
        if current.get("excluded"):
            coverage["excluded_files"] += 1
            continue
        if offset == stat.st_size:
            continue
        coverage["scanned_files"] += 1
        try:
            records, _, _, read_stats = _read_batch(Path(path_string), offset, bool(current.get("discarding_line")))
        except OSError:
            coverage["read_errors"] += 1
            continue
        for name, count in read_stats.items():
            coverage[name] += count
        # Lock one mirror stream for this file; legacy event records take
        # priority when available in the first bounded batch.
        if not current.get("stream"):
            streams = {_public_text(record)[0] for _, record, _ in records
                       if record is not None and _public_text(record) is not None}
            if streams:
                current["stream"] = "event" if "event" in streams else "response"
        messages = []
        metrics = {"new_user_messages": 0, "assistant_messages": 0, "truncated_messages": 0}
        for end, record, discarding in records:
            if record is not None and record.get("type") == "session_meta":
                payload = record.get("payload")
                if isinstance(payload, dict):
                    current.update(_metadata(payload, Path(path_string).stem))
                current["offset"] = end
                current["discarding_line"] = discarding
                if current.get("excluded"):
                    messages.clear()
                    coverage["excluded_files"] += 1
                    break
                continue
            public = _public_text(record) if record is not None else None
            if public and current.get("metadata_seen"):
                stream, role, text = public
                text = _strip_injected(text) if role == "user" else text.strip()
                if role == "user" and text.startswith(INTERNAL_MARKER):
                    current["excluded"] = "coach_internal"
                    current["offset"] = end
                    messages.clear()
                    coverage["excluded_files"] += 1
                    break
                timestamp = _time(record.get("timestamp"))
                created = _time(current.get("created_at"))
                genuine = bool(text and stream == current.get("stream") and timestamp
                               and timestamp <= now + timedelta(minutes=5)
                               and not (current.get("forked") and created and timestamp < created))
                if genuine and role == "user" and timestamp < cutoff:
                    # Establish task ownership without returning old text.
                    # Assistant activity may continue hours after this turn.
                    current["last_user_timestamp"] = timestamp.isoformat()
                if genuine and timestamp >= cutoff:
                    if role == "user":
                        last_user = timestamp
                    else:
                        last_user = _time(current.get("last_user_timestamp"))
                    if last_user is not None:
                        session_id = current["session_id"]
                        fingerprints = seen_by_session.setdefault(session_id, [])
                        material = "\0".join((role, timestamp.astimezone(timezone.utc).isoformat(), text))
                        fingerprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
                        if fingerprint in fingerprints:
                            coverage["duplicate_messages"] += 1
                            current["message_fingerprints"] = list(fingerprints)
                            if role == "user":
                                current["last_user_timestamp"] = timestamp.isoformat()
                            current["offset"] = end
                            current["discarding_line"] = discarding
                            continue
                        if used_messages >= max_messages or used_chars >= max_chars:
                            break
                        clean = redact(text)
                        allowance = max_chars - used_chars
                        if len(clean) > allowance:
                            clean = clean[:allowance]
                            metrics["truncated_messages"] += 1
                            coverage["truncated_messages"] += 1
                        messages.append({"role": role, "text": clean, "timestamp": timestamp.isoformat()})
                        used_messages += 1
                        used_chars += len(clean)
                        metrics["new_user_messages" if role == "user" else "assistant_messages"] += 1
                        fingerprints = (fingerprints + [fingerprint])[-MAX_FINGERPRINTS_PER_SESSION:]
                        seen_by_session[session_id] = fingerprints
                        current["message_fingerprints"] = fingerprints
                        if role == "user":
                            current["last_user_timestamp"] = timestamp.isoformat()
            current["offset"] = end
            current["discarding_line"] = discarding
        cursor[path_string] = current
        if current.get("offset", offset) < stat.st_size and not current.get("excluded"):
            coverage["backlog_files"] += 1
        if messages:
            sessions.append({"session_id": current["session_id"], "cwd": current.get("cwd", ""),
                             "source_file": path_string, "messages": messages, "metrics": metrics})
    coverage["returned_sessions"] = len(sessions)
    coverage["returned_messages"] = sum(len(session["messages"]) for session in sessions)
    coverage["returned_chars"] = sum(len(message["text"]) for session in sessions for message in session["messages"])
    return {"sessions": sessions, "cursor": cursor, "coverage": coverage}
