"""Opt-in, local full-page PDF OCR in one bounded CPU worker.

This module has no connector/store dependency and performs no downloads. The
caller supplies a PDF and an explicit directory containing eng.traineddata and
kor.traineddata. OCR is an unreviewed transcription, never verified authorship
or a claim that all visual information was recovered. Consumers must sanitize
the returned text as they do every other retrieved document.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import resource
import signal
import stat
import subprocess
import sys
import tempfile


MAX_BYTES = 32 * 1024 * 1024
MAX_PAGES = 256
MAX_PAGE_PIXELS = 12_000_000
TIMEOUT_SECONDS = 300
CPU_SECONDS = 240
MEMORY_BYTES = 2 * 1024 * 1024 * 1024
RENDER_DPI = 200
WORKER_SCHEMA_VERSION = 2
RENDER_DPI_POLICY = "highest_integer_dpi_within_page_pixel_budget"
REDUCED_RESOLUTION_LIMITATION = "render_resolution_reduced_for_pixel_budget_ocr_accuracy_not_verified"
LANGUAGES = ("eng", "kor")
LIMITATIONS = (
    "ocr_transcription_may_contain_errors_or_omit_text",
    "ocr_transcription_not_human_reviewed",
    "reading_order_layout_tables_and_equations_not_verified",
    "figures_images_and_visual_meaning_not_extracted",
    "handwriting_and_languages_other_than_eng_kor_not_verified",
    "empty_pages_may_be_blank_or_unrecognized",
    "rendered_annotations_included_without_authorship_attribution",
    "source_text_requires_consumer_sanitization",
)
SAFE_ERROR_CODES = frozenset({
    "pdf_ocr_input_unavailable", "pdf_ocr_input_invalid", "pdf_ocr_input_budget_exceeded",
    "pdf_ocr_encrypted", "pdf_ocr_page_budget_exceeded", "pdf_ocr_pixel_budget_exceeded",
    "pdf_ocr_text_budget_exceeded", "pdf_ocr_output_budget_exceeded", "pdf_ocr_empty_text",
    "pdf_ocr_tessdata_unavailable", "pdf_ocr_tessdata_invalid", "pdf_ocr_tessdata_changed",
    "pdf_ocr_engine_unavailable", "pdf_ocr_resource_limit_exceeded", "pdf_ocr_timeout",
    "pdf_ocr_worker_failed", "pdf_ocr_worker_terminated", "pdf_ocr_response_invalid",
})


class PdfOcrError(RuntimeError):
    """A fixed safe code, without paths, source text, or native error output."""

    def __init__(self, code: str):
        self.code = code if code in SAFE_ERROR_CODES else "pdf_ocr_worker_failed"
        super().__init__(self.code)


def _read_regular(path: Path, unavailable: str, invalid: str, oversized: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise PdfOcrError(invalid)
            if info.st_size > MAX_BYTES:
                raise PdfOcrError(oversized)
            data = handle.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise PdfOcrError(oversized)
            if not data:
                raise PdfOcrError(invalid)
            return data
    except (OSError, ValueError):
        raise PdfOcrError(unavailable) from None


def _models(tessdata) -> tuple[Path, dict[str, str]]:
    try:
        directory = Path(tessdata).expanduser().absolute()
        if directory.is_symlink() or not directory.is_dir():
            raise PdfOcrError("pdf_ocr_tessdata_unavailable")
        hashes = {}
        for language in LANGUAGES:
            data = _read_regular(directory / (language + ".traineddata"),
                "pdf_ocr_tessdata_unavailable", "pdf_ocr_tessdata_invalid", "pdf_ocr_tessdata_invalid")
            hashes[language] = hashlib.sha256(data).hexdigest()
        return directory, hashes
    except (TypeError, ValueError, OSError):
        raise PdfOcrError("pdf_ocr_tessdata_unavailable") from None


def _join_pages(pages: list[dict]) -> str:
    return "\n\n".join("[Page " + str(page["page_number"]) + "]\n" + page["text"] for page in pages)


def _decode_result(raw: bytes, input_hash: str, models: dict[str, str]) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        result = json.loads(raw, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(result, dict) or type(result.get("schema_version")) is not int
                or result["schema_version"] != WORKER_SCHEMA_VERSION):
            raise ValueError()
        if set(result) == {"schema_version", "error"} and result["error"] in SAFE_ERROR_CODES:
            raise PdfOcrError(result["error"])
        if set(result) != {"schema_version", "input_sha256", "engine_version", "mupdf_version",
                          "model_sha256", "page_count", "pages"}:
            raise ValueError()
        if result["input_sha256"] != input_hash or result["model_sha256"] != models:
            raise ValueError()
        for field in ("engine_version", "mupdf_version"):
            if not isinstance(result[field], str) or not result[field] or len(result[field]) > 64:
                raise ValueError()
        count = result["page_count"]
        pages = result["pages"]
        if (type(count) is not int or not 1 <= count <= MAX_PAGES
                or not isinstance(pages, list) or len(pages) != count):
            raise ValueError()
        for index, page in enumerate(pages, 1):
            if not isinstance(page, dict) or set(page) != {"page_number", "text", "text_bytes", "rendered_pixels", "render_dpi"}:
                raise ValueError()
            if (type(page["page_number"]) is not int or page["page_number"] != index
                    or not isinstance(page["text"], str) or type(page["text_bytes"]) is not int
                    or page["text_bytes"] != len(page["text"].encode("utf-8"))
                    or type(page["rendered_pixels"]) is not int
                    or not 1 <= page["rendered_pixels"] <= MAX_PAGE_PIXELS
                    or type(page["render_dpi"]) is not int
                    or not 1 <= page["render_dpi"] <= RENDER_DPI):
                raise ValueError()
        if not any(page["text"].strip() for page in pages):
            raise PdfOcrError("pdf_ocr_empty_text")
        text = _join_pages(pages)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise PdfOcrError("pdf_ocr_text_budget_exceeded")
        page_dpis = {page["render_dpi"] for page in pages}
        reduced_count = sum(page["render_dpi"] < RENDER_DPI for page in pages)
        limitations = list(LIMITATIONS)
        if reduced_count:
            limitations.append(REDUCED_RESOLUTION_LIMITATION)
        document = {"extracted_text": text, "extraction": {
            "format": "pdf_ocr", "text_scope": "full_page_ocr_all_pages", "partial_text": True,
            "page_count": count, "all_pages_processed": True,
            "pages": [{key: value for key, value in page.items() if key != "text"} for page in pages],
            "text_bytes": len(text.encode("utf-8")), "engine": "pymupdf_pixmap_pdfocr",
            "engine_version": result["engine_version"], "mupdf_version": result["mupdf_version"],
            "ocr_backend": "tesseract", "ocr_backend_version": None,
            "languages": list(LANGUAGES), "model_sha256": models, "input_sha256": input_hash,
            "render_dpi": next(iter(page_dpis)) if len(page_dpis) == 1 else None,
            "requested_render_dpi": RENDER_DPI, "render_dpi_policy": RENDER_DPI_POLICY,
            "resolution_reduced_page_count": reduced_count, "ocr_worker_schema_version": WORKER_SCHEMA_VERSION,
            "cpu_only": True, "worker_count": 1, "limitations": limitations,
        }}
        if len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_BYTES:
            raise PdfOcrError("pdf_ocr_output_budget_exceeded")
        return document
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise PdfOcrError("pdf_ocr_response_invalid") from None


def extract_pdf_ocr(path, tessdata, *, runner=None) -> dict:
    """OCR every page or fail; ``runner`` follows subprocess.run's signature.

    Input, recognized text (including page markers), and worker JSON each have a
    32 MiB budget. The worker has 300s wall time, 240s CPU, 2 GiB address space,
    and a 32 MiB file-size cap. These bounds do not assert transcription accuracy.
    ``ocr_backend_version=None`` means the Tesseract version is not exposed by
    this API; PyMuPDF/MuPDF versions and the exact language model hashes are kept.
    Each page uses the highest integer DPI from 1 through the requested 200 that
    fits the pixel budget. Reduced resolution is disclosed, not quality-assured.
    """
    try:
        source = _read_regular(Path(path), "pdf_ocr_input_unavailable", "pdf_ocr_input_invalid",
                               "pdf_ocr_input_budget_exceeded")
        directory, models = _models(tessdata)
        input_hash = hashlib.sha256(source).hexdigest()
        with tempfile.TemporaryDirectory(prefix="cha-pdf-ocr-") as temporary:
            home = Path(temporary)
            os.chmod(home, 0o700)
            snapshot = home / "input.pdf"
            with os.fdopen(os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                handle.write(source)
            del source
            output = home / "result.json"
            with os.fdopen(os.open(output, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600), "w+b") as handle:
                try:
                    completed = (runner or subprocess.run)(
                        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--worker", str(snapshot), str(directory)],
                        shell=False, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.DEVNULL,
                        timeout=TIMEOUT_SECONDS, check=False, cwd=str(home),
                        env={"LANG": "C.UTF-8", "OMP_THREAD_LIMIT": "1", "OMP_NUM_THREADS": "1",
                             "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"})
                except subprocess.TimeoutExpired:
                    raise PdfOcrError("pdf_ocr_timeout") from None
                except OSError:
                    raise PdfOcrError("pdf_ocr_worker_failed") from None
                if os.fstat(handle.fileno()).st_size > MAX_BYTES:
                    raise PdfOcrError("pdf_ocr_output_budget_exceeded")
                if completed.returncode == -signal.SIGXFSZ:
                    raise PdfOcrError("pdf_ocr_output_budget_exceeded")
                if completed.returncode == -signal.SIGXCPU:
                    raise PdfOcrError("pdf_ocr_resource_limit_exceeded")
                if completed.returncode < 0:
                    raise PdfOcrError("pdf_ocr_worker_terminated")
                handle.seek(0)
                raw = handle.read(MAX_BYTES + 1)
            result = _decode_result(raw, input_hash, models)
            if completed.returncode != 0:
                raise PdfOcrError("pdf_ocr_worker_failed")
            return result
    except PdfOcrError:
        raise
    except (OSError, TypeError, ValueError):
        raise PdfOcrError("pdf_ocr_input_unavailable") from None


def _limit_resources():
    os.umask(0o077)
    for name, soft, hard in ((resource.RLIMIT_CPU, CPU_SECONDS, CPU_SECONDS + 5),
                             (resource.RLIMIT_AS, MEMORY_BYTES, MEMORY_BYTES),
                             (resource.RLIMIT_FSIZE, MAX_BYTES, MAX_BYTES),
                             (resource.RLIMIT_CORE, 0, 0)):
        _, existing_hard = resource.getrlimit(name)
        if existing_hard != resource.RLIM_INFINITY:
            hard = min(hard, existing_hard)
        resource.setrlimit(name, (min(soft, hard), hard))


def _page_render_dpi(rect, pymupdf) -> int:
    """Choose using MuPDF's raster bounds before allocating any page pixmap."""
    if (not all(math.isfinite(value) for value in (*rect, rect.width, rect.height))
            or rect.width <= 0 or rect.height <= 0):
        raise PdfOcrError("pdf_ocr_input_invalid")
    # At most 200 small geometry calculations per page. Descending enumeration
    # avoids an estimated DPI accidentally exceeding rounded .irect bounds.
    for dpi in range(RENDER_DPI, 0, -1):
        pixel_rect = (rect * pymupdf.Matrix(dpi / 72, dpi / 72)).irect
        if (pixel_rect.width > 0 and pixel_rect.height > 0
                and pixel_rect.width * pixel_rect.height <= MAX_PAGE_PIXELS):
            return dpi
    raise PdfOcrError("pdf_ocr_pixel_budget_exceeded")


