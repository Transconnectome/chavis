"""Bind prepared tasks to private, immutable input manifests.

Receipts contain hashes and identifiers, never manuscripts or rubric text.
They detect substitutions relative to this preparation, not the identity of a
human supplier or semantic reading. Same-user filesystem tampering is outside
this integrity boundary; a model response cannot create a valid receipt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from .store import canonical, digest

_ID = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECEIPT_BYTES = 512_000


def _manifest(bundle):
    return {
        "contract": "cha-task-input-manifest-v1",
        "task": bundle["task"],
        "request_source_id": bundle["request_source_id"],
        "request_hash": digest(bundle["query"]),
        # Order is meaningful when multiple manuscripts/references are supplied.
        "sources": [{"source_id": s["source_id"], "snapshot_hash": digest(s)}
                    for s in bundle["sources"]],
        "principles": [{"principle_id": p["principle_id"], "snapshot_hash": digest(p)}
                       for p in bundle["principles"]],
    }


def _directory(store, *, create=False):
    path = Path(store.home) / "task_receipts"
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError("private_task_receipt_directory_required")
    return path


def _read(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        if os.fstat(handle.fileno()).st_mode & 0o077:
            raise ValueError("private_task_receipt_file_required")
        raw = handle.read(_MAX_RECEIPT_BYTES + 1)
    if len(raw) > _MAX_RECEIPT_BYTES:
        raise ValueError("task_receipt_too_large")
    return json.loads(raw)


def register_task_bundle(store, bundle):
    """Save the original source set once and return its task receipt identity."""
    manifest = _manifest(bundle)
    receipt_id = digest(manifest)
    receipt = {"schema_version": 1, "receipt_id": receipt_id, "manifest": manifest}
    encoded = canonical(receipt).encode("utf-8")
    if len(encoded) > _MAX_RECEIPT_BYTES:
        raise ValueError("task_receipt_too_large")
    path = _directory(store, create=True) / (receipt_id + ".json")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".task-receipt-",delete=False) as handle:
            temporary=Path(handle.name)
            os.fchmod(handle.fileno(),0o600)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # Atomic publication without replacing another producer's receipt.
            os.link(temporary,path)
        except FileExistsError:
            if _read(path)!=receipt:
                raise ValueError("task_receipt_collision")
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return {**bundle, "task_receipt_id": receipt_id}


def validate_task_receipt(store, bundle):
    """Compare against the original private receipt, not caller-generated hashes."""
    receipt_id = bundle.get("task_receipt_id") if isinstance(bundle, dict) else None
    if not isinstance(receipt_id, str) or not _ID.fullmatch(receipt_id):
        return ["task_receipt_missing_or_invalid"]
    try:
        receipt = _read(_directory(store) / (receipt_id + ".json"))
    except FileNotFoundError:
        return ["task_receipt_not_found"]
    except (OSError, ValueError, TypeError):
        return ["task_receipt_unavailable"]
    try:
        if (set(receipt) != {"schema_version", "receipt_id", "manifest"}
                or receipt["schema_version"] != 1 or receipt["receipt_id"] != receipt_id
                or digest(receipt["manifest"]) != receipt_id):
            return ["task_receipt_invalid"]
        if _manifest(bundle) != receipt["manifest"]:
            return ["task_input_manifest_changed"]
    except (KeyError, TypeError, ValueError):
        return ["task_input_manifest_invalid"]
    return []
