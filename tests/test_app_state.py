import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as app_module


class EmptyDuplicates:
    def __contains__(self, domain):
        return False


class AppStateTests(unittest.TestCase):
    def test_urls_are_normalized_to_bare_domains(self):
        state = app_module.AppState()
        result = state.add_batch(
            "DE",
            "https://www.Example.de/path?q=1\nexample.de/other\nbad..de",
            EmptyDuplicates(),
        )
        self.assertEqual(result["loaded"], 1)
        self.assertEqual(result["invalid_skipped"], 1)
        self.assertEqual(state.queue[0].domain, "example.de")

    def test_processing_item_is_in_remaining_count(self):
        state = app_module.AppState()
        state.add_batch("DE", "example.de", EmptyDuplicates())
        state.get_next_item()
        snapshot = state.get_snapshot()
        self.assertEqual(snapshot["counts"]["remaining"], 1)
        self.assertEqual(snapshot["counts"]["queued"], 0)

    def test_pending_result_is_pinned_even_outside_recent_window(self):
        state = app_module.AppState()
        state.results.append(
            app_module.ResultRow(
                title="OLD",
                domain="still-checking.example",
                status=app_module.PENDING_AI_STATUS,
                ai_status="CHECKING",
            )
        )
        for index in range(305):
            state.results.append(
                app_module.ResultRow(
                    title="DONE",
                    domain=f"done-{index}.example",
                    status="BAD",
                )
            )

        snapshot = state.get_snapshot()

        self.assertEqual(snapshot["results"][0]["domain"], "still-checking.example")
        self.assertEqual(snapshot["results"][0]["status"], app_module.PENDING_AI_STATUS)
        self.assertEqual(snapshot["counts"]["pending_ai"], 1)

    def test_promoted_completed_result_moves_to_top_of_recent_results(self):
        state = app_module.AppState()
        old_row = app_module.ResultRow(title="OLD", domain="old.example", status="PENDING:AI")
        newer_row = app_module.ResultRow(title="NEW", domain="new.example", status="BAD")
        state.results.extend([old_row, newer_row])

        old_row.status = "GOOD"
        state.promote_result(old_row)
        snapshot = state.get_snapshot()

        self.assertEqual(snapshot["results"][-1]["domain"], "old.example")

    def test_webarchive_queue_status_is_queued_before_worker_starts(self):
        test_state = app_module.AppState()
        row = app_module.ResultRow(title="DE", domain="example.de", status="GOOD", ai_reason="ok")
        test_state.results.append(row)

        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "WEBARCHIVE_SPAM_ENABLED", True),
            patch.object(app_module.webarchive_tasks, "put") as put_task,
        ):
            queued = app_module.queue_webarchive_check(row)

        self.assertTrue(queued)
        self.assertEqual(row.status, app_module.PENDING_WEBARCHIVE_STATUS)
        self.assertEqual(row.webarchive_status, "QUEUED")
        put_task.assert_called_once()

    def test_finalize_ai_task_promotes_completed_row(self):
        test_state = app_module.AppState()
        row = app_module.ResultRow(
            title="DE",
            domain="example.de",
            status=app_module.PENDING_AI_STATUS,
            ai_status="QUEUED",
        )
        newer_row = app_module.ResultRow(title="DE", domain="newer.example", status="BAD")
        test_state.results.extend([row, newer_row])

        class FakeChecker:
            ready = True
            last_error = ""
            model = "gpt-5.6-terra"
            _model_access_checked = True
            model_notice = ""
            strict_mode = False
            strict_unique_deficit = 1
            strict_article_deficit = 1
            freshness_filter_enabled = True
            freshness_cutoff_year = 2016
            freshness_max_old_share_percent = 50

            def evaluate(self, **kwargs):
                return app_module.DomainVerdict(
                    verdict="PASS",
                    status="GOOD",
                    reason="ok",
                    locale="DE",
                    unique_quality=9,
                    article_links=5,
                    homepage_links=5,
                    anchor_risk="CLEAN",
                    model="gpt-5.6-terra",
                    api_calls=1,
                )

        task = app_module.AITask(
            row=row,
            majestic_status="GOOD",
            backlinks_report={"rows": []},
            historic_pages={"rows": []},
            fresh_anchors={"rows": []},
            historic_anchors={"rows": []},
            settings={
                "quality_model": "gpt-5.6-terra",
                "strict_mode": False,
                "strict_unique_deficit": 1,
                "strict_article_deficit": 1,
                "freshness_filter_enabled": True,
                "freshness_cutoff_year": 2016,
                "freshness_max_old_share_percent": 50,
            },
        )

        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "queue_webarchive_check", return_value=False),
            patch.object(app_module.duplicate_store, "add") as add_duplicate,
            patch.object(app_module.sheets, "append_good") as append_good,
            patch.object(app_module, "write_results_csv") as write_csv,
        ):
            app_module.finalize_ai_task(task, FakeChecker())

        self.assertEqual(row.status, "GOOD")
        self.assertEqual(row.ai_status, "OK 1")
        self.assertIs(test_state.results[-1], row)
        add_duplicate.assert_called_once_with("example.de")
        append_good.assert_called_once_with(row)
        write_csv.assert_called_once()

    def test_finalize_ai_good_waits_for_webarchive_before_outputs(self):
        test_state = app_module.AppState()
        row = app_module.ResultRow(
            title="DE",
            domain="example.de",
            status=app_module.PENDING_AI_STATUS,
            ai_status="QUEUED",
        )
        test_state.results.append(row)

        class FakeChecker:
            ready = True
            last_error = ""
            model = "gpt-5.6-terra"
            _model_access_checked = True
            model_notice = ""
            strict_mode = False
            strict_unique_deficit = 1
            strict_article_deficit = 1
            freshness_filter_enabled = True
            freshness_cutoff_year = 2016
            freshness_max_old_share_percent = 50

            def evaluate(self, **kwargs):
                return app_module.DomainVerdict(
                    verdict="PASS",
                    status="GOOD",
                    reason="ok",
                    locale="DE",
                    unique_quality=9,
                    article_links=5,
                    homepage_links=5,
                    anchor_risk="CLEAN",
                    model="gpt-5.6-terra",
                    api_calls=1,
                )

        task = app_module.AITask(
            row=row,
            majestic_status="GOOD",
            backlinks_report={"rows": []},
            historic_pages={"rows": []},
            fresh_anchors={"rows": []},
            historic_anchors={"rows": []},
            settings={
                "quality_model": "gpt-5.6-terra",
                "strict_mode": False,
                "strict_unique_deficit": 1,
                "strict_article_deficit": 1,
                "freshness_filter_enabled": True,
                "freshness_cutoff_year": 2016,
                "freshness_max_old_share_percent": 50,
            },
        )

        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "WEBARCHIVE_SPAM_ENABLED", True),
            patch.object(app_module.webarchive_tasks, "put") as put_archive_task,
            patch.object(app_module.duplicate_store, "add") as add_duplicate,
            patch.object(app_module.sheets, "append_good") as append_good,
            patch.object(app_module, "write_results_csv") as write_csv,
        ):
            app_module.finalize_ai_task(task, FakeChecker())

        self.assertEqual(row.status, app_module.PENDING_WEBARCHIVE_STATUS)
        self.assertEqual(row.ai_status, "OK 1")
        self.assertEqual(row.webarchive_status, "QUEUED")
        put_archive_task.assert_called_once()
        add_duplicate.assert_not_called()
        append_good.assert_not_called()
        write_csv.assert_called_once()

    def test_finalize_webarchive_task_switches_to_checking_and_promotes(self):
        test_state = app_module.AppState()
        row = app_module.ResultRow(
            title="DE",
            domain="example.de",
            status=app_module.PENDING_WEBARCHIVE_STATUS,
            ai_reason="ai ok",
            webarchive_status="QUEUED",
        )
        newer_row = app_module.ResultRow(title="DE", domain="newer.example", status="BAD")
        test_state.results.extend([row, newer_row])

        class ArchiveResult:
            checked = True
            spam = False
            snapshots_checked = 2
            reason = "clean"

        def fake_archive_check(*args, **kwargs):
            self.assertEqual(row.webarchive_status, "CHECKING")
            return ArchiveResult()

        task = app_module.WebArchiveTask(row=row, original_status="GOOD", original_reason="ai ok")
        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "check_webarchive_spam", side_effect=fake_archive_check),
            patch.object(app_module.duplicate_store, "add") as add_duplicate,
            patch.object(app_module.sheets, "append_good") as append_good,
            patch.object(app_module, "write_results_csv") as write_csv,
        ):
            app_module.finalize_webarchive_task(task)

        self.assertEqual(row.status, "GOOD")
        self.assertEqual(row.webarchive_status, "OK 2")
        self.assertIs(test_state.results[-1], row)
        add_duplicate.assert_called_once_with("example.de")
        append_good.assert_called_once_with(row)
        write_csv.assert_called_once()

    def test_webarchive_timeout_is_requeued_without_final_error(self):
        test_state = app_module.AppState()
        row = app_module.ResultRow(
            title="CH",
            domain="bmw-abo.ch",
            status=app_module.PENDING_WEBARCHIVE_STATUS,
            ai_reason="ai ok",
            webarchive_status="QUEUED",
        )
        test_state.results.append(row)

        class ArchiveResult:
            checked = False
            spam = False
            snapshots_found = 0
            snapshots_checked = 0
            errors = ["CDX latest http://bmw-abo.ch/: TimeoutError"]

            @property
            def reason(self):
                return "WebArchive не проверен: CDX latest http://bmw-abo.ch/: TimeoutError"

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.daemon = False

            def start(self):
                self.callback()

        task = app_module.WebArchiveTask(row=row, original_status="GOOD", original_reason="ai ok")
        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "check_webarchive_spam", return_value=ArchiveResult()),
            patch.object(app_module.threading, "Timer", ImmediateTimer),
            patch.object(app_module.webarchive_tasks, "put") as put_task,
            patch.object(app_module.duplicate_store, "add") as add_duplicate,
            patch.object(app_module.sheets, "append_good") as append_good,
            patch.object(app_module, "write_results_csv") as write_csv,
        ):
            app_module.finalize_webarchive_task(task)

        self.assertEqual(row.status, app_module.PENDING_WEBARCHIVE_STATUS)
        self.assertEqual(row.webarchive_status, "QUEUED RETRY 1")
        self.assertEqual(row.error, "")
        self.assertEqual(row.ai_reason, "ai ok")
        put_task.assert_called_once_with(task)
        add_duplicate.assert_not_called()
        append_good.assert_not_called()
        write_csv.assert_called_once()

    def test_webarchive_cdx_urlerror_is_requeued_without_final_error(self):
        test_state = app_module.AppState()
        row = app_module.ResultRow(
            title="CH",
            domain="nuitdesmusees-ne.ch",
            status=app_module.PENDING_WEBARCHIVE_STATUS,
            ai_reason="ai ok",
            webarchive_status="QUEUED",
        )
        test_state.results.append(row)

        class ArchiveResult:
            checked = False
            spam = False
            snapshots_found = 0
            snapshots_checked = 0
            errors = ["CDX latest http://nuitdesmusees-ne.ch/: URLError"]

            @property
            def reason(self):
                return "WebArchive РЅРµ РїСЂРѕРІРµСЂРµРЅ: CDX latest http://nuitdesmusees-ne.ch/: URLError"

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.daemon = False

            def start(self):
                self.callback()

        task = app_module.WebArchiveTask(row=row, original_status="GOOD", original_reason="ai ok")
        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "check_webarchive_spam", return_value=ArchiveResult()),
            patch.object(app_module.threading, "Timer", ImmediateTimer),
            patch.object(app_module.webarchive_tasks, "put") as put_task,
            patch.object(app_module.duplicate_store, "add") as add_duplicate,
            patch.object(app_module.sheets, "append_good") as append_good,
            patch.object(app_module, "write_results_csv") as write_csv,
        ):
            app_module.finalize_webarchive_task(task)

        self.assertEqual(row.status, app_module.PENDING_WEBARCHIVE_STATUS)
        self.assertEqual(row.webarchive_status, "QUEUED RETRY 1")
        self.assertEqual(row.error, "")
        self.assertEqual(row.ai_reason, "ai ok")
        put_task.assert_called_once_with(task)
        add_duplicate.assert_not_called()
        append_good.assert_not_called()
        write_csv.assert_called_once()

    def test_low_context_status_reaches_free_backlink_gate(self):
        self.assertIn("REVIEW:L", app_module.AI_ELIGIBLE_MAJESTIC_STATUSES)

    def test_context_base_check_counts_deleted_dofollow_links(self):
        class FakeDriver:
            current_url = ""
            title = ""

            def get(self, url):
                self.current_url = url

        rows = [object() for _ in range(6)]

        def row_json(_row):
            return {
                "flagDeleted": True,
                "flagNoFollow": False,
                "sourceOutgoingExternalLinks": 8,
                "targetUrl": "https://deleted-live.example/",
                "linkDensity": 10,
            }

        with (
            patch.object(app_module, "try_enable_dofollow"),
            patch.object(app_module, "get_rows", return_value=rows),
            patch.object(app_module, "extract_row_json", side_effect=row_json),
            patch.object(app_module, "is_logged_out_page", return_value=False),
            patch.object(app_module, "is_network_error_page", return_value=False),
        ):
            status = app_module.majestic_base_check(FakeDriver(), "deleted-live.example")

        self.assertEqual(status, "GOOD")

    def test_context_base_check_returns_named_density_stop(self):
        class FakeDriver:
            current_url = ""
            title = ""

            def get(self, url):
                self.current_url = url

        rows = [object() for _ in range(6)]

        def row_json(_row):
            return {
                "flagNoFollow": False,
                "sourceOutgoingExternalLinks": 8,
                "targetUrl": "https://density-stop.example/",
                "linkDensity": 80,
            }

        with (
            patch.object(app_module, "try_enable_dofollow"),
            patch.object(app_module, "get_rows", return_value=rows),
            patch.object(app_module, "extract_row_json", side_effect=row_json),
            patch.object(app_module, "is_logged_out_page", return_value=False),
            patch.object(app_module, "is_network_error_page", return_value=False),
        ):
            status = app_module.majestic_base_check(FakeDriver(), "density-stop.example")

        self.assertEqual(status, "BAD:CONTEXT_DENSITY")
        self.assertIn("link density", app_module.context_status_reason(status))

    def test_context_base_check_returns_named_outbound_stop(self):
        class FakeDriver:
            current_url = ""
            title = ""

            def get(self, url):
                self.current_url = url

        rows = [object() for _ in range(6)]

        def row_json(_row):
            return {
                "flagNoFollow": False,
                "sourceOutgoingExternalLinks": 500,
                "targetUrl": "https://outbound-stop.example/",
                "linkDensity": 10,
            }

        with (
            patch.object(app_module, "try_enable_dofollow"),
            patch.object(app_module, "get_rows", return_value=rows),
            patch.object(app_module, "extract_row_json", side_effect=row_json),
            patch.object(app_module, "is_logged_out_page", return_value=False),
            patch.object(app_module, "is_network_error_page", return_value=False),
        ):
            status = app_module.majestic_base_check(FakeDriver(), "outbound-stop.example")

        self.assertEqual(status, "BAD:CONTEXT_OUTBOUND")
        self.assertIn("исходящих", app_module.context_status_reason(status))

    def test_context_base_check_returns_named_homepage_share_stop(self):
        class FakeDriver:
            current_url = ""
            title = ""

            def get(self, url):
                self.current_url = url

        rows = [object() for _ in range(6)]

        def row_json(_row):
            return {
                "flagNoFollow": False,
                "sourceOutgoingExternalLinks": 8,
                "targetUrl": "https://homepage-stop.example/inner-page",
                "linkDensity": 10,
            }

        with (
            patch.object(app_module, "try_enable_dofollow"),
            patch.object(app_module, "get_rows", return_value=rows),
            patch.object(app_module, "extract_row_json", side_effect=row_json),
            patch.object(app_module, "is_logged_out_page", return_value=False),
            patch.object(app_module, "is_network_error_page", return_value=False),
        ):
            status = app_module.majestic_base_check(FakeDriver(), "homepage-stop.example")

        self.assertEqual(status, "BAD:CONTEXT_HOMEPAGE_SHARE")
        self.assertIn("35%", app_module.context_status_reason(status))

    def test_good_old_is_sent_to_ai_pipeline(self):
        self.assertIn("GOOD", app_module.AI_ELIGIBLE_MAJESTIC_STATUSES)
        self.assertIn("GOOD OLD", app_module.AI_ELIGIBLE_MAJESTIC_STATUSES)

    def test_report_retry_does_not_navigate_back_to_context(self):
        calls = []

        def collector():
            calls.append("backlinks")
            raise app_module.MajesticReportError("table missing")

        with (
            patch.object(app_module, "MAX_REPORT_RETRIES", 3),
            patch.object(app_module, "retry_delay", lambda attempt: None),
        ):
            with self.assertRaises(app_module.MajesticReportError):
                app_module.collect_majestic_stage(object(), "drop.de", "Backlinks", collector)

        self.assertEqual(calls, ["backlinks", "backlinks", "backlinks"])

    def test_new_batch_after_completed_queue_reuses_healthy_browser(self):
        test_state = app_module.AppState()
        test_state.browser_ready = True
        test_state.has_started_once = True
        test_state.add_batch("DE", "saved-one.de\nsaved-two.de", EmptyDuplicates())

        class FakeSession:
            restart_calls = 0

            def is_healthy(self):
                return True

            def restart_preserving_app_state(self):
                self.restart_calls += 1
                with test_state.lock:
                    test_state.browser_ready = True
                return True

        fake_session = FakeSession()

        class ImmediateThread:
            def __init__(self, target, daemon=True):
                self.target = target

            def start(self):
                self.target()

        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "session", fake_session),
            patch.object(app_module.threading, "Thread", ImmediateThread),
        ):
            response = app_module.app.test_client().post("/api/start")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["recovering_browser"])
        self.assertEqual(fake_session.restart_calls, 0)
        self.assertEqual(len(test_state.queue), 2)
        self.assertTrue(test_state.running)
        self.assertFalse(test_state.browser_recovery_in_progress)

    def test_repeated_start_recovers_unhealthy_browser_and_keeps_queue(self):
        test_state = app_module.AppState()
        test_state.browser_ready = True
        test_state.has_started_once = True
        test_state.add_batch("DE", "saved-one.de\nsaved-two.de", EmptyDuplicates())

        class FakeSession:
            restart_calls = 0

            def is_healthy(self):
                return False

            def restart_preserving_app_state(self):
                self.restart_calls += 1
                with test_state.lock:
                    test_state.browser_ready = True
                return True

        fake_session = FakeSession()

        class ImmediateThread:
            def __init__(self, target, daemon=True):
                self.target = target

            def start(self):
                self.target()

        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "session", fake_session),
            patch.object(app_module.threading, "Thread", ImmediateThread),
        ):
            response = app_module.app.test_client().post("/api/start")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["recovering_browser"])
        self.assertEqual(fake_session.restart_calls, 1)
        self.assertEqual(len(test_state.queue), 2)
        self.assertTrue(test_state.running)
        self.assertFalse(test_state.browser_recovery_in_progress)

    def test_moving_batch_changes_next_processing_order(self):
        state = app_module.AppState()
        duplicates = EmptyDuplicates()
        first = state.add_batch("FIRST", "first.example", duplicates)
        second = state.add_batch("SECOND", "second.example", duplicates)
        move = state.move_batch(second["batch_id"], "up")
        self.assertTrue(move["moved"])
        self.assertEqual(state.get_next_item().title, "SECOND")
        state.complete_item(state.current_item_id, "BAD")
        self.assertEqual(state.get_next_item().title, "FIRST")
        self.assertNotEqual(first["batch_id"], second["batch_id"])

    def test_add_domains_to_existing_batch_by_id(self):
        state = app_module.AppState()
        duplicates = EmptyDuplicates()
        first = state.add_batch("FIRST", "first.example", duplicates)
        second = state.add_batch("SECOND", "second.example", duplicates)

        stats = state.add_domains_to_batch(
            first["batch_id"],
            "https://www.extra.example/path\nsecond.example\nbad..example",
            duplicates,
        )

        self.assertEqual(stats["loaded"], 1)
        self.assertEqual(stats["duplicates_skipped"], 1)
        self.assertEqual(stats["invalid_skipped"], 1)
        first_domains = [
            item.domain
            for item in state.queue
            if item.batch_id == first["batch_id"] and item.state == "queued"
        ]
        self.assertEqual(first_domains, ["first.example", "extra.example"])
        self.assertEqual(state.get_next_item().domain, "first.example")
        state.complete_item(state.current_item_id, "BAD")
        self.assertEqual(state.get_next_item().domain, "extra.example")

    def test_add_domains_to_existing_batch_endpoint(self):
        test_state = app_module.AppState()
        duplicates = EmptyDuplicates()
        batch = test_state.add_batch("FIRST", "first.example", duplicates)
        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "duplicate_store", duplicates),
        ):
            response = app_module.app.test_client().post(
                f"/api/batches/{batch['batch_id']}/add-domains",
                json={"domains": "extra.example\nfirst.example"},
            )

        data = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["loaded"], 1)
        self.assertEqual(data["duplicates_skipped"], 1)
        self.assertEqual([item.domain for item in test_state.queue], ["first.example", "extra.example"])

    def test_prompt_files_persist_independently_from_app_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            prompt_files = {
                "link_prompt": root / "links.txt",
                "anchor_prompt": root / "anchors.txt",
            }
            for path in prompt_files.values():
                path.write_text("initial\n", encoding="utf-8")
            with patch.object(app_module, "PROMPT_FILES", prompt_files):
                app_module.save_active_prompts(
                    {"link_prompt": "saved links", "anchor_prompt": "saved anchors"}
                )
                app_module.AppState()
                values = app_module.read_active_prompts()
            self.assertEqual(values["link_prompt"], "saved links\n")
            self.assertEqual(values["anchor_prompt"], "saved anchors\n")

    def test_bulk_remove_from_duplicate_store(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "duplicates.txt"
            store = app_module.DuplicateStore(path)
            store.add("keep.example")
            store.add("remove.example")
            result = store.remove_many(
                "https://www.remove.example/path\nmissing.example\nremove.example"
            )
            self.assertEqual(result["requested"], 2)
            self.assertEqual(result["removed"], 1)
            self.assertEqual(result["not_found"], 1)
            self.assertIn("keep.example", store)
            self.assertNotIn("remove.example", store)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep.example\n")

    def test_clear_logs_endpoint_clears_server_state(self):
        test_state = app_module.AppState()
        test_state.append_log("old message")
        with patch.object(app_module, "state", test_state):
            response = app_module.app.test_client().post("/api/logs/clear")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(test_state.logs, [])

    def test_settings_endpoint_updates_custom_strict_deficits(self):
        test_state = app_module.AppState()
        original = (
            app_module.ai_checker.strict_mode,
            app_module.ai_checker.strict_unique_deficit,
            app_module.ai_checker.strict_article_deficit,
            app_module.ai_checker.freshness_filter_enabled,
            app_module.ai_checker.freshness_cutoff_year,
            app_module.ai_checker.freshness_max_old_share_percent,
            app_module.ai_checker.model,
            app_module.ai_checker._model_access_checked,
        )
        try:
            app_module.ai_checker._model_access_checked = True
            with patch.object(app_module, "state", test_state):
                response = app_module.app.test_client().post(
                    "/api/settings",
                    json={
                        "strict_mode": True,
                        "strict_unique_deficit": "2",
                        "strict_article_deficit": "3",
                        "freshness_filter_enabled": True,
                        "freshness_cutoff_year": "2017",
                        "freshness_max_old_share_percent": "40",
                        "quality_model": "gpt-5.6-terra",
                    },
                )
            data = response.get_json()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(data["strict_mode"])
            self.assertEqual(data["strict_unique_deficit"], 2)
            self.assertEqual(data["strict_article_deficit"], 3)
            self.assertTrue(data["freshness_filter_enabled"])
            self.assertEqual(data["freshness_cutoff_year"], 2017)
            self.assertEqual(data["freshness_max_old_share_percent"], 40)
            self.assertTrue(app_module.ai_checker.strict_mode)
            self.assertEqual(app_module.ai_checker.strict_unique_deficit, 2)
            self.assertEqual(app_module.ai_checker.strict_article_deficit, 3)
            self.assertTrue(app_module.ai_checker.freshness_filter_enabled)
            self.assertEqual(app_module.ai_checker.freshness_cutoff_year, 2017)
            self.assertEqual(app_module.ai_checker.freshness_max_old_share_percent, 40)
            self.assertEqual(data["quality_model"], "gpt-5.6-terra")
            self.assertEqual(app_module.ai_checker.model, "gpt-5.6-terra")
            self.assertFalse(app_module.ai_checker._model_access_checked)
        finally:
            (
                app_module.ai_checker.strict_mode,
                app_module.ai_checker.strict_unique_deficit,
                app_module.ai_checker.strict_article_deficit,
                app_module.ai_checker.freshness_filter_enabled,
                app_module.ai_checker.freshness_cutoff_year,
                app_module.ai_checker.freshness_max_old_share_percent,
                app_module.ai_checker.model,
                app_module.ai_checker._model_access_checked,
            ) = original

    def test_domain_name_precheck_result_fields_are_local_and_token_free(self):
        verdict = app_module.local_domain_name_precheck("cialisbuyonlinegf.com", "AT")
        self.assertIsNotNone(verdict)
        fields = app_module.ai_result_fields(verdict, majestic_status="LOCAL")
        self.assertEqual(verdict.status, "BAD:DOMAIN_NAME")
        self.assertEqual(fields["majestic_status"], "LOCAL")
        self.assertEqual(fields["ai_api_calls"], 0)
        self.assertEqual(fields["ai_early_stop_stage"], "local_domain_name")

    def test_google_sheets_good_row_is_compact(self):
        class Worksheet:
            def __init__(self):
                self.rows = []

            def append_row(self, values, value_input_option=None):
                self.rows.append((values, value_input_option))

        sink = app_module.GoogleSheetsSink()
        sink.ready = True
        sink.worksheet = Worksheet()
        row = app_module.ResultRow(
            title="HU export",
            domain="example.com",
            status="GOOD",
            ai_reason="Порог выполнен",
            locale="HU",
            locale_source="AI",
            unique_quality=10,
            article_links=7,
            homepage_links=6,
        )
        self.assertTrue(sink.append_good(row))
        self.assertEqual(
            sink.worksheet.rows[0][0],
            ["HU export", "example.com", "GOOD", "HU · AI", "U10 A7 H6", "Порог выполнен"],
        )

    def test_google_sheets_good_row_keeps_link_years_inside_metric(self):
        class Worksheet:
            def __init__(self):
                self.rows = []

            def append_row(self, values, value_input_option=None):
                self.rows.append((values, value_input_option))

        sink = app_module.GoogleSheetsSink()
        sink.ready = True
        sink.worksheet = Worksheet()
        row = app_module.ResultRow(
            title="TEST",
            domain="example.com",
            status="GOOD",
            ai_reason="ok",
            unique_quality=8,
            article_links=6,
            homepage_links=7,
            link_year_min=2017,
            link_year_max=2017,
            link_year_count=1,
        )
        self.assertTrue(sink.append_good(row))
        self.assertEqual(
            sink.worksheet.rows[0][0],
            ["TEST", "example.com", "GOOD", "", "U8 A6 H7 Y2017 1/8", "ok"],
        )

    def test_compact_metric_includes_partial_link_year_range(self):
        row = app_module.ResultRow(
            title="DE",
            domain="example.de",
            status="GOOD",
            unique_quality=13,
            article_links=5,
            homepage_links=12,
            link_year_min=2014,
            link_year_max=2021,
            link_year_count=6,
        )
        self.assertEqual(app_module.format_compact_metric(row), "U13 A5 H12 Y2014-2021 6/13")

    def test_google_sheets_cjk_locale_goes_to_archive(self):
        class Worksheet:
            def __init__(self):
                self.rows = []

            def append_row(self, values, value_input_option=None):
                self.rows.append((values, value_input_option))

        sink = app_module.GoogleSheetsSink()
        sink.ready = True
        sink.worksheet = Worksheet()
        sink.archive_worksheet = Worksheet()
        row = app_module.ResultRow(
            title="JP export",
            domain="example.jp",
            status="GOOD",
            ai_reason="ok",
            locale="JP",
            locale_source="AI",
            unique_quality=10,
            article_links=5,
            homepage_links=6,
        )
        self.assertTrue(sink.append_good(row))
        self.assertEqual(sink.worksheet.rows, [])
        self.assertEqual(sink.archive_worksheet.rows[0][0][1], "example.jp")

    def test_webarchive_spam_gate_rejects_good_verdict(self):
        class ArchiveResult:
            checked = True
            spam = True
            snapshots_checked = 2
            reason = "WebArchive spam: casino"

        verdict = app_module.DomainVerdict(
            verdict="PASS",
            status="GOOD",
            reason="Порог выполнен",
            locale="DE",
        )
        with patch.object(app_module, "check_webarchive_spam", return_value=ArchiveResult()):
            result = app_module.apply_webarchive_spam_gate(verdict, "example.de")
        self.assertEqual(result.status, "BAD:WEBARCHIVE_SPAM")
        self.assertEqual(result.verdict, "REJECT")
        self.assertIn("Предыдущий AI-итог", result.reason)
        self.assertEqual(result.early_stop_stage, "webarchive_spam")
        self.assertEqual(result.webarchive_status, "SPAM 2")

    def test_webarchive_skip_status_explains_cdx_timeout(self):
        class ArchiveResult:
            checked = False
            spam = False
            snapshots_found = 0
            snapshots_checked = 0
            errors = ["CDX http://example.de/: TimeoutError"]

        verdict = app_module.DomainVerdict(
            verdict="PASS",
            status="GOOD",
            reason="Порог выполнен",
            locale="DE",
        )
        with patch.object(app_module, "check_webarchive_spam", return_value=ArchiveResult()):
            result = app_module.apply_webarchive_spam_gate(verdict, "example.de")
        self.assertEqual(result.status, "GOOD")
        self.assertEqual(result.webarchive_status, "SKIP:CDX_TIMEOUT")

    def test_webarchive_skip_status_explains_no_html_snapshots(self):
        class ArchiveResult:
            checked = False
            spam = False
            snapshots_found = 0
            snapshots_checked = 0
            errors = []

        verdict = app_module.DomainVerdict(
            verdict="PASS",
            status="GOOD",
            reason="Порог выполнен",
            locale="DE",
        )
        with patch.object(app_module, "check_webarchive_spam", return_value=ArchiveResult()):
            result = app_module.apply_webarchive_spam_gate(verdict, "example.de")
        self.assertEqual(result.webarchive_status, "SKIP:NO_HTML")

    def test_worker_successful_ai_path_does_not_read_ai_exception(self):
        test_state = app_module.AppState()
        test_state.browser_ready = True
        test_state.running = True
        test_state.add_batch("DE", "example.de", EmptyDuplicates())

        class FakeSession:
            def bind_majestic_context(self):
                return object()

        class FakeChecker:
            ready = True
            model = "gpt-5.6-terra"
            _model_access_checked = True
            strict_mode = False
            strict_unique_deficit = 1
            strict_article_deficit = 1

            def precheck_backlinks(self, **kwargs):
                return None

            def precheck_historic_pages(self, **kwargs):
                return None

            def evaluate(self, **kwargs):
                return app_module.DomainVerdict(
                    verdict="PASS",
                    status="GOOD",
                    reason="ok",
                    locale="DE",
                    unique_quality=9,
                    article_links=5,
                    homepage_links=5,
                    anchor_risk="CLEAN",
                    api_calls=1,
                    early_stop_stage="test",
                )

        def fake_collect_stage(driver, domain, label, collector):
            if label == "Backlinks Fresh DoFollow":
                return {"rows": [{"source_url": "https://donor.de/a", "target_url": "https://example.de/"}]}
            return {"rows": []}

        with (
            patch.object(app_module, "state", test_state),
            patch.object(app_module, "session", FakeSession()),
            patch.object(app_module, "ai_checker", FakeChecker()),
            patch.object(app_module, "check_domain_context", return_value="GOOD"),
            patch.object(app_module, "collect_majestic_stage", side_effect=fake_collect_stage),
            patch.object(app_module, "write_backlinks_debug"),
            patch.object(app_module, "WEBARCHIVE_SPAM_ENABLED", False),
            patch.object(app_module, "queue_ai_check") as queue_ai_check,
            patch.object(app_module.duplicate_store, "add"),
            patch.object(app_module.sheets, "append_good"),
            patch.object(app_module, "write_results_csv"),
            patch.object(app_module.random, "randint", return_value=0),
            patch.object(app_module.time, "sleep", side_effect=[None, KeyboardInterrupt]),
        ):
            with self.assertRaises(KeyboardInterrupt):
                app_module.domain_worker()

        self.assertEqual(test_state.results[0].status, "PENDING:AI")
        self.assertEqual(test_state.results[0].ai_status, "QUEUED")
        self.assertEqual(test_state.results[0].error, "")
        queue_ai_check.assert_called_once()


if __name__ == "__main__":
    unittest.main()
