"""Adversarial source attribution and read-only pagination contracts."""
import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from tools.cha_philosophy.connectors import (
    ConnectorError, GogClient, SyncProgress, TeamsGraphClient, authored_text,
    normalize_drive_comment, normalize_drive_document, normalize_gmail, normalize_teams,
    normalize_teams_native, sanitize_source,
    READABLE_DRIVE_MIME_TYPES,
)


def mail(mid, sender="Professor <prof@example.org>", to="student@example.org", text="증거를 먼저 확인하세요.", labels=None, html=False):
    return {"id": mid, "internalDate": "1700000000000", "labelIds": ["SENT"] if labels is None else labels,
            "payload": {"mimeType": "text/html" if html else "text/plain",
                        "headers": [{"name": "From", "value": sender}, {"name": "To", "value": to},
                                    {"name": "Subject", "value": "연구 기준"}],
                        "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}


def thread(*messages):
    return {"_account_id": "prof@example.org", "thread": {"id": "thread-1", "messages": list(messages)}}


def graph_message(mid="m1", uid="prof-oid", **kwargs):
    return {"id": mid, "from": {"user": {"id": uid, "displayName": "차지욱"}},
            "body": {"contentType": "html", "content": "<p>근거가 바뀌면 판단을 바꾸세요.</p>"},
            "messageType": "message", **kwargs}


class AttributionTests(unittest.TestCase):
    def normalize(self, raw):
        return normalize_gmail(raw, {"prof@example.org", "accepted-alias@example.org"}, {"student@example.org"})

    def test_thread_context_and_quoted_student_do_not_become_professor_evidence(self):
        sent = mail("1", text='<p>근거를 확인하세요.</p><blockquote>교수님은 무조건 맞습니다.</blockquote><p>결론은 보류합니다.</p>', html=True)
        received = mail("2", sender="Student <student@example.org>", to="prof@example.org", text="교수님은 항상 맞습니다.", labels=["INBOX"])
        rows = self.normalize(thread(sent, received))
        self.assertEqual([r["authorship"] for r in rows], ["direct", "context"])
        self.assertIn("무조건", rows[0]["body"])
        self.assertNotIn("무조건", rows[0]["authored_text"])
        self.assertIn("결론은 보류", rows[0]["authored_text"])
        self.assertEqual(rows[1]["authored_text"], "")

    def test_exact_recipient_and_sent_label_required_even_with_custom_query(self):
        self.assertEqual(self.normalize(thread(mail("1", to="student@example.org.attacker.net"))), [])
        self.assertEqual(self.normalize(thread(mail("1", labels=["INBOX"]))), [])
        self.assertEqual(self.normalize(thread(mail("1", sender="Bot <unverified@example.org>"))), [])
        self.assertEqual(self.normalize(thread(mail("1", sender="accepted-alias@example.org")))[0]["authorship"], "direct")

    def test_inline_prefix_quotes_keep_following_authored_paragraph(self):
        text = "> 학생의 질문\n직접 작성한 답변\n> 또 다른 질문\n직접 작성한 다음 답변"
        result, meta = authored_text(text)
        self.assertIn("다음 답변", result)
        self.assertNotIn("학생의 질문", result)
        self.assertEqual(meta["excluded_quote_lines"], 2)

    def test_ambiguous_quoted_tail_is_explicitly_flagged_and_full_body_retained(self):
        body = "직접 발언\nOn Monday, Student wrote:\n누가 썼는지 모르는 문장"
        row = self.normalize(thread(mail("1", text=body)))[0]
        self.assertEqual(row["body"], body)
        self.assertEqual(row["authored_text"], "직접 발언")
        self.assertTrue(row["metadata"]["quote_parse_uncertain"])

    def test_snippet_only_is_failure_not_complete_source(self):
        with self.assertRaisesRegex(ConnectorError, "message_full_body_missing"):
            self.normalize(thread({"id": "1", "snippet": "incomplete"}))

    def test_multipart_alternative_deduplicates_and_omits_attachment(self):
        sent = mail("1")
        sent["payload"] = {**sent["payload"], "mimeType": "multipart/mixed", "parts": [
            {"mimeType": "multipart/alternative", "parts": [mail("x", text="동일 발언")["payload"], mail("x", text="<p>동일 발언</p>", html=True)["payload"]]},
            {"filename": "joint.txt", "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"not the professor").decode()}}
        ]}
        row = self.normalize(thread(sent))[0]
        self.assertEqual(row["body"].count("동일 발언"), 1)
        self.assertNotIn("not the professor", row["body"])

    def test_wrong_korean_charset_uses_strict_utf8_with_provenance(self):
        msg = mail("1", text="근거가 달라지면 판단을 바꾸세요.")
        msg["payload"]["headers"].append({"name": "Content-Type", "value": "text/plain; charset=ks_c_5601-1987"})
        row = self.normalize(thread(msg))[0]
        self.assertEqual(row["body"], "근거가 달라지면 판단을 바꾸세요.")
        discrepancy = row["metadata"]["charset_discrepancies"][0]
        self.assertEqual(discrepancy["declared_charset"], "ks_c_5601-1987")
        self.assertEqual(discrepancy["decoded_charset"], "utf-8")
        self.assertEqual(discrepancy["part_path"], "payload")
        self.assertNotIn("\ufffd", row["body"])

    def test_correct_declared_euckr_is_not_reinterpreted_and_bad_bytes_fail(self):
        msg = mail("1")
        msg["payload"]["headers"].append({"name": "Content-Type", "value": "text/plain; charset=EUC-KR"})
        msg["payload"]["body"]["data"] = base64.urlsafe_b64encode("근거 우선".encode("euc-kr")).decode()
        row = self.normalize(thread(msg))[0]
        self.assertEqual(row["body"], "근거 우선")
        self.assertNotIn("charset_discrepancies", row["metadata"])
        msg["payload"]["body"]["data"] = base64.urlsafe_b64encode(b"\xff\xff").decode()
        with self.assertRaisesRegex(ConnectorError, "undecodable_message_charset"):
            self.normalize(thread(msg))

    def test_malformed_nontext_attachment_subtree_does_not_block_body(self):
        msg = mail("1")
        msg["payload"] = {**msg["payload"], "mimeType": "multipart/mixed", "parts": [
            mail("x", text="검증할 본문")["payload"],
            {"mimeType": "application/octet-stream", "headers": "malformed", "parts": [
                {"mimeType": "text/plain", "body": {"data": "invalid%%%"}}
            ]},
            {"mimeType": "message/rfc822", "parts": [{"mimeType": "text/plain", "body": {"attachmentId": "missing"}}]}
        ]}
        row = self.normalize(thread(msg))[0]
        self.assertEqual(row["body"], "검증할 본문")
        self.assertEqual(row["metadata"]["excluded_nonbody_mime_parts"], 2)

    def test_raw_message_wrong_charset_has_same_strict_fallback(self):
        msg = mail("1")
        raw = b"From: prof@example.org\r\nTo: student@example.org\r\nContent-Type: text/plain; charset=EUC-KR\r\nContent-Transfer-Encoding: base64\r\n\r\n" + base64.b64encode("확인된 근거".encode())
        msg.pop("payload")
        msg["raw"] = base64.urlsafe_b64encode(raw).decode()
        row = self.normalize(thread(msg))[0]
        self.assertEqual(row["body"], "확인된 근거")
        self.assertEqual(row["metadata"]["charset_discrepancies"][0]["part_path"], "raw.body")

    def test_teams_composite_id_and_no_display_name_attribution(self):
        a = normalize_teams(graph_message(), "tenant", "chat1", {"prof-oid"})
        b = normalize_teams(graph_message(uid="someone-else"), "tenant", "chat2", {"prof-oid"})
        c = normalize_teams(graph_message(), "tenant", "chat1", {"prof-oid"}, parent_id="other-root")
        self.assertEqual(len({a["source_id"], b["source_id"], c["source_id"]}), 3)
        self.assertEqual(b["authorship"], "context")
        self.assertEqual(b["authored_text"], "")

    def test_reaction_does_not_change_body_fingerprint_and_deletion_clears_content(self):
        a = normalize_teams(graph_message(), "t", "c", {"prof-oid"})
        b = normalize_teams(graph_message(lastModifiedDateTime="2026-09-12", reactions=[{"reactionType": "like"}]), "t", "c", {"prof-oid"})
        self.assertEqual(a["metadata"]["body_sha256"], b["metadata"]["body_sha256"])
        gone = normalize_teams(graph_message(deletedDateTime="2026-09-12"), "t", "c", {"prof-oid"})
        self.assertEqual((gone["status"], gone["body"], gone["authored_text"]), ("deleted", "", ""))

    def test_drive_only_verified_requesting_account_me_is_direct(self):
        file = {"id": "file", "_account_id": "prof@example.org", "name": "공동 원고"}
        comment = {"id": "comment", "author": {"me": True, "displayName": "차지욱"}, "content": "근거부터",
                   "quotedFileContent": {"value": "학생의 과도한 결론"},
                   "replies": [{"id": "reply", "author": {"displayName": "차지욱"}, "content": "동의"}]}
        rows = normalize_drive_comment(file, comment, True)
        self.assertEqual([r["authorship"] for r in rows], ["direct", "unverified"])
        self.assertEqual(rows[0]["body"], "근거부터")
        self.assertEqual(normalize_drive_comment(file, comment, False)[0]["authorship"], "unverified")

    def test_drive_nested_tabs_and_shared_ownership(self):
        def tab(tid, text, children=None):
            return {"tabProperties": {"tabId": tid, "title": tid}, "documentTab": {"body": {"content": [{"paragraph": {"elements": [{"textRun": {"content": text}}]}}]}}, "childTabs": children or []}
        doc = {"tabs": [tab("one", "첫 탭", [tab("child", "중첩 원칙")]), tab("two", "다른 원칙")]}
        row = normalize_drive_document({"id": "f", "owners": [{"emailAddress": "prof@example.org"}]}, doc)
        self.assertIn("중첩 원칙", row["body"])
        self.assertIn("다른 원칙", row["body"])
        self.assertEqual(row["metadata"]["tab_ids"], ["one", "child", "two"])
        self.assertEqual(row["authorship"], "unverified")
        with self.assertRaisesRegex(ConnectorError, "all_tabs"):
            normalize_drive_document({"id": "f"}, {"body": {}})

    def test_metadata_does_not_copy_auth_headers_or_signed_attachment_query(self):
        message = mail("1")
        message["payload"]["headers"].append({"name": "X-Auth-Token", "value": "SECRET"})
        row = self.normalize(thread(message))[0]
        self.assertNotIn("SECRET", json.dumps(row))
        raw = graph_message(attachments=[{"id": "a", "contentUrl": "https://example.org/file?token=SECRET", "content": "SECRET"}])
        row = normalize_teams(raw, "t", "c", {"prof-oid"})
        self.assertNotIn("SECRET", json.dumps(row))


class NativeAndPrivacyTests(unittest.TestCase):
    def test_native_message_identity_parent_and_text_limits(self):
        raw = {"message_id": "m", "author_user_id": "prof-id", "author_name": "차지욱",
               "channel_id": "c", "team_id": "t", "parent_message_id": "r",
               "content": "원본을 보존하세요.\n> 학생의 인용문", "created_at": "2026-09-12T00:00:00Z"}
        row = normalize_teams_native(raw, "tenant", {"prof-id"})
        self.assertEqual(row["source_id"], "teams/tenant/c/r/m")
        self.assertEqual(row["authorship"], "direct")
        self.assertNotIn("학생", row["authored_text"])
        self.assertIn("native_edit_timestamp_unavailable", row["metadata"]["limitations"])
        for changed in ({"author_user_id": None}, {"author_application_id": "bot"}):
            self.assertNotEqual(normalize_teams_native({**raw, **changed}, "tenant", {"prof-id"})["authorship"], "direct")

    def test_native_search_summary_cannot_be_direct_evidence(self):
        with self.assertRaisesRegex(ConnectorError, "message_required"):
            normalize_teams_native({"path": "p", "summary": "search excerpt", "sender_name": "차지욱"}, "t", {"id"})

    def test_redaction_removes_credentials_preserves_prose_without_mutation(self):
        text = "근거를 먼저 봅시다.\npassword: test-private-password\n평가는 독립적으로 하세요.\nAPI_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz"
        row = {"body": text, "authored_text": text, "metadata": {"headers": {"subject": "논문"}, "body_sha256": "old"}}
        clean = sanitize_source(row)
        self.assertNotIn("test-private-password", json.dumps(clean))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", json.dumps(clean))
        self.assertIn("근거를 먼저", clean["authored_text"])
        self.assertIn("독립적으로", clean["authored_text"])
        self.assertEqual(row["body"], text)
        self.assertEqual(clean, sanitize_source(clean))
        self.assertGreater(clean["metadata"]["secret_redaction_count"], 0)
        self.assertEqual(clean["metadata"]["text_transform"], "credential_redaction_v1")
        self.assertNotEqual(clean["metadata"]["body_sha256"], "old")

    def test_private_keys_and_nested_metadata_are_redacted(self):
        row = {"body": "-----BEGIN RSA PRIVATE KEY-----\nprivate key bytes\n-----END RSA PRIVATE KEY-----",
               "metadata": {"quoted_file_content": {"value": "비밀번호: lab-secret"}, "attachments": ["Bearer abcdefghijklmnopqrstuvwxyz"]}}
        clean = sanitize_source(row)
        for secret in ("private key bytes", "lab-secret", "abcdefghijklmnopqrstuvwxyz"):
            self.assertNotIn(secret, json.dumps(clean))

    def test_redaction_keeps_adjacent_prose_and_handles_metadata_credentials(self):
        row = {"body": 'password="multiple word secret"; 근거를 보존하세요. token=abcdefghi&next=kept',
               "metadata": {"api_key": "plain-secret", "authorship_basis": "verified", "password": 123456}}
        clean = sanitize_source(row)
        for secret in ("multiple word secret", "abcdefghi", "plain-secret", "123456"):
            self.assertNotIn(secret, json.dumps(clean))
        self.assertIn("근거를 보존하세요.", clean["body"])
        self.assertIn("&next=kept", clean["body"])
        self.assertEqual(clean["metadata"]["authorship_basis"], "verified")
        self.assertEqual(clean, sanitize_source(clean))


class GogTests(unittest.TestCase):
    def client(self, responses):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(r), "") for r in responses])
        return GogClient("prof@example.org", runner=runner), runner

    def test_all_drive_pages_and_shell_false_no_results_only(self):
        client, runner = self.client([{"files": [{"id": "1"}], "nextPageToken": "next"}, {"files": [{"id": "2"}]}])
        progress = SyncProgress()
        self.assertEqual([f["id"] for f in client.iter_drive_inventory(query="trashed=false", progress=progress)], ["1", "2"])
        self.assertTrue(progress.complete)
        self.assertEqual(progress.pages, 2)
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertIn("--page=next", runner.call_args.args[0])
        self.assertNotIn("--results-only", runner.call_args.args[0])

    def test_write_and_global_auth_flags_cannot_enter_runner(self):
        client, runner = self.client([])
        for command, args in [(("gmail", "send"), []), (("drive", "get"), ["--access-token=secret"]), (("drive", "get"), ["--account=other@example.org"])]:
            with self.assertRaises(ConnectorError):
                client._call(command, args)
        runner.assert_not_called()

    def test_thread_cap_reports_current_page_remaining_ids(self):
        client, _ = self.client([{"threads": [{"id": "a"}, {"id": "b"}], "nextPageToken": "later"}, thread(mail("1"))])
        progress = SyncProgress()
        items = list(client.iter_sent_threads({"prof@example.org"}, {"student@example.org"}, max_threads=1, progress=progress))
        self.assertEqual(len(items), 1)
        self.assertFalse(progress.complete)
        self.assertEqual(progress.pending_ids, ["b"])
        self.assertIn("thread_cap_reached", progress.limitations)

    def test_missing_envelope_and_repeated_token_are_not_success(self):
        client, _ = self.client([{"result": []}])
        with self.assertRaisesRegex(ConnectorError, "collection_shape"):
            list(client.iter_drive_inventory())
        client, _ = self.client([{"files": [], "nextPageToken": "x"}, {"files": [], "nextPageToken": "x"}])
        with self.assertRaisesRegex(ConnectorError, "pagination_cycle"):
            list(client.iter_drive_inventory())

    def test_comment_limit_exposes_cursor_and_deletion_limitation(self):
        client, _ = self.client([{"comments": [{"id": "1"}], "nextPageToken": "more"}])
        progress = SyncProgress()
        self.assertEqual(len(list(client.iter_drive_comments("f", max_pages=1, progress=progress))), 1)
        self.assertEqual(progress.remaining_page_token, "more")
        self.assertFalse(progress.complete)
        self.assertIn("gog_comments_no_include_deleted_flag", progress.limitations)

    def test_command_errors_do_not_leak_stderr(self):
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", "invalid_grant token=SECRET"))
        with self.assertRaises(ConnectorError) as ctx:
            GogClient("p@example.org", runner=runner).get_thread("1")
        self.assertEqual(str(ctx.exception), "gog_auth_required")
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_source_denial_not_found_and_transient_error_are_distinguished(self):
        cases = [("Google API error (403 forbidden): private content", "source_access_denied"),
                 ("Google API error (404 notFound): private id", "source_not_found"),
                 ("network timeout", "gog_read_failed"),
                 ("HTTP 403 invalid_grant", "gog_auth_required"),
                 ("HTTP 401 authError", "gog_auth_required"),
                 ("HTTP 403 rateLimitExceeded", "gog_rate_limited"),
                 ("HTTP 403 userRateLimitExceeded", "gog_rate_limited"),
                 ("HTTP 429 Too Many Requests", "gog_rate_limited"),
                 ("HTTP 403 dailyLimitExceeded", "gog_quota_exceeded"),
                 ("HTTP 403 storageQuotaExceeded", "gog_quota_exceeded"),
                 ("HTTP 403 exportSizeLimitExceeded", "source_access_denied")]
        for stderr, expected in cases:
            runner = Mock(return_value=subprocess.CompletedProcess([], 1, "", stderr))
            with self.assertRaises(ConnectorError) as ctx:
                GogClient("p@example.org", runner=runner).get_drive_metadata("f")
            self.assertEqual(ctx.exception.code, expected)
            self.assertNotIn("private", str(ctx.exception))
            self.assertEqual(ctx.exception.operation, "drive.get")


