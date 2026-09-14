"""Synthetic PDFs and fake workers; no private corpus, network, or real OCR."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tools.cha_philosophy import pdf_ocr as ocr

try:
    import pymupdf
except ImportError:
    pymupdf = None


class PdfOcrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.pdf = self.home / 'input;$(must-not-run).pdf'
        self.pdf.write_bytes(b"%PDF- synthetic input for fake worker")
        self.tessdata = self.home / 'models;$(must-not-run)'
        self.tessdata.mkdir()
        for language in ocr.LANGUAGES:
            (self.tessdata / (language + '.traineddata')).write_bytes((language + ' synthetic model').encode())

    def payload(self, texts=('First page\n', '한글 두 번째 페이지\n')):
        return {'schema_version': 2,
            'input_sha256': hashlib.sha256(self.pdf.read_bytes()).hexdigest(),
            'engine_version': 'synthetic-1', 'mupdf_version': 'synthetic-2',
            'model_sha256': ocr._models(self.tessdata)[1], 'page_count': len(texts),
            'pages': [{'page_number': i, 'text': text, 'text_bytes': len(text.encode()), 'rendered_pixels': 10000, 'render_dpi': 200}
                      for i, text in enumerate(texts, 1)]}

    def runner(self, payload=None, *, returncode=0):
        result = self.payload() if payload is None else payload
        def run(args, **kwargs):
            raw = result if isinstance(result, bytes) else json.dumps(result, ensure_ascii=False).encode()
            kwargs['stdout'].write(raw)
            kwargs['stdout'].flush()
            return subprocess.CompletedProcess(args, returncode)
        return run

    def assert_error(self, code, operation):
        with self.assertRaises(ocr.PdfOcrError) as raised:
            operation()
        self.assertEqual(str(raised.exception), code)
        self.assertEqual(raised.exception.code, code)

    def test_all_pages_unicode_counts_markers_and_honest_limits(self):
        payload = self.payload(('FIRST\n', '', '한국어 마지막\n'))
        result = ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=self.runner(payload))
        self.assertEqual(result['extracted_text'], '[Page 1]\nFIRST\n\n\n[Page 2]\n\n\n[Page 3]\n한국어 마지막\n')
        metadata = result['extraction']
        self.assertEqual(metadata['page_count'], 3)
        self.assertEqual([p['text_bytes'] for p in metadata['pages']], [6, 0, len('한국어 마지막\n'.encode())])
        self.assertEqual(metadata['text_bytes'], len(result['extracted_text'].encode()))
        self.assertTrue(metadata['partial_text'])
        self.assertTrue(metadata['all_pages_processed'])
        self.assertIn('ocr_transcription_not_human_reviewed', metadata['limitations'])
        self.assertEqual(metadata['languages'], ['eng', 'kor'])
        self.assertIsNone(metadata['ocr_backend_version'])
        self.assertEqual(metadata['model_sha256'], payload['model_sha256'])
        self.assertEqual(metadata['render_dpi'], 200)
        self.assertEqual(metadata['requested_render_dpi'], 200)
        self.assertEqual(metadata['resolution_reduced_page_count'], 0)
        self.assertEqual([page['render_dpi'] for page in metadata['pages']], [200, 200, 200])
        self.assertNotIn(ocr.REDUCED_RESOLUTION_LIMITATION, metadata['limitations'])

    def test_fixed_process_arguments_private_files_and_no_shell_or_secret_environment(self):
        original = self.runner()
        def run(args, **kwargs):
            self.assertEqual(args[:4], [sys.executable, '-I', '-B', str(Path(ocr.__file__).resolve())])
            self.assertEqual(args[4], '--worker')
            self.assertEqual(args[6], str(self.tessdata))
            self.assertNotIn(str(self.pdf), args)
            snapshot = Path(args[5])
            self.assertEqual(snapshot.read_bytes(), self.pdf.read_bytes())
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.assertEqual(snapshot.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(os.fstat(kwargs['stdout'].fileno()).st_mode & 0o777, 0o600)
            self.assertFalse(kwargs['shell'])
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            self.assertEqual(kwargs['timeout'], 300)
            self.assertEqual(kwargs['env']['OMP_THREAD_LIMIT'], '1')
            self.assertNotIn('HOME', kwargs['env'])
            self.assertNotIn('SYNTHETIC_SECRET', kwargs['env'])
            return original(args, **kwargs)
        with patch.dict(os.environ, {'SYNTHETIC_SECRET': 'do-not-inherit'}):
            ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=run)

    def test_missing_symlink_and_empty_models_fail_before_runner(self):
        run = Mock()
        model = self.tessdata / 'eng.traineddata'
        model.unlink()
        self.assert_error('pdf_ocr_tessdata_unavailable', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=run))
        model.symlink_to(self.tessdata / 'kor.traineddata')
        self.assert_error('pdf_ocr_tessdata_unavailable', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=run))
        model.unlink(); model.write_bytes(b'')
        self.assert_error('pdf_ocr_tessdata_invalid', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=run))
        run.assert_not_called()

    def test_input_size_and_symlink_rejected_without_worker(self):
        run = Mock()
        with self.pdf.open('wb') as handle:
            handle.truncate(ocr.MAX_BYTES + 1)
        self.assert_error('pdf_ocr_input_budget_exceeded', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=run))
        link = self.home / 'link.pdf'; link.symlink_to(self.pdf)
        self.assert_error('pdf_ocr_input_unavailable', lambda: ocr.extract_pdf_ocr(link, self.tessdata, runner=run))
        run.assert_not_called()

    def test_malformed_or_incomplete_worker_output_is_rejected(self):
        original = self.payload()
        cases = []
        value = copy.deepcopy(original); value['pages'].pop(); cases.append(value)
        value = copy.deepcopy(original); value['pages'][1]['page_number'] = 1; cases.append(value)
        value = copy.deepcopy(original); value['pages'][1]['text_bytes'] = 1; cases.append(value)
        value = copy.deepcopy(original); value['input_sha256'] = 'other'; cases.append(value)
        value = copy.deepcopy(original); value['model_sha256']['eng'] = 'other'; cases.append(value)
        value = copy.deepcopy(original); value['schema_version'] = True; cases.append(value)
        value = copy.deepcopy(original); value['schema_version'] = 1; cases.append(value)
        cases.extend([b'{"schema_version":2,"schema_version":2}', b'{"schema_version":2,"error":NaN}', b'private native failure'])
        for value in cases:
            with self.subTest(case=cases.index(value)):
                self.assert_error('pdf_ocr_response_invalid', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=self.runner(value)))

    def test_empty_ocr_never_turns_markers_into_a_blank_success(self):
        self.assert_error('pdf_ocr_empty_text', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=self.runner(self.payload(('', ' \n')))))

    def test_timeout_and_safe_worker_errors_do_not_expose_stderr(self):
        run = Mock(side_effect=subprocess.TimeoutExpired(['private path'], 300, output=b'private text', stderr=b'private secret'))
        self.assert_error('pdf_ocr_timeout', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=run))
        self.assert_error('pdf_ocr_encrypted', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata,
            runner=self.runner({'schema_version': 2, 'error': 'pdf_ocr_encrypted'}, returncode=2)))
        self.assertEqual(str(ocr.PdfOcrError('private unknown error')), 'pdf_ocr_worker_failed')

    def test_worker_signals_and_sparse_output_overflow(self):
        for sig, code in ((signal.SIGXCPU, 'pdf_ocr_resource_limit_exceeded'),
                          (signal.SIGXFSZ, 'pdf_ocr_output_budget_exceeded'),
                          (signal.SIGKILL, 'pdf_ocr_worker_terminated')):
            with self.subTest(signal=sig):
                self.assert_error(code, lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=self.runner(b'', returncode=-sig)))
        def oversized(args, **kwargs):
            kwargs['stdout'].seek(ocr.MAX_BYTES)
            kwargs['stdout'].write(b'x'); kwargs['stdout'].flush()
            return subprocess.CompletedProcess(args, 0)
        self.assert_error('pdf_ocr_output_budget_exceeded', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=oversized))

    def test_text_budget_includes_all_page_markers(self):
        payload = self.payload(('x' * 30, 'y' * 30))
        with patch.object(ocr, 'MAX_BYTES', 65):
            self.assert_error('pdf_ocr_text_budget_exceeded', lambda: ocr._decode_result(
                json.dumps(payload).encode(), payload['input_sha256'], payload['model_sha256']))
        with patch.object(ocr, 'MAX_BYTES', 512):
            self.assert_error('pdf_ocr_output_budget_exceeded', lambda: ocr._decode_result(
                json.dumps(payload).encode(), payload['input_sha256'], payload['model_sha256']))

    def test_limits_set_only_in_worker_and_failure_has_no_partial_text(self):
        with patch.object(ocr.resource, 'getrlimit', return_value=(-1, -1)), patch.object(ocr.resource, 'setrlimit') as limits, patch.object(ocr.os, 'umask'):
            ocr._limit_resources()
        calls = {call.args[0]: call.args[1] for call in limits.call_args_list}
        self.assertEqual(calls[resource.RLIMIT_CPU], (240, 245))
        self.assertEqual(calls[resource.RLIMIT_AS], (2 * 1024**3, 2 * 1024**3))
        self.assertEqual(calls[resource.RLIMIT_FSIZE], (ocr.MAX_BYTES, ocr.MAX_BYTES))
        capture = io.BytesIO()
        with patch.object(ocr, '_limit_resources'), patch.object(ocr, '_ocr_pages', side_effect=RuntimeError('private page 2 failed')), patch.object(sys, 'stdout', SimpleNamespace(buffer=capture)):
            status = ocr._worker_main(['--worker', str(self.pdf), str(self.tessdata)])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(capture.getvalue()), {'schema_version': 2, 'error': 'pdf_ocr_worker_failed'})

    @unittest.skipIf(pymupdf is None, 'PyMuPDF is optional')
    def test_real_worker_rejects_invalid_and_encrypted_synthetic_pdf(self):
        self.assert_error('pdf_ocr_input_invalid', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata))
        with pymupdf.open() as doc:
            doc.new_page()
            doc.save(self.pdf, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw='synthetic-owner', user_pw='synthetic-user')
        self.assert_error('pdf_ocr_encrypted', lambda: ocr.extract_pdf_ocr(self.pdf, self.tessdata))

    @unittest.skipIf(pymupdf is None, 'PyMuPDF is optional')
    def test_page_and_pixel_budget_stop_before_rasterization(self):
        for pages, width, height, code in ((257, 100, 100, 'pdf_ocr_page_budget_exceeded'),
                                           (1, 300000, 300000, 'pdf_ocr_pixel_budget_exceeded')):
            with self.subTest(code=code):
                with pymupdf.open() as doc:
                    for _ in range(pages):
                        doc.new_page(width=width, height=height)
                    self.pdf.write_bytes(doc.tobytes())
                with patch.object(pymupdf.Page, 'get_pixmap') as raster:
                    self.assert_error(code, lambda: ocr._ocr_pages(self.pdf, self.tessdata))
                    raster.assert_not_called()

    def test_malformed_page_dpi_is_rejected(self):
        for dpi in (True, 84.0, '84', 0, -1, 201, None):
            with self.subTest(dpi=dpi):
                value = self.payload()
                value['pages'][0]['render_dpi'] = dpi
                self.assert_error('pdf_ocr_response_invalid', lambda: ocr.extract_pdf_ocr(
                    self.pdf, self.tessdata, runner=self.runner(value)))
        value = self.payload(); value['pages'][0].pop('render_dpi')
        self.assert_error('pdf_ocr_response_invalid', lambda: ocr.extract_pdf_ocr(
            self.pdf, self.tessdata, runner=self.runner(value)))

    def test_uniform_reduced_and_mixed_dpi_metadata_are_truthful(self):
        for dpis, summary in (((84, 84), 84), ((200, 84), None)):
            with self.subTest(dpis=dpis):
                value = self.payload()
                for page, dpi in zip(value['pages'], dpis):
                    page['render_dpi'] = dpi
                metadata = ocr.extract_pdf_ocr(self.pdf, self.tessdata, runner=self.runner(value))['extraction']
                self.assertEqual(metadata['render_dpi'], summary)
                self.assertEqual(metadata['requested_render_dpi'], 200)
                self.assertEqual(metadata['render_dpi_policy'], 'highest_integer_dpi_within_page_pixel_budget')
                self.assertEqual(metadata['resolution_reduced_page_count'], sum(dpi < 200 for dpi in dpis))
                self.assertIn(ocr.REDUCED_RESOLUTION_LIMITATION, metadata['limitations'])

    @unittest.skipIf(pymupdf is None, 'PyMuPDF is optional')
    def test_actual_giant_page_uses_highest_fitting_dpi_and_mixed_worker_metadata(self):
        with pymupdf.open() as doc:
            doc.new_page(width=100, height=200)
            doc.new_page(width=2551.08, height=3401.64)
            self.pdf.write_bytes(doc.tobytes())
        with pymupdf.open(self.pdf) as doc:
            rect = doc[1].rect
            self.assertEqual(ocr._page_render_dpi(rect, pymupdf), 84)
            fits = (rect * pymupdf.Matrix(84 / 72, 84 / 72)).irect
            exceeds = (rect * pymupdf.Matrix(85 / 72, 85 / 72)).irect
            self.assertLessEqual(fits.width * fits.height, 12_000_000)
            self.assertGreater(exceeds.width * exceeds.height, 12_000_000)
        with pymupdf.open() as recognized:
            recognized.new_page().insert_text((72, 72), 'SYNTHETIC')
            raw = recognized.tobytes()
        def raster(page, **kwargs):
            pixels = (page.rect * pymupdf.Matrix(kwargs['dpi'] / 72, kwargs['dpi'] / 72)).irect
            return SimpleNamespace(width=pixels.width, height=pixels.height, pdfocr_tobytes=Mock(return_value=raw))
        with patch.object(pymupdf.Page, 'get_pixmap', autospec=True, side_effect=raster) as render:
            payload = ocr._ocr_pages(self.pdf, self.tessdata)
        self.assertEqual([call.kwargs['dpi'] for call in render.call_args_list], [200, 84])
        self.assertEqual([page['render_dpi'] for page in payload['pages']], [200, 84])
        metadata = ocr._decode_result(json.dumps(payload).encode(), payload['input_sha256'], payload['model_sha256'])['extraction']
        self.assertIsNone(metadata['render_dpi'])
        self.assertEqual(metadata['resolution_reduced_page_count'], 1)

    @unittest.skipIf(pymupdf is None, 'PyMuPDF is optional')
    def test_full_page_ocr_visits_every_page_and_passes_explicit_languages(self):
        with pymupdf.open() as doc:
            doc.new_page(width=100, height=200).set_rotation(90)
            doc.new_page(width=200, height=100)
            self.pdf.write_bytes(doc.tobytes())
        outputs = []
        for word in ('FIRST', 'SECOND'):
            with pymupdf.open() as doc:
                page = doc.new_page(); page.insert_text((72, 72), word)
                outputs.append(doc.tobytes())
        pixmaps = [SimpleNamespace(width=500, height=250, pdfocr_tobytes=Mock(return_value=raw)) for raw in outputs]
        with patch.object(pymupdf.Page, 'get_pixmap', side_effect=pixmaps) as raster:
            result = ocr._ocr_pages(self.pdf, self.tessdata)
        self.assertEqual(result['page_count'], 2)
        self.assertEqual([p['text'].strip() for p in result['pages']], ['FIRST', 'SECOND'])
        self.assertEqual(raster.call_count, 2)
        for call in raster.call_args_list:
            self.assertEqual(call.kwargs, {'dpi': 200, 'colorspace': pymupdf.csRGB, 'alpha': False, 'annots': True})
        for pixmap in pixmaps:
            pixmap.pdfocr_tobytes.assert_called_once_with(compress=True, language='eng+kor', tessdata=str(self.tessdata))
        with patch.object(pymupdf.Page, 'get_pixmap', side_effect=[pixmaps[0], RuntimeError('second-page failure')]), patch.object(ocr, '_limit_resources'):
            capture = io.BytesIO()
            with patch.object(sys, 'stdout', SimpleNamespace(buffer=capture)):
                self.assertEqual(ocr._worker_main(['--worker', str(self.pdf), str(self.tessdata)]), 2)
            self.assertNotIn(b'FIRST', capture.getvalue())
            self.assertEqual(json.loads(capture.getvalue())['error'], 'pdf_ocr_worker_failed')


if __name__ == '__main__':
    unittest.main()
