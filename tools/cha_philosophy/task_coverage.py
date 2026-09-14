"""Bounded, complete task-input accounting; no model calls or source writes.

``plan_task_coverage(bundle)`` returns metadata, never a copy of the input text.
Units cover each task source in supplied order using absolute Python/Unicode
character offsets [char_start, char_end), with SHA-256 of the exact UTF-8 span.
The pure planner checks identities; only read/audit check the private receipt.
Unit IDs carry the byte limit so reads can reconstruct the deterministic plan.

The strict coverage JSON shape is also exposed by ``coverage_schema(plan)``::

    {"schema_version": 1, "task_receipt_id": "...", "plan_id": "...",
     "max_unit_bytes": 6000,
     "unit_notes": [{"unit_id": "task-unit:6000:...", "notes": "...",
                     "citations": [{"source_id": "task:...", "char_start": 0,
                                    "char_end": 3, "quote": "..."}]}],
     "synthesis": {"text": "...", "addressed_unit_ids": ["task-unit:..."],
                   "cross_unit_checks": [{"unit_ids": ["...", "..."],
                                          "notes": "..."}],
                   "rubric_source_ids": ["task:rubric:..."]}}

Only citations are optional. Unit IDs and rubric IDs must be exact complete sets
without duplicates. Each cross-unit check names at least two distinct units;
when there is more than one unit, the checks must collectively address all of
them. A single-unit task uses an empty check list. Notes alone are partial work.
No unknown fields or caller-supplied completion flags are accepted.

``input_coverage_accounted`` means these declarations are structurally complete.
It does NOT prove reading, useful notes, correct synthesis, rubric compliance,
entailment, task completion, or professor fidelity. Even fabricated nonsense can
pass; a human/app must still assess the work. All input and note text is inert
data, including embedded commands. Nothing in this module executes that text.

Limits are explicit failures, never truncation: 256..65536 bytes per unit,
8 MiB per source, 32 MiB per task, 256 task sources, 8192 units, 16 KiB per note,
64 KiB synthesis text, and 8 MiB per submitted coverage declaration.
"""
from __future__ import annotations

import hashlib
import re

from .store import canonical, digest
from .task_receipts import validate_task_receipt

DEFAULT_UNIT_BYTES = 6000
MIN_UNIT_BYTES = 256
MAX_UNIT_BYTES = 65_536
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TASK_BYTES = 32 * 1024 * 1024
MAX_TASK_SOURCES = 256
MAX_UNITS = 8192
MAX_NOTE_BYTES = 16_384
MAX_SYNTHESIS_BYTES = 65_536
MAX_COVERAGE_BYTES = 8 * 1024 * 1024
MAX_CITATIONS_PER_UNIT = 64

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_UNIT_ID = re.compile(r"task-unit:([0-9]{3,5}):([0-9a-f]{64})\Z")


