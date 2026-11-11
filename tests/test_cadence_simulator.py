import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cadence_simulator
import poster


class CadenceSimulationTests(unittest.TestCase):
    def test_dry_run_main_emits_structured_simulation_without_sending_or_saving(self):
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
                 patch.object(poster, "send_message") as sender, \
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
        self.assertEqual(len(parsed["simulation"]["scenarios"]), 5)
        self.assertEqual(parsed["published"], 0)

    def test_empty_queue_has_no_publication_capacity_or_clearance_work(self):
        scenarios = cadence_simulator.simulate_intervals(
            queue_depth=0, available_seconds=720,
        )
        self.assertEqual([item["interval_seconds"] for item in scenarios], [30, 25, 20, 15, 10])
        self.assertTrue(all(item["estimated_published_this_run"] == 0 for item in scenarios))
        self.assertTrue(all(item["estimated_clearance_seconds"] == 0 for item in scenarios))

    def test_request_latency_uses_recent_measurements_and_falls_back_when_missing(self):
        self.assertEqual(cadence_simulator.estimated_send_request_seconds([]), 0.5)
        measured = cadence_simulator.estimated_send_request_seconds([
            {"telegram_request_duration_seconds": 1.2, "telegram_attempts": 3},
            {"telegram_request_duration_seconds": 0.4, "telegram_attempts": 1},
        ])
        self.assertAlmostEqual(measured, 0.4)

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
            state = poster.load_state(str(path))
        self.assertEqual(state["file"], pending)
        self.assertEqual(state["metrics_history"], [])
        self.assertIsNone(poster._oldest_pending_age_seconds(state["file"], now_epoch=2000000000))

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
            poster.save_state(state, {}, str(path))
            resumed = poster.load_state(str(path))
        plan = cadence_simulator.build_simulation(
            history=resumed["metrics_history"], queue_depth=500,
            oldest_age_seconds=80000, available_seconds=720,
            run_started_epoch=900, articles_discovered=50,
            previous_simulation=poster._previous_simulation(resumed["metrics_history"]),
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
            sent = poster.process_queue(
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
        sent = poster.process_queue(
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
            "link": "https://example.org/article", "published": "2001-01-01",
        }
        response = Mock(content=b"<rss/>")
        response.raise_for_status.return_value = None
        transport = Mock()
        transport.get.return_value = response
        state = {"vus": [], "file": [], "feeds": [feed], "yt": {}, "init": True}
        metrics = {}
        poster.collect_articles(
            [feed], state, http=transport,
            parser=Mock(return_value=Mock(entries=[entry])),
            metrics=metrics, queue_clock=lambda: 1000,
        )
        self.assertEqual(state["file"][0]["queued_at"], 1000)
        self.assertEqual(state["file"][0]["pub"], "2001-01-01")
        self.assertEqual(poster._oldest_pending_age_seconds(state["file"], now_epoch=1017), 17)
        self.assertEqual(metrics["articles_discovered"], 1)
        self.assertEqual(metrics["articles_added"], 1)
        self.assertEqual(metrics["queue_max"], 1)

    def test_run_metrics_history_stays_bounded_and_contains_no_article_content(self):
        state = {"metrics_history": []}
        for index in range(15):
            poster._store_run_metrics(state, {
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


if __name__ == "__main__":
    unittest.main()