def _ocr_pages(path: Path, tessdata: Path) -> dict:
    # Import the native engine only after the subprocess resource limits apply.
    try:
        import pymupdf
        if not pymupdf.mupdf.FZ_ENABLE_OCR_OUTPUT:
            raise ImportError()
    except (ImportError, AttributeError):
        raise PdfOcrError("pdf_ocr_engine_unavailable") from None
    source = _read_regular(path, "pdf_ocr_input_unavailable", "pdf_ocr_input_invalid", "pdf_ocr_input_budget_exceeded")
    directory, models = _models(tessdata)
    try:
        document = pymupdf.open(stream=source, filetype="pdf")
    except MemoryError:
        raise
    except Exception:
        raise PdfOcrError("pdf_ocr_input_invalid") from None
    with document:
        if document.is_encrypted or document.needs_pass:
            raise PdfOcrError("pdf_ocr_encrypted")
        if not document.is_pdf or document.page_count < 1:
            raise PdfOcrError("pdf_ocr_input_invalid")
        if document.page_count > MAX_PAGES:
            raise PdfOcrError("pdf_ocr_page_budget_exceeded")
        pages = []
        text_bytes = 0
        for index in range(document.page_count):
            page = document.load_page(index)
            dpi = _page_render_dpi(page.rect, pymupdf)
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False, annots=True)
            pixels = pixmap.width * pixmap.height
            if not 1 <= pixels <= MAX_PAGE_PIXELS:
                raise PdfOcrError("pdf_ocr_pixel_budget_exceeded")
            ocr_bytes = pixmap.pdfocr_tobytes(compress=True, language="eng+kor", tessdata=str(directory))
            with pymupdf.open(stream=ocr_bytes, filetype="pdf") as recognized:
                if recognized.page_count != 1:
                    raise PdfOcrError("pdf_ocr_worker_failed")
                text = recognized[0].get_text("text", sort=False)
            size = len(text.encode("utf-8"))
            text_bytes += size + len(("[Page " + str(index + 1) + "]\n").encode()) + (2 if index else 0)
            if text_bytes > MAX_BYTES:
                raise PdfOcrError("pdf_ocr_text_budget_exceeded")
            pages.append({"page_number": index + 1, "text": text, "text_bytes": size,
                          "rendered_pixels": pixels, "render_dpi": dpi})
            del pixmap, ocr_bytes
        if not any(page["text"].strip() for page in pages):
            raise PdfOcrError("pdf_ocr_empty_text")
        if _models(directory)[1] != models:
            raise PdfOcrError("pdf_ocr_tessdata_changed")
        return {"schema_version": WORKER_SCHEMA_VERSION, "input_sha256": hashlib.sha256(source).hexdigest(),
                "engine_version": str(pymupdf.VersionBind), "mupdf_version": str(pymupdf.VersionFitz),
                "model_sha256": models, "page_count": document.page_count, "pages": pages}


def _worker_main(argv):
    try:
        _limit_resources()
        if len(argv) != 3 or argv[0] != "--worker":
            raise PdfOcrError("pdf_ocr_worker_failed")
        result = _ocr_pages(Path(argv[1]), Path(argv[2]))
        raw = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise PdfOcrError("pdf_ocr_output_budget_exceeded")
        code = 0
    except Exception as exc:
        error = exc.code if isinstance(exc, PdfOcrError) else (
            "pdf_ocr_resource_limit_exceeded" if isinstance(exc, MemoryError) else "pdf_ocr_worker_failed")
        raw = json.dumps({"schema_version": WORKER_SCHEMA_VERSION, "error": error}).encode("ascii")
        code = 2
    # No text is emitted until all pages and budgets have passed.
    try:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()
    except OSError:
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(_worker_main(sys.argv[1:]))