class TaskCoverageError(ValueError):
    """Source-free diagnostic; safe to report without raw user/model text."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _limit(value):
    if type(value) is not int or not MIN_UNIT_BYTES <= value <= MAX_UNIT_BYTES:
        raise TaskCoverageError("invalid_coverage_unit_limit")
    return value


def _utf8(value, code):
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise TaskCoverageError(code) from None


def _sources(bundle):
    # Local import avoids an import cycle when task audits add coverage support.
    from .task import INPUT_KINDS, TASKS, _task_source

    if (not isinstance(bundle, dict) or not isinstance(bundle.get("task"), str)
            or bundle["task"] not in TASKS or not isinstance(bundle.get("sources"), list)):
        raise TaskCoverageError("invalid_coverage_bundle")
    receipt = bundle.get("task_receipt_id")
    if not isinstance(receipt, str) or not _HASH.fullmatch(receipt):
        raise TaskCoverageError("task_receipt_missing_or_invalid")
    found, source_ids, total_bytes = [], set(), 0
    for source in bundle["sources"]:
        if (not isinstance(source, dict) or not isinstance(source.get("source_id"), str)
                or not isinstance(source.get("platform"), str)):
            raise TaskCoverageError("invalid_coverage_source")
        sid = source["source_id"]
        if not sid or sid in source_ids:
            raise TaskCoverageError("duplicate_or_invalid_coverage_source_id")
        source_ids.add(sid)
        if source["platform"] != "task":
            if sid.startswith("task:"):
                raise TaskCoverageError("task_source_integrity_failed")
            continue
        kind, content = source.get("kind"), source.get("authored_text")
        if (not isinstance(kind, str) or kind not in INPUT_KINDS | {"request"}
                or not isinstance(content, str) or not content.strip()):
            raise TaskCoverageError("invalid_coverage_source")
        # Check character count first to avoid encoding an obviously huge string.
        if len(content) > MAX_SOURCE_BYTES:
            raise TaskCoverageError("coverage_source_too_large")
        raw = _utf8(content, "invalid_coverage_source_encoding")
        if len(raw) > MAX_SOURCE_BYTES:
            raise TaskCoverageError("coverage_source_too_large")
        total_bytes += len(raw)
        if total_bytes > MAX_TASK_BYTES:
            raise TaskCoverageError("coverage_task_too_large")
        if source != _task_source(kind, content, bundle["task"]):
            raise TaskCoverageError("task_source_integrity_failed")
        found.append((source, raw))
        if len(found) > MAX_TASK_SOURCES:
            raise TaskCoverageError("too_many_coverage_sources")
    requests = [s for s, _ in found if s["kind"] == "request"]
    if (len(requests) != 1 or bundle.get("request_source_id") != requests[0]["source_id"]
            or bundle.get("query") != requests[0]["authored_text"]):
        raise TaskCoverageError("task_request_integrity_failed")
    return found


def plan_task_coverage(bundle, max_unit_bytes=DEFAULT_UNIT_BYTES):
    """Return a deterministic, text-free plan covering ALL task-platform sources.

    Byte boundaries never split a UTF-8 code point. Whitespace, newlines, and
    combining marks are preserved exactly; units need not end at word boundaries.
    The receipt ID is included in every unit identity and in the plan identity.
    """
    limit = _limit(max_unit_bytes)
    sources = _sources(bundle)
    manifest, units = [], []
    for source, raw in sources:
        manifest.append({
            "source_id": source["source_id"], "source_hash": source["source_hash"],
            "kind": source["kind"], "char_length": len(source["authored_text"]),
            "byte_length": len(raw), "content_hash": hashlib.sha256(raw).hexdigest(),
        })
        byte_start, char_start = 0, 0
        while byte_start < len(raw):
            if len(units) >= MAX_UNITS:
                raise TaskCoverageError("too_many_coverage_units")
            byte_end = min(byte_start + limit, len(raw))
            while byte_end < len(raw) and raw[byte_end] & 0xC0 == 0x80:
                byte_end -= 1
            span = raw[byte_start:byte_end]
            char_end = char_start + len(span.decode("utf-8"))
            unit = {
                "source_id": source["source_id"], "source_hash": source["source_hash"],
                "kind": source["kind"], "char_start": char_start, "char_end": char_end,
                "byte_length": len(span), "span_hash": hashlib.sha256(span).hexdigest(),
            }
            identity = digest({"contract": "cha-task-unit-v1",
                               "task_receipt_id": bundle["task_receipt_id"],
                               "max_unit_bytes": limit, **unit})
            units.append({"unit_id": f"task-unit:{limit}:{identity}", **unit})
            byte_start, char_start = byte_end, char_end
    plan = {"schema_version": 1, "contract": "cha-task-coverage-plan-v1",
            "task_receipt_id": bundle["task_receipt_id"], "max_unit_bytes": limit,
            "source_manifest": manifest, "units": units}
    return {**plan, "plan_id": digest(plan), "input_reading_verified": False,
            "task_completion_verified": False}


def read_task_unit(store, bundle, unit_id):
    """Validate the original receipt and plan, then return one exact input span.

    The read returns complete content and its original source identity/offsets.
    It records no reading event and makes no semantic completion assertion.
    """
    if not isinstance(unit_id, str) or not (match := _UNIT_ID.fullmatch(unit_id)):
        raise TaskCoverageError("invalid_coverage_unit_id")
    limit = _limit(int(match.group(1)))
    errors = validate_task_receipt(store, bundle)
    if errors:
        raise TaskCoverageError(errors[0])
    plan = plan_task_coverage(bundle, limit)
    unit = next((u for u in plan["units"] if u["unit_id"] == unit_id), None)
    if unit is None:
        raise TaskCoverageError("unknown_coverage_unit")
    source = next(s for s in bundle["sources"] if s["source_id"] == unit["source_id"])
    content = source["authored_text"][unit["char_start"]:unit["char_end"]]
    return {**unit, "content": content, "task_receipt_id": plan["task_receipt_id"],
            "plan_id": plan["plan_id"], "max_unit_bytes": limit,
            "input_reading_verified": False, "task_completion_verified": False}


def coverage_schema(plan):
    """JSON Schema for declarations; audit additionally checks sets and spans."""
    unit_ids = [u["unit_id"] for u in plan["units"]]
    source_ids = [s["source_id"] for s in plan["source_manifest"]]
    rubric_ids = [s["source_id"] for s in plan["source_manifest"] if s["kind"] == "rubric"]

    def obj(properties, required=None):
        return {"type": "object", "properties": properties,
                "required": list(properties) if required is None else required,
                "additionalProperties": False}

    def ids(allowed, minimum=0, maximum=None):
        return {"type": "array", "uniqueItems": True, "minItems": minimum,
                "maxItems": len(allowed) if maximum is None else maximum,
                "items": {"type": "string", "enum": allowed}}

    note = {"type": "string", "minLength": 1, "maxLength": MAX_NOTE_BYTES}
    citation = obj({"source_id": {"type": "string", "enum": source_ids},
                    "char_start": {"type": "integer", "minimum": 0},
                    "char_end": {"type": "integer", "minimum": 1},
                    "quote": {"type": "string", "minLength": 1,
                              "maxLength": plan["max_unit_bytes"]}})
    schema = obj({
        "schema_version": {"type": "integer", "const": 1},
        "task_receipt_id": {"type": "string", "const": plan["task_receipt_id"]},
        "plan_id": {"type": "string", "const": plan["plan_id"]},
        "max_unit_bytes": {"type": "integer", "const": plan["max_unit_bytes"]},
        "unit_notes": {"type": "array", "minItems": len(unit_ids), "maxItems": len(unit_ids),
                       "items": obj({"unit_id": {"type": "string", "enum": unit_ids},
                                     "notes": note,
                                     "citations": {"type": "array", "maxItems": MAX_CITATIONS_PER_UNIT,
                                                   "items": citation}}, ["unit_id", "notes"])},
        "synthesis": obj({
            "text": {"type": "string", "minLength": 1, "maxLength": MAX_SYNTHESIS_BYTES},
            "addressed_unit_ids": ids(unit_ids, len(unit_ids)),
            "cross_unit_checks": {"type": "array", "minItems": int(len(unit_ids) > 1),
                                  "maxItems": len(unit_ids) if len(unit_ids) > 1 else 0,
                                  "items": obj({"unit_ids": ids(unit_ids, 2), "notes": note})},
            "rubric_source_ids": ids(rubric_ids, len(rubric_ids)),
        }),
    })
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def _text_ok(value, limit=MAX_NOTE_BYTES):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


def _id_set(value, allowed, *, complete=False, minimum=0):
    if (not isinstance(value, list) or len(value) < minimum or len(value) > len(allowed)
            or any(not isinstance(x, str) for x in value)):
        return False
    present = set(value)
    return (len(present) == len(value) and present <= allowed
            and (not complete or present == allowed))


def _result(errors, plan=None, valid_notes=0):
    return {"status": "invalid" if errors else "structural_valid",
            "structural_valid": not errors, "input_coverage_accounted": not errors,
            "accounting_scope": "declared_unit_notes_and_synthesis",
            "input_reading_verified": False, "task_completion_verified": False,
            "entailment_verified": False, "objective_correctness_verified": False,
            "professor_fidelity_measured": False, "semantic_review_required": True,
            "units_required": len(plan["units"]) if plan else None,
            "units_with_valid_notes": valid_notes,
            "errors": sorted(set(errors))}


def _citation_ok(citation, unit, texts):
    if (not isinstance(citation, dict)
            or set(citation) != {"source_id", "char_start", "char_end", "quote"}
            or citation["source_id"] != unit["source_id"]
            or type(citation["char_start"]) is not int
            or type(citation["char_end"]) is not int
            or not _text_ok(citation["quote"], MAX_UNIT_BYTES)):
        return False
    start, end = citation["char_start"], citation["char_end"]
    return (unit["char_start"] <= start < end <= unit["char_end"]
            and texts[unit["source_id"]][start:end] == citation["quote"])


def audit_task_coverage(store, bundle, coverage):
    """Fail closed on incomplete/altered declarations; never certify semantics."""
    errors = validate_task_receipt(store, bundle)
    if errors:
        return _result(errors)
    required = {"schema_version", "task_receipt_id", "plan_id", "max_unit_bytes",
                "unit_notes", "synthesis"}
    if not isinstance(coverage, dict) or set(coverage) != required:
        return _result(["invalid_coverage_fields"])
    if type(coverage["schema_version"]) is not int or coverage["schema_version"] != 1:
        return _result(["invalid_coverage_version"])
    if coverage["task_receipt_id"] != bundle["task_receipt_id"]:
        return _result(["coverage_receipt_mismatch"])
    try:
        # Bound the submitted record independently from complete source content.
        if len(canonical(coverage).encode("utf-8")) > MAX_COVERAGE_BYTES:
            return _result(["coverage_declaration_too_large"])
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        return _result(["invalid_coverage_json"])
    try:
        plan = plan_task_coverage(bundle, coverage["max_unit_bytes"])
    except TaskCoverageError as exc:
        return _result([exc.code])
    if coverage["plan_id"] != plan["plan_id"]:
        return _result(["coverage_plan_mismatch"], plan)
    units = {u["unit_id"]: u for u in plan["units"]}
    texts = {s["source_id"]: s["authored_text"] for s in bundle["sources"]
             if s["platform"] == "task"}
    seen, valid = set(), set()
    records = coverage["unit_notes"]
    if not isinstance(records, list) or len(records) > MAX_UNITS:
        return _result(["invalid_coverage_unit_notes"], plan)
    for record in records:
        if (not isinstance(record, dict) or not {"unit_id", "notes"} <= record.keys()
                or record.keys() - {"unit_id", "notes", "citations"}):
            errors.append("invalid_coverage_unit_note_fields")
            continue
        uid = record["unit_id"]
        if not isinstance(uid, str) or uid not in units:
            errors.append("unknown_coverage_unit")
            continue
        if uid in seen:
            errors.append("duplicate_coverage_unit")
        seen.add(uid)
        if not _text_ok(record["notes"]):
            errors.append("invalid_coverage_unit_notes")
            continue
        citations = record.get("citations", [])
        if (not isinstance(citations, list) or len(citations) > MAX_CITATIONS_PER_UNIT
                or any(not _citation_ok(c, units[uid], texts) for c in citations)):
            errors.append("invalid_coverage_citation")
            continue
        valid.add(uid)
    if seen != set(units) or valid != set(units):
        errors.append("incomplete_coverage_unit_notes")
    synthesis = coverage["synthesis"]
    if (not isinstance(synthesis, dict)
            or set(synthesis) != {"text", "addressed_unit_ids", "cross_unit_checks", "rubric_source_ids"}):
        errors.append("invalid_coverage_synthesis_fields")
        return _result(errors, plan, len(valid))
    if not _text_ok(synthesis["text"], MAX_SYNTHESIS_BYTES):
        errors.append("invalid_coverage_synthesis_text")
    if not _id_set(synthesis["addressed_unit_ids"], set(units), complete=True):
        errors.append("incomplete_coverage_synthesis_units")
    rubrics = {s["source_id"] for s in plan["source_manifest"] if s["kind"] == "rubric"}
    if not _id_set(synthesis["rubric_source_ids"], rubrics, complete=True):
        errors.append("incomplete_coverage_synthesis_rubrics")
    checks, checked = synthesis["cross_unit_checks"], set()
    if (not isinstance(checks, list) or len(checks) > len(units)
            or (len(units) == 1 and checks)):
        errors.append("invalid_coverage_cross_unit_checks")
    else:
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {"unit_ids", "notes"}
                    or not _id_set(check["unit_ids"], set(units), minimum=2)
                    or not _text_ok(check["notes"])):
                errors.append("invalid_coverage_cross_unit_checks")
                continue
            checked.update(check["unit_ids"])
        if len(units) > 1 and checked != set(units):
            errors.append("incomplete_coverage_cross_unit_checks")
    return _result(errors, plan, len(valid))