class DriveFormatsTests(unittest.TestCase):
    def client(self, responses):
        runner = Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(r), "") for r in responses])
        return GogClient("prof@example.org", runner=runner), runner

    def test_all_sheet_tabs_values_notes_and_quoted_titles(self):
        client, runner = self.client([
            {"sheets": [{"properties": {"title": "교수's 기준", "sheetId": 1, "gridProperties": {"rowCount": 2, "columnCount": 2}}},
                        {"properties": {"title": "숨김", "sheetId": 2, "hidden": True, "gridProperties": {"rowCount": 1, "columnCount": 1}}}]},
            {"values": [["근거", "맥락"], ["비판"]]}, {"notes": [{"a1": "B2", "note": "왜 달라졌는지 설명"}]},
            {"values": [["유보"]]}, {"notes": []},
        ])
        doc = client.read_document({"id": "f", "mimeType": "application/vnd.google-apps.spreadsheet"})
        self.assertIn("왜 달라졌는지", doc["extracted_text"])
        self.assertIn("유보", doc["extracted_text"])
        self.assertIn("'교수''s 기준'!A1:B2", runner.call_args_list[1].args[0])
        self.assertEqual(doc["extraction"]["tab_ids"], ["1", "2"])
        row = normalize_drive_document({"id": "f", "mimeType": "application/vnd.google-apps.spreadsheet"}, doc)
        self.assertEqual(row["authorship"], "unverified")
        self.assertEqual(row["authored_text"], "")
        self.assertEqual(row["metadata"]["extraction"], doc["extraction"])

    def test_sheet_chunking_does_not_stop_at_empty_rows(self):
        client, runner = self.client([
            {"sheets": [{"properties": {"title": "all", "sheetId": 0, "gridProperties": {"rowCount": 1001, "columnCount": 1}}}]},
            {"values": None}, {"notes": None}, {"values": [["last row"]]}, {"notes": []},
        ])
        doc = client.read_document({"id": "f", "mimeType": "application/vnd.google-apps.spreadsheet"})
        self.assertIn("[row 1001] last row", doc["extracted_text"])
        self.assertIn("'all'!A1001:A1001", runner.call_args_list[3].args[0])

    def test_unsupported_size_and_large_grid_fail_explicitly(self):
        client, runner = self.client([])
        for meta, code in [({"mimeType": "application/zip"}, "format_unsupported"),
                           ({"mimeType": "application/pdf"}, "size_metadata_required"),
                           ({"mimeType": "application/pdf", "size": 100_000_000}, "budget_exceeded")]:
            with self.assertRaisesRegex(ConnectorError, code):
                client.read_document({"id": "f", **meta})
        runner.assert_not_called()
        client, runner = self.client([{"sheets": [{"properties": {"title": "huge", "gridProperties": {"rowCount": 3_000_000, "columnCount": 1}}}]}])
        with self.assertRaisesRegex(ConnectorError, "cell_budget_exceeded"):
            client.read_document({"id": "f", "mimeType": "application/vnd.google-apps.spreadsheet"})
        self.assertEqual(runner.call_count, 1)

    def test_download_private_directory_and_cleanup_and_no_arbitrary_output(self):
        paths = []
        def runner(argv, **kwargs):
            target = next(a[len("--out="):] for a in argv if a.startswith("--out="))
            paths.append(Path(target))
            self.assertEqual(Path(target).parent.stat().st_mode & 0o077, 0)
            self.assertIn("preexec_fn", kwargs)
            Path(target).write_text("첨부자료의 맥락입니다.")
            return subprocess.CompletedProcess([], 0, "{}", "")
        client = GogClient("prof@example.org", runner=runner)
        doc = client.read_document({"id": "f", "mimeType": "text/plain", "size": 30})
        self.assertIn("맥락", doc["extracted_text"])
        self.assertFalse(paths[0].exists())
        self.assertFalse(paths[0].parent.exists())
        with self.assertRaisesRegex(ConnectorError, "private_download_required"):
            client._call(("drive", "download"), ["f", "--out=/tmp/unsafe"])

    def test_docx_extracts_tables_headers_and_footnotes_without_author_inference(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        def runner(argv, **kwargs):
            target = next(a[len("--out="):] for a in argv if a.startswith("--out="))
            with zipfile.ZipFile(target, "w") as z:
                z.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body><w:p><w:r><w:t>본문</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>표 안의 기준</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')
                for part, text in (("header1", "머리말"), ("footnotes", "각주")):
                    z.writestr("word/" + part + ".xml", f'<w:root xmlns:w="{ns}"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:root>')
            return subprocess.CompletedProcess([], 0, "{}", "")
        client = GogClient("prof@example.org", runner=runner)
        meta = {"id": "f", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "size": 1000}
        row = normalize_drive_document(meta, client.read_document(meta))
        for text in ("본문", "표 안의 기준", "머리말", "각주"):
            self.assertIn(text, row["body"])
        self.assertEqual(row["authorship"], "unverified")
        self.assertEqual(row["authored_text"], "")

    def test_native_slides_preserves_relationship_order_groups_tables_and_notes(self):
        calls = []
        p = "http://schemas.openxmlformats.org/presentationml/2006/main"
        a = "http://schemas.openxmlformats.org/drawingml/2006/main"
        r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        rel = "http://schemas.openxmlformats.org/package/2006/relationships"
        def runner(argv, **kwargs):
            calls.append(argv)
            if "list-slides" in argv:
                return subprocess.CompletedProcess([], 0, json.dumps({"slideCount": 2, "slides": [{"objectId": "native2"}, {"objectId": "native1"}]}), "")
            target = str(Path(next(v[len("--out="):] for v in argv if v.startswith("--out="))).with_suffix(".pptx"))
            with zipfile.ZipFile(target, "w") as z:
                z.writestr("ppt/presentation.xml", f'<p:presentation xmlns:p="{p}" xmlns:r="{r}"><p:sldIdLst><p:sldId r:id="r2"/><p:sldId r:id="r1"/></p:sldIdLst></p:presentation>')
                z.writestr("ppt/_rels/presentation.xml.rels", f'<Relationships xmlns="{rel}"><Relationship Id="r1" Target="slides/slide1.xml"/><Relationship Id="r2" Target="slides/slide2.xml"/></Relationships>')
                for i, text in ((1, "나중 슬라이드"), (2, "먼저 표와 그룹")):
                    z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="{p}" xmlns:a="{a}"><p:grpSp><a:tbl><a:p><a:r><a:t>{text}</a:t></a:r></a:p></a:tbl></p:grpSp></p:sld>')
                z.writestr("ppt/slides/_rels/slide2.xml.rels", f'<Relationships xmlns="{rel}"><Relationship Id="n" Target="../notesSlides/notesSlide2.xml" Type="{r}/notesSlide"/></Relationships>')
                z.writestr("ppt/notesSlides/notesSlide2.xml", f'<p:notes xmlns:p="{p}" xmlns:a="{a}"><a:p><a:r><a:t>발표자 판단 기준</a:t></a:r></a:p></p:notes>')
            return subprocess.CompletedProcess([], 0, json.dumps({"path": target}), "")
        client = GogClient("prof@example.org", runner=runner)
        doc = client.read_document({"id": "f", "mimeType": "application/vnd.google-apps.presentation"})
        self.assertLess(doc["extracted_text"].index("먼저"), doc["extracted_text"].index("나중"))
        self.assertIn("발표자 판단 기준", doc["extracted_text"])
        self.assertEqual(doc["extraction"]["native_slide_ids"], ["native2", "native1"])
        self.assertIn("--format=pptx", calls[1])

    def test_download_result_cannot_redirect_to_other_local_file(self):
        client, _ = self.client([{"path": "/etc/passwd"}])
        with self.assertRaisesRegex(ConnectorError, "document_download_invalid"):
            client.read_document({"id": "f", "mimeType": "text/plain", "size": 10})

    def test_native_export_header_timeout_recovers_every_slide_with_honest_partial_scope(self):
        calls = []
        temporary = []
        inventory = {"presentationId": "f", "slideCount": 2, "slides": [{"objectId": "s1"}, {"objectId": "s2"}]}
        def runner(argv, **kwargs):
            calls.append(argv[4:6])
            if "download" in argv:
                target = Path(next(x[len("--out="):] for x in argv if x.startswith("--out=")))
                temporary.append(target.parent)
                target.write_bytes(b"unfinished")
                return subprocess.CompletedProcess([], 1, "", "net/http: timeout awaiting response headers: https://private?token=SECRET")
            if "list-slides" in argv:
                data = inventory
            elif "read-slide" in argv:
                sid = argv[-1]
                data = {"presentationId": "f", "slideObjectId": sid, "slideNumber": int(sid[-1]),
                        "textElements": [{"objectId": "t", "text": "첫 본문"}] if sid == "s1" else None,
                        "notes": "둘째 발표자 노트" if sid == "s2" else "", "images": [{"contentUrl": "https://private?token=SECRET"}]}
            else:
                data = {"file": {"id": "f", "modifiedTime": "unchanged"}}
            return subprocess.CompletedProcess([], 0, json.dumps(data), "")
        client = GogClient("prof@example.org", runner=runner)
        meta = {"id": "f", "mimeType": "application/vnd.google-apps.presentation", "modifiedTime": "unchanged"}
        document = client.read_document(meta)
        self.assertEqual(document["extraction"]["native_slide_ids"], ["s1", "s2"])
        self.assertEqual(document["extraction"]["slide_count"], 2)
        self.assertTrue(document["extraction"]["partial_text"])
        self.assertIn("group_and_table_text_not_extracted", document["extraction"]["limitations"])
        self.assertIn("첫 본문", document["extracted_text"])
        self.assertIn("둘째 발표자 노트", document["extracted_text"])
        self.assertNotIn("SECRET", json.dumps(document))
        self.assertEqual(calls.count(["slides", "read-slide"]), 2)
        self.assertEqual(calls.count(["slides", "list-slides"]), 2)
        self.assertFalse(temporary[0].exists())
        source = normalize_drive_document(meta, document)
        self.assertEqual((source["authorship"], source["authored_text"]), ("unverified", ""))

    def test_native_export_size_limit_recovers_native_text_without_claiming_full_deck_content(self):
        inventory = {"presentationId": "f", "slideCount": 1, "slides": [{"objectId": "s1"}]}
        meta = {"id": "f", "mimeType": "application/vnd.google-apps.presentation", "modifiedTime": "unchanged"}
        slide = {"presentationId": "f", "slideObjectId": "s1", "slideNumber": 1,
                 "textElements": [{"text": "Exact native shape text"}], "notes": "Exact speaker notes"}
        responses = [inventory,
                     subprocess.CompletedProcess([], 6, "", "Google API error (403 exportSizeLimitExceeded): This file is too large to be exported. PRIVATE"),
                     slide, inventory, {"file": meta}]
        runner = Mock(side_effect=[value if isinstance(value, subprocess.CompletedProcess)
                                  else subprocess.CompletedProcess([], 0, json.dumps(value), "") for value in responses])
        document = GogClient("prof@example.org", runner=runner).read_document(meta)
        self.assertEqual(document["extraction"]["fallback_reason"], "gog_native_export_size_limit")
        self.assertTrue(document["extraction"]["partial_text"])
        self.assertEqual(document["extraction"]["native_slide_ids"], ["s1"])
        self.assertIn("group_and_table_text_not_extracted", document["extraction"]["limitations"])
        self.assertIn("Exact native shape text", document["extracted_text"])
        self.assertIn("Exact speaker notes", document["extracted_text"])
        self.assertNotIn("PRIVATE", json.dumps(document))
        self.assertEqual(runner.call_count, 5)

    def test_native_slide_fallback_requires_specific_export_failure(self):
        for error, expected in [("invalid_grant timeout awaiting response headers", "gog_auth_required"),
                                ("403 Forbidden", "source_access_denied"), ("connection reset by peer", "gog_read_failed"),
                                ("403 rateLimitExceeded exportSizeLimitExceeded", "gog_rate_limited"),
                                ("403 dailyLimitExceeded", "gog_quota_exceeded"),
                                ("403 insufficientFilePermissions exportSizeLimitExceeded", "source_access_denied"),
                                ("403 download_restricted_for_revision", "source_download_restricted"),
                                ("403 fileNotExportable", "gog_export_unsupported")]:
            with self.subTest(error=error):
                client, runner = self.client([{"slideCount": 1, "slides": [{"objectId": "s1"}]}])
                runner.side_effect = [subprocess.CompletedProcess([], 0, json.dumps({"slideCount": 1, "slides": [{"objectId": "s1"}]}), ""),
                                      subprocess.CompletedProcess([], 1, "", error)]
                with self.assertRaisesRegex(ConnectorError, expected):
                    client.read_document({"id": "f", "mimeType": "application/vnd.google-apps.presentation"})
                self.assertEqual(runner.call_count, 2)

    def test_native_slide_fallback_never_returns_incomplete_or_changed_deck(self):
        inventory = {"presentationId": "f", "slideCount": 1, "slides": [{"objectId": "s1"}]}
        slide = {"presentationId": "f", "slideObjectId": "s1", "slideNumber": 1, "textElements": [], "notes": "note"}
        for response, final, expected in [
            (subprocess.CompletedProcess([], 1, "", "connection reset"), inventory, "gog_read_failed"),
            ({**slide, "slideObjectId": "wrong"}, inventory, "document_slide_response_invalid"),
            (slide, {**inventory, "slides": [{"objectId": "replacement"}]}, "document_slide_inventory_changed"),
        ]:
            with self.subTest(expected=expected):
                client, runner = self.client([])
                values = [inventory, response, final]
                runner.side_effect = [x if isinstance(x, subprocess.CompletedProcess) else subprocess.CompletedProcess([], 0, json.dumps(x), "") for x in values]
                with self.assertRaisesRegex(ConnectorError, expected):
                    client._read_native_slide_text({"id": "f"}, client._call(("slides", "list-slides"), ["f"]))
        client, _ = self.client([slide, inventory, {"file": {"id": "f", "modifiedTime": "later"}}])
        with self.assertRaisesRegex(ConnectorError, "document_changed_during_read"):
            client._read_native_slide_text({"id": "f", "modifiedTime": "before"}, inventory)

    def test_scanned_pdf_requires_ocr_and_temp_is_removed_on_failure(self):
        paths = []
        def download(argv, **kwargs):
            target = next(a[len("--out="):] for a in argv if a.startswith("--out="))
            paths.append(Path(target))
            Path(target).write_bytes(b"%PDF-1.7 fake")
            return subprocess.CompletedProcess([], 0, "{}", "")
        def convert(argv, **kwargs):
            Path(argv[-1]).write_text("\f")
            return subprocess.CompletedProcess([], 0, "", "")
        with patch("tools.cha_philosophy.connectors.subprocess.run", side_effect=convert):
            with self.assertRaisesRegex(ConnectorError, "requires_ocr"):
                GogClient("prof@example.org", runner=download).read_document({"id": "f", "mimeType": "application/pdf", "size": 20})
        self.assertFalse(paths[0].parent.exists())


class FakeResponse(io.BytesIO):
    def __init__(self, data):
        super().__init__(json.dumps(data).encode())


class GraphTests(unittest.TestCase):
    def client(self, responses):
        opener = Mock()
        opener.open.side_effect = [r if isinstance(r, Exception) else FakeResponse(r) for r in responses]
        sleeps = []
        return TeamsGraphClient("FAKE-SECRET", opener=opener, sleep=sleeps.append), opener, sleeps

    def test_user_chat_inventory_follows_all_pages_under_exact_verified_user(self):
        next_url=TeamsGraphClient.BASE+"/users/prof%40example.org/chats?$skiptoken=page2"
        client,opener,_=self.client([{"value":[{"id":"a"}],"@odata.nextLink":next_url},{"value":[{"id":"b"}]}])
        progress=SyncProgress()
        rows=list(client.list_user_chats("prof@example.org",progress=progress))
        self.assertEqual([r["id"] for r in rows],["a","b"])
        self.assertTrue(progress.complete)
        self.assertEqual(progress.pages,2)
        self.assertEqual(opener.open.call_args_list[0].args[0].method,"GET")
        self.assertIn("/users/prof%40example.org/chats?$top=50",opener.open.call_args_list[0].args[0].full_url)
        self.assertNotIn("$expand",opener.open.call_args_list[0].args[0].full_url)

    def test_chat_page_cap_resume_is_scope_checked_against_original_chat(self):
        next_url=TeamsGraphClient.BASE+"/chats/chat/messages?$skiptoken=p2"
        client,opener,_=self.client([{"value":[graph_message("a")],"@odata.nextLink":next_url},{"value":[graph_message("b")]}])
        first=SyncProgress()
        self.assertEqual(len(list(client.iter_chat_messages("chat",progress=first,max_pages=1))),1)
        self.assertFalse(first.complete)
        self.assertEqual(first.remaining_page_token,next_url)
        resumed=SyncProgress()
        self.assertEqual([r["id"] for r in client.iter_chat_messages("chat",progress=resumed,start_page=first.remaining_page_token)],["b"])
        self.assertTrue(resumed.complete)
        self.assertEqual(opener.open.call_args.args[0].full_url,next_url)
        for bad in (TeamsGraphClient.BASE+"/chats/other/messages?$skiptoken=p2","https://attacker.invalid/v1.0/chats/chat/messages"):
            client,opener,_=self.client([])
            with self.assertRaises(ConnectorError):list(client.iter_chat_messages("chat",start_page=bad))
            opener.open.assert_not_called()

    def test_user_chat_nextlink_cannot_switch_professor(self):
        bad=TeamsGraphClient.BASE+"/users/other/chats?$skiptoken=p2"
        client,opener,_=self.client([{"value":[],"@odata.nextLink":bad}])
        with self.assertRaisesRegex(ConnectorError,"scope_changed"):
            list(client.list_user_chats("prof"))
        self.assertEqual(opener.open.call_count,1)
        client,opener,_=self.client([])
        with self.assertRaisesRegex(ConnectorError,"scope_changed"):
            list(client.list_user_chats("prof",start_page=bad))
        opener.open.assert_not_called()

    def test_chat_message_failure_after_first_page_exposes_failed_cursor(self):
        second=TeamsGraphClient.BASE+"/chats/c/messages?$skiptoken=second"
        error=urllib.error.HTTPError(second,403,"private response",{},None)
        client,_,_=self.client([{"value":[graph_message("a")],"@odata.nextLink":second},error])
        progress=SyncProgress();seen=[]
        with self.assertRaisesRegex(ConnectorError,"teams_access_denied"):
            for row in client.iter_chat_messages("c",progress=progress):seen.append(row["id"])
        self.assertEqual(seen,["a"])
        self.assertEqual(progress.remaining_page_token,second)
        self.assertFalse(progress.complete)

    def test_falsey_malformed_chat_nextlink_cannot_prove_completion(self):
        for invalid in ([],{},0,False):
            client,_,_=self.client([{"value":[],"@odata.nextLink":invalid}])
            progress=SyncProgress()
            with self.assertRaisesRegex(ConnectorError,"page_token_invalid"):
                list(client.iter_chat_messages("chat",progress=progress))
            self.assertFalse(progress.complete)

    def test_expanded_200_replies_follow_separate_nextlink_and_root_nextlink(self):
        base = TeamsGraphClient.BASE + "/teams/team/channels/channel/messages"
        root = graph_message("root", replies=[graph_message(str(i)) for i in range(200)])
        root["replies@odata.nextLink"] = base + "/root/replies?$skiptoken=reply-page"
        client, opener, _ = self.client([
            {"value": [root], "@odata.nextLink": base + "?$skiptoken=root-page"},
            {"value": [graph_message("201")]}, {"value": [graph_message("root2", replies=[])]},
        ])
        progress = SyncProgress()
        rows = list(client.iter_channel_messages("team", "channel", progress=progress))
        self.assertEqual(len(rows), 203)
        self.assertEqual(rows[201]["_parent_id"], "root")
        self.assertTrue(progress.complete)
        self.assertEqual(opener.open.call_count, 3)

    def test_missing_expand_falls_back_to_reply_list(self):
        client, opener, _ = self.client([{"value": [graph_message("root")]}, {"value": [graph_message("reply")]}])
        rows = list(client.iter_channel_messages("team", "channel"))
        self.assertEqual(rows[-1]["_parent_id"], "root")
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("/root/replies?$top=50"))

    def test_equivalent_encoded_channel_nextlink_is_accepted(self):
        base = TeamsGraphClient.BASE + "/teams/t/channels/19:abc@thread/messages"
        root = graph_message("r", replies=[])
        root["replies@odata.nextLink"] = base + "/r/replies?$skiptoken=x"
        client, opener, _ = self.client([{"value": [root]}, {"value": [graph_message("reply")]}])
        rows = list(client.iter_channel_messages("t", "19:abc@thread"))
        self.assertEqual(len(rows), 2)

    def test_reply_fetch_failure_keeps_pending_root_and_failed_cursor(self):
        base = TeamsGraphClient.BASE + "/teams/t/channels/c/messages/r/replies?$skiptoken=x"
        root = graph_message("r", replies=[])
        root["replies@odata.nextLink"] = base
        error = urllib.error.HTTPError(base, 403, "", {}, None)
        client, _, _ = self.client([{"value": [root]}, error])
        progress = SyncProgress()
        with self.assertRaises(ConnectorError):
            list(client.iter_channel_messages("t", "c", progress=progress))
        self.assertEqual(progress.pending_ids, ["r"])
        self.assertEqual(progress.remaining_page_token, base)
        self.assertFalse(progress.complete)

    def test_cross_host_and_other_container_nextlinks_rejected_before_fetch(self):
        for next_url in ("https://attacker.invalid/v1.0/chats/c/messages", TeamsGraphClient.BASE + "/chats/other/messages"):
            client, opener, _ = self.client([{"value": [], "@odata.nextLink": next_url}])
            with self.assertRaises(ConnectorError):
                list(client.iter_chat_messages("c"))
            self.assertEqual(opener.open.call_count, 1)

    def test_reply_scope_cannot_be_switched(self):
        root = graph_message("root", replies=[])
        root["replies@odata.nextLink"] = TeamsGraphClient.BASE + "/teams/t/channels/c/messages/other/replies"
        client, opener, _ = self.client([{"value": [root]}])
        with self.assertRaisesRegex(ConnectorError, "reply_nextlink_scope_changed"):
            list(client.iter_channel_messages("t", "c"))
        self.assertEqual(opener.open.call_count, 1)

    def test_throttling_retry_after_and_safe_permission_error(self):
        error = urllib.error.HTTPError("https://graph.microsoft.com", 429, "secret-body", {"Retry-After": "2"}, None)
        client, opener, sleeps = self.client([error, {"value": []}])
        self.assertEqual(list(client.iter_chat_messages("c")), [])
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(opener.open.call_args.args[0].method, "GET")
        error = urllib.error.HTTPError("https://graph.microsoft.com", 403, "FAKE-SECRET", {}, None)
        client, _, _ = self.client([error])
        with self.assertRaisesRegex(ConnectorError, "teams_access_denied"):
            list(client.iter_chat_messages("c"))

    def test_invalid_urls_and_long_retry_return_to_scheduler(self):
        client, opener, _ = self.client([])
        for url in ("http://graph.microsoft.com/v1.0/me", "https://graph.microsoft.com@evil.invalid/v1.0/me", "https://graph.microsoft.com/beta/me", "https://["):
            with self.assertRaises(ConnectorError):
                client.get_json(url)
        opener.open.assert_not_called()
        error = urllib.error.HTTPError("https://graph.microsoft.com", 429, "", {"Retry-After": "120"}, None)
        client, _, sleeps = self.client([error])
        with self.assertRaises(ConnectorError) as ctx:
            list(client.iter_chat_messages("c"))
        self.assertEqual(ctx.exception.retry_after, 120)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
