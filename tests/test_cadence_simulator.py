import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cadence_simulator
import poster
from newsbot import collection, persistence, admission, metrics as run_metrics, publication


class CadenceSimulationTests(unittest.TestCase):
    def test_dry_run_main_emits_metrics_without_simulation_sending_or_saving(self):
        state = {
            "vus": [], "file": [{
                "text": "pending", "link": "https://example.org/article",
            }], "feeds": [], "yt": {}, "metrics_history": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with patch.dict("os.environ", {
                "DRY_RUN": "1", "ALLOW_TELEGRAM_SEND": "0",
                "STATE_FILE": str(state_path),
            }, clear=True), patch.object(poster, "load_feeds", return_value=[]), \
                 patch.object(poster, "load_state", return_value=state), \
                 patch.object(publication.requests, "post") as sender, \
                 patch("builtins.print") as log:
                poster.main()
        sender.assert_not_called()
        self.assertFalse(state_path.exists())
        summary = next(
            call.args[1] for call in log.call_args_list
            if call.args and call.args[0] == "PHASE51_METRICS |"
        )
        parsed = json.loads(summary)
        self.assertEqual(parsed["telegram_interval_configured_seconds"], 30)
        self.assertNotIn("simulation", parsed)
        self.assertEqual(parsed["queue_final"], 1)
        self.assertIn("articles_admissible", parsed)
        self.assertEqual(parsed["published"], 0)

    def test_empty_queue_has_no_publication_capacity_or_clearance_work(self):
        scenarios = cadence_simulator.simulate_intervals(
            queue_depth=0, available_seconds=720,
        )
        self.assertEqual([item["interval_seconds"] for item in scenarios], [30, 25, 20, 15, 10])
        self.assertTrue(all(item["estimated_published_this_run"] == 0 for item in scenarios))
        self.assertTrue(all(item["estimated_clearance_seconds"] == 0 for item in scenarios))

    def test_capacity_boundaries_include_first_send_reserve_wait_and_operation_latency(self):
        common = {
            "queue_depth": 10,
            "interval_seconds": 30,
            "send_operation_seconds": 2,
            "send_budget_floor_seconds": 100,
            "max_per_run": 50,
        }
        def capacity(available):
            row = next(item for item in cadence_simulator.simulate_intervals(
                queue_depth=common["queue_depth"],
                available_seconds=available,
                seconds_since_last_success=25,
                send_operation_seconds=common["send_operation_seconds"],
                send_budget_floor_seconds=common["send_budget_floor_seconds"],
            ) if item["interval_seconds"] == common["interval_seconds"])
            return row["capacity_this_run"]

        self.assertEqual(capacity(104.99), 0)
        self.assertEqual(capacity(105), 1)
        self.assertEqual(capacity(136.99), 1)
        self.assertEqual(capacity(137), 2)

    def test_capacity_is_monotonic_across_all_simulated_intervals_and_budget_edges(self):
        scenarios = cadence_simulator.simulate_intervals(
            queue_depth=100, available_seconds=500,
            send_budget_floor_seconds=100, send_operation_seconds=0.5,
        )
        self.assertEqual([row["interval_seconds"] for row in scenarios], [30, 25, 20, 15, 10])
        capacities = [row["capacity_this_run"] for row in scenarios]
        self.assertEqual(capacities, sorted(capacities))
        below_reserve = cadence_simulator.simulate_intervals(
            queue_depth=10, available_seconds=209.99,
            send_budget_floor_seconds=210,
        )
        self.assertTrue(all(row["capacity_this_run"] == 0 for row in below_reserve))

    def test_candidate_intervals_have_individual_initial_waits(self):
        scenarios = cadence_simulator.simulate_intervals(
            queue_depth=100, available_seconds=720,
            seconds_since_last_success=15,
        )
        self.assertEqual([row["first_wait_seconds"] for row in scenarios], [15, 10, 5, 0, 0])

    def test_workflow_period_reports_observed_spread_and_non_guarantee(self):
        observed = cadence_simulator.estimate_workflow_interval([
            {"run_started_epoch": 100},
            {"run_started_epoch": 400},
            {"run_started_epoch": 1300},
        ])
        self.assertEqual(observed["seconds"], 600)
        self.assertEqual(observed["samples"], 2)
        self.assertEqual(observed["minimum_observed_seconds"], 300)
        self.assertEqual(observed["maximum_observed_seconds"], 900)
        self.assertFalse(observed["guaranteed"])
        fallback = cadence_simulator.estimate_workflow_interval([])
        self.assertEqual(fallback["seconds"], 300)
        self.assertEqual(fallback["source"], "cron_fallback_no_observed_gap")

    def test_overlapping_workflow_uses_longer_cycle_and_marks_clearance_unreliable(self):
        row = next(item for item in cadence_simulator.simulate_intervals(
            queue_depth=100, available_seconds=540, next_run_seconds=300,
            workflow_nonpublishing_seconds=180,
        ) if item["interval_seconds"] == 30)
        self.assertTrue(row["workflow_duration_exceeds_assumption"])
        self.assertGreaterEqual(row["effective_cycle_seconds"], row["estimated_workflow_seconds"])
        self.assertFalse(row["clearance_forecast_reliable"])
        self.assertEqual(row["clearance_forecast_quality"], "faible_chevauchement")

    def test_collection_time_is_not_counted_as_time_since_last_success_at_next_run(self):
        after_publication = next(item for item in cadence_simulator.simulate_intervals(
            queue_depth=100, available_seconds=540, next_run_seconds=300,
            workflow_nonpublishing_seconds=400,
        ) if item["interval_seconds"] == 30)
        self.assertTrue(after_publication["workflow_duration_exceeds_assumption"])
        self.assertEqual(after_publication["first_wait_next_run_seconds"], 30)
        self.assertEqual(after_publication["capacity_next_run"], 10)

        without_publication = next(item for item in cadence_simulator.simulate_intervals(
            queue_depth=100, available_seconds=100, next_run_seconds=300,
            workflow_nonpublishing_seconds=400,
            seconds_since_last_success=0,
        ) if item["interval_seconds"] == 30)
        self.assertEqual(without_publication["capacity_this_run"], 0)
        self.assertEqual(without_publication["first_wait_next_run_seconds"], 30)

    def test_arrivals_can_prevent_queue_clearance_and_saturation_is_reported(self):
        row = next(item for item in cadence_simulator.simulate_intervals(
            queue_depth=100, available_seconds=720, arrivals_per_minute=20,
            next_run_seconds=300,
        ) if item["interval_seconds"] == 30)
        self.assertIsNone(row["estimated_clearance_seconds"])
        self.assertEqual(row["clearance_status"], "arrivals_at_or_above_service")
        self.assertGreater(row["projected_queue_before_next_run"], row["queue_after_run"])
        self.assertGreater(row["effective_cycle_seconds"], row["assumed_start_interval_seconds"])
        self.assertGreater(row["incoming_before_next_run"], 100)

    def test_insufficient_execution_budget_reports_no_clearance_forecast(self):
        row = next(item for item in cadence_simulator.simulate_intervals(
            queue_depth=10, available_seconds=100,
            send_budget_floor_seconds=210,
        ) if item["interval_seconds"] == 10)
        self.assertEqual(row["capacity_this_run"], 0)
        self.assertIsNone(row["estimated_clearance_seconds"])
        self.assertEqual(row["clearance_status"], "budget_insufficient")

    def test_request_latency_uses_recent_measurements_and_falls_back_when_missing(self):
        self.assertEqual(cadence_simulator.estimated_send_request_seconds([]), 0.5)
        measured = cadence_simulator.estimated_send_request_seconds([
            {"telegram_request_duration_seconds": 1.2, "telegram_attempts": 3},
            {"telegram_request_duration_seconds": 0.4, "telegram_attempts": 1},
        ])
        self.assertAlmostEqual(measured, 0.4)

    def test_sender_operation_estimate_includes_retry_waits_measured_by_the_caller(self):
        measured = cadence_simulator.estimated_send_operation_seconds([
            {"telegram_call_duration_seconds": 9.0, "telegram_send_operations": 3},
        ])
        self.assertEqual(measured, 3.0)
        self.assertEqual(cadence_simulator.estimated_send_operation_seconds([]), 0.5)

    def test_arrival_rate_uses_admissible_count_when_available(self):
        rate = cadence_simulator.estimated_arrivals_per_minute([
            {"run_started_epoch": 100, "articles_discovered": 80, "articles_admissible": 2},
            {"run_started_epoch": 160, "articles_discovered": 90, "articles_admissible": 3},
        ])
        self.assertEqual(rate, 3)

    def test_light_queue_keeps_comfortable_simulated_cadence(self):
        result = cadence_simulator.build_simulation(
            history=[{"queue_final": 12}, {"queue_final": 15}],
            queue_depth=20, oldest_age_seconds=300, available_seconds=720,
            run_started_epoch=300, articles_discovered=5,
        )
        self.assertEqual(result["recommendation"]["interval_seconds"], 30)
        self.assertEqual(result["recommendation"]["target_interval_seconds"], 30)
        self.assertEqual(result["scenarios"][0]["estimated_published_this_run"], 17)
        self.assertEqual(result["scenarios"][0]["queue_after_run"], 3)

    def test_continuous_queue_growth_requests_acceleration_but_uses_hysteresis(self):
        history = [{"queue_final": 100}, {"queue_final": 250}]
        first = cadence_simulator.build_simulation(
            history=history, queue_depth=500, oldest_age_seconds=7200,
            available_seconds=720, run_started_epoch=600, articles_discovered=80,
        )
        self.assertEqual(first["queue_trend_per_run"], 200)
        self.assertEqual(first["recommendation"]["target_interval_seconds"], 10)
        self.assertEqual(first["recommendation"]["interval_seconds"], 30)
        self.assertEqual(first["recommendation"]["reason"], "hysteresis_en_cours")

    def test_queue_near_saturation_targets_minimum_without_skipping_steps(self):
        first = cadence_simulator.regulate_simulated_interval(
            queue_depth=950, trend_per_run=150, oldest_age_seconds=90000,
            current_interval_seconds=30, history_runs=2,
        )
        second = cadence_simulator.regulate_simulated_interval(
            queue_depth=950, trend_per_run=150, oldest_age_seconds=90000,
            current_interval_seconds=30,
            candidate_interval_seconds=first["candidate_interval_seconds"],
            candidate_observations=first["candidate_observations"], history_runs=3,
        )
        self.assertEqual(second["target_interval_seconds"], 10)
        self.assertEqual(second["interval_seconds"], 25)
        self.assertGreaterEqual(second["interval_seconds"], 10)

    def test_recovery_moves_back_toward_normal_one_step_after_hysteresis(self):
        current = 10
        candidate = None
        observations = 0
        steps = []
        for _ in range(8):
            result = cadence_simulator.regulate_simulated_interval(
                queue_depth=10, trend_per_run=-20, oldest_age_seconds=60,
                current_interval_seconds=current,
                candidate_interval_seconds=candidate,
                candidate_observations=observations, history_runs=4,
            )
            current = result["interval_seconds"]
            candidate = result["candidate_interval_seconds"]
            observations = result["candidate_observations"]
            steps.append(current)
        self.assertEqual(steps[-1], 30)
        self.assertEqual(steps[:2], [10, 15])
        self.assertTrue(all(right - left in (0, 5) for left, right in zip(steps, steps[1:])))

    def test_insufficient_history_holds_simulated_baseline(self):
        for count in (0, 1):
            with self.subTest(history_runs=count):
                result = cadence_simulator.regulate_simulated_interval(
                    queue_depth=900, trend_per_run=200, oldest_age_seconds=90000,
                    current_interval_seconds=30, history_runs=count,
                )
                self.assertFalse(result["history_sufficient"])
                self.assertEqual(result["interval_seconds"], 30)
                self.assertEqual(result["reason"], "historique_insuffisant")

    def test_legacy_state_migration_preserves_queue_without_inventing_age(self):
        pending = [{"text": "ancien article", "link": "https://example.org/a", "pub": "2020-01-01"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({"v": 2, "file": pending, "vus": ["seen"]}), encoding="utf-8")
            state = persistence.load_state(str(path))
        self.assertEqual(state["file"], pending)
        self.assertEqual(state["metrics_history"], [])
        self.assertIsNone(run_metrics._oldest_pending_age_seconds(state["file"], now_epoch=2000000000))

    def test_simulated_hysteresis_survives_restart_in_bounded_state_history(self):
        state = {"file": [], "vus": [], "metrics_history": [
            {"queue_final": 100, "simulated_interval_seconds": 30,
             "simulated_candidate_interval_seconds": 10,
             "simulated_candidate_observations": 1},
            {"queue_final": 250, "simulated_interval_seconds": 30,
             "simulated_candidate_interval_seconds": 10,
             "simulated_candidate_observations": 1},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            persistence.save_state(state, {}, str(path))
            resumed = persistence.load_state(str(path))
        plan = cadence_simulator.build_simulation(
            history=resumed["metrics_history"], queue_depth=500,
            oldest_age_seconds=80000, available_seconds=720,
            run_started_epoch=900, articles_discovered=50,
            previous_simulation=cadence_simulator.previous_simulation(resumed["metrics_history"]),
        )
        self.assertEqual(plan["recommendation"]["interval_seconds"], 25)

    def test_actual_configured_interval_is_kept_even_when_simulation_recommends_faster(self):
        fake = type("Clock", (), {
            "value": 100.0,
            "__call__": lambda self: self.value,
            "sleep": lambda self, seconds: setattr(self, "value", self.value + seconds),
        })()
        state = {
            "vus": [], "file": [
                {"text": "one", "link": "https://example.org/1"},
                {"text": "two", "link": "https://example.org/2"},
            ], "erreurs": [], "last_successful_send_at": 40,
        }
        sender = Mock(return_value=True)
        plan = cadence_simulator.regulate_simulated_interval(
            queue_depth=900, trend_per_run=100, oldest_age_seconds=50000,
            current_interval_seconds=30, history_runs=3,
        )
        self.assertLess(plan["target_interval_seconds"], 30)
        with tempfile.TemporaryDirectory() as directory:
            sent = publication.process_queue(
                state, {}, "token", "chat", state_path=str(Path(directory) / "state.json"),
                dry_run=False, sender=sender, sleep=fake.sleep, clock=fake,
                monotonic=fake, interval_seconds=60, time_limit=1000,
            )
        self.assertEqual(sent, 2)
        self.assertGreaterEqual(state["last_successful_send_at"] - 100, 60)
        self.assertEqual(state["last_successful_send_at"], 160)

    def test_simulation_does_not_call_sender_or_change_queue(self):
        pending = [{"text": "pending", "link": "https://example.org/a"}]
        state = {"vus": [], "file": list(pending), "erreurs": []}
        sender = Mock(side_effect=AssertionError("le simulateur ne doit pas publier"))
        plan = cadence_simulator.build_simulation(
            history=[], queue_depth=1, oldest_age_seconds=None,
            available_seconds=720, run_started_epoch=1, articles_discovered=0,
        )
        sent = publication.process_queue(
            state, {}, "token", "chat", dry_run=True, sender=sender,
        )
        self.assertEqual(len(plan["scenarios"]), 5)
        self.assertEqual(sent, 0)
        self.assertEqual(state["file"], pending)
        sender.assert_not_called()

    def test_new_article_age_uses_enqueue_time_not_rss_publication_date(self):
        feed = "https://example.org/feed"
        entry = {
            "id": "old-rss-date", "title": "Old article",
            "link": "https://example.org/article", "published": "1970-01-01T00:16:39Z",
            "published_parsed": time.gmtime(999),
        }
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        state = {"vus": [], "file": [], "feeds": [feed], "yt": {}, "init": True}
        metrics = {}
        seen = collection.collect_articles(
            [feed], state, http=transport,
            parser=Mock(return_value=Mock(entries=[entry])),
            metrics=metrics, queue_clock=lambda: 1000,
        )
        self.assertEqual(state["file"][0]["queued_at"], 1000)
        self.assertEqual(state["file"][0]["pub"], "1970-01-01T00:16:39Z")
        self.assertEqual(run_metrics._oldest_pending_age_seconds(state["file"], now_epoch=1017), 17)
        self.assertEqual(metrics["articles_discovered"], 1)
        self.assertEqual(metrics["articles_admissible"], 1)
        self.assertEqual(metrics["articles_added"], 1)
        self.assertEqual(metrics["queue_max"], 1)
        self.assertTrue(metrics["freshness_policy_enabled"])
        self.assertEqual(metrics["articles_rejected_by_freshness"], 0)
        self.assertEqual(metrics["articles_with_pubdate"], 1)

    def test_metrics_separate_feed_cursor_skips_id_duplicates_and_admissions(self):
        feed = "https://example.org/feed"
        entries = [
            {"id": "new", "title": "New", "link": "https://example.org/new",
             "published_parsed": time.gmtime(1000)},
            {"id": "dup", "title": "Dup", "link": "https://example.org/dup",
             "published_parsed": time.gmtime(1000)},
            {"id": "cursor", "title": "Cursor", "link": "https://example.org/cursor",
             "published_parsed": time.gmtime(1000)},
        ]
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        state = {
            "vus": ["dup"], "file": [], "feeds": [feed], "yt": {}, "init": True,
            "feed_health": {feed: {"latest_uid": "cursor"}},
        }
        metrics = {}
        collection.collect_articles(
            [feed], state, http=Mock(get=Mock(return_value=response)),
            parser=Mock(return_value=Mock(entries=entries)), metrics=metrics,
            queue_clock=lambda: 1000,
            max_age_seconds=24 * 3600,
        )
        self.assertEqual(metrics["entries_examined"], 3)
        self.assertEqual(metrics["articles_skipped_by_feed_cursor"], 1)
        self.assertEqual(metrics["articles_skipped_by_seen_id"], 1)
        self.assertEqual(metrics["articles_discovered"], 1)
        self.assertEqual(metrics["articles_admissible"], 1)
        self.assertEqual(metrics["articles_added"], 1)
        self.assertTrue(metrics["freshness_policy_enabled"])

    def test_full_queue_counts_admissible_article_without_counting_it_as_added(self):
        feed = "https://example.org/feed"
        entry = {
            "id": "deferred", "title": "Deferred",
            "link": "https://example.org/deferred",
            "published_parsed": time.gmtime(1_800_000_000),
        }
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        state = {
            "vus": [],
            "file": [{"text": "occupied", "link": "https://example.org/occupied"}]
                * admission.MAX_FILE,
            "feeds": [feed], "yt": {}, "init": True,
        }
        metrics = {}
        seen = collection.collect_articles(
            [feed], state, http=Mock(get=Mock(return_value=response)),
            parser=Mock(return_value=Mock(entries=[entry])), metrics=metrics,
            queue_clock=lambda: 1_800_000_000, max_age_seconds=24 * 3600,
        )
        self.assertEqual(metrics["articles_discovered"], 1)
        self.assertEqual(metrics["articles_admissible"], 1)
        self.assertEqual(metrics["articles_added"], 0)
        self.assertEqual(metrics["articles_added_to_admission_buffer"], 1)
        self.assertEqual(metrics["rss_deferred"], 0)
        self.assertIn("deferred", seen)
        self.assertEqual(len(state["admission_buffer"]), 1)
        self.assertEqual(len(state["file"]), admission.MAX_FILE)

    def test_run_metrics_history_stays_bounded_and_contains_no_article_content(self):
        state = {"metrics_history": []}
        for index in range(15):
            run_metrics._store_run_metrics(state, {
                "run_started_epoch": index,
                "feeds_examined": 63,
                "articles_discovered": 3,
                "articles_added": 3,
                "published": 2,
                "rss_deferred": 0,
                "queue_initial": 0,
                "queue_final": 1,
                "oldest_pending_age_seconds": 12,
                "collection_seconds": 4,
                "publication_seconds": 30,
                "total_seconds": 34,
                "intervals_seconds": [30],
                "simulation": {"recommendation": {"interval_seconds": 30}},
                "url": "https://example.org/private?token=secret",
            })
        encoded = json.dumps(state["metrics_history"])
        self.assertEqual(len(state["metrics_history"]), 12)
        self.assertEqual(state["metrics_history"][0]["run_started_epoch"], 3)
        self.assertNotIn("example.org", encoded)
        self.assertNotIn("secret", encoded)

    def test_explicit_cli_is_read_only_offline_and_does_not_add_an_observation(self):
        history = [
            {"run_started_epoch": (index + 1) * 300, "queue_final": depth,
             "articles_admissible": 10, "oldest_pending_age_seconds": 100}
            for index, depth in enumerate((100, 250, 500, 700))
        ]
        history[1].update({
            "simulated_interval_seconds": 30,
            "simulated_candidate_interval_seconds": 10,
            "simulated_candidate_observations": 1,
        })
        state = {"vus": [], "file": [
            {"text": "synthetic", "link": f"https://example.invalid/{index}", "queued_at": 1000}
            for index in range(700)
        ], "metrics_history": history}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            before = json.dumps(state).encode("utf-8")
            path.write_bytes(before)
            outputs = []
            with patch("requests.sessions.Session.request", side_effect=AssertionError("network forbidden")):
                for _ in range(2):
                    output = io.StringIO()
                    with patch("sys.stdout", output):
                        cadence_simulator.main([
                            "--state", str(path), "--now", "2000", "--available-seconds", "400",
                        ])
                    outputs.append(json.loads(output.getvalue()))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(directory).iterdir()), [path])
        self.assertEqual(outputs[0], outputs[1])
        plan = outputs[0]
        self.assertEqual(plan["snapshot"]["history_observations"], 4)
        self.assertEqual(plan["snapshot"]["analysis_epoch"], 2000)
        self.assertEqual(plan["estimated_arrivals_per_minute"], 2)
        self.assertEqual(plan["recommendation"]["interval_seconds"], 25)
        self.assertEqual(plan["recommendation"]["candidate_observations"], 1)
        self.assertEqual(len(plan["scenarios"]), 5)

    def test_explicit_cli_refuses_missing_state_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.json"
            with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                cadence_simulator.main(["--state", str(path), "--now", "1000"])
            self.assertEqual(error.exception.code, 2)
            self.assertFalse(path.exists())

    def test_offline_replay_counts_already_admitted_buffer_pressure(self):
        history = [{"queue_final": 50, "admission_buffer_final": 800} for _ in range(4)]
        recommendation = cadence_simulator.replay_recommendation(history)
        self.assertEqual(recommendation["target_interval_seconds"], 10)
        self.assertEqual(recommendation["interval_seconds"], 25)

    def test_explicit_cli_refuses_invalid_numeric_assumptions(self):
        for arguments in (("--now", "nan"), ("--available-seconds", "-1")):
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                cadence_simulator.main(list(arguments))
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
