import io
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import monitor

NOW = datetime(2026, 10, 14, 3, tzinfo=timezone.utc)


def event(text="A global reset will arrive tomorrow.", **kwargs):
    data = {"id": "post-1", "group": "reset", "type": "reset", "announcement_state": "none",
            "announced_at": "2026-10-14T02:00:00Z", "summary": text}
    data.update(kwargs)
    return data


class ClassificationTests(unittest.TestCase):
    def test_explicit_future_notice(self):
        alert = monitor.classify_event(event())
        self.assertEqual((alert.category, alert.stage), ("reset_notice", "announced"))

    def test_discussion_without_commitment_is_not_an_announcement(self):
        self.assertIsNone(monitor.classify_event(event("We are discussing a global reset today.")))
        self.assertIsNone(monitor.classify_event(event("A global reset today is what people are asking about.")))

    def test_usage_reset_commitment_and_in_progress_notice(self):
        for text in ("We will reset the usage limits tomorrow.", "We are resetting usage for paid users."):
            with self.subTest(text=text):
                self.assertEqual(monitor.classify_event(event(text)).category, "reset_notice")

    def test_jokes_conditionals_questions_and_negations_are_withheld(self):
        for text in (
            "A reset would be nice tomorrow.", "Maybe a global reset will happen today.",
            "If we miss the launch, a global reset will happen tomorrow.",
            "Either we release a feature or a global reset will happen today.",
            "No global reset will happen today.", "We won't do a global reset tomorrow.",
            "Will a global reset arrive tomorrow?", "Day 11: Reset", "I reset my router today.",
            "A full factory reset will happen tomorrow.", "We cannot offer a global reset today.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(monitor.classify_event(event(text)))

    def test_confirmation_uses_documented_source_state(self):
        alert = monitor.classify_event(event("Recorded by the tracker.", announcement_state="announced"))
        self.assertEqual(alert.category, "reset_done")

    def test_done_keyword_without_verified_state_is_not_completion(self):
        self.assertIsNone(monitor.classify_event(event("A global reset is done today.")))

    def test_boosts_and_model_news_are_ignored(self):
        self.assertIsNone(monitor.classify_event(event(group="boost")))

    def test_unknown_announcement_state_is_not_guessed(self):
        self.assertIsNone(monitor.classify_event(event(announcement_state="pending-new-value")))

    def test_banked_available(self):
        alert = monitor.classify_event(event("A banked reset is ready for eligible accounts.", group="credits", banked_state="available"))
        self.assertEqual((alert.category, alert.stage), ("banked", "available"))

    def test_banked_future_stays_separate_from_global_reset(self):
        alert = monitor.classify_event(event("A banked reset is arriving tomorrow.", group="credits", banked_state="arriving"))
        self.assertEqual((alert.category, alert.stage), ("banked", "arriving"))

    def test_unknown_banked_state_does_not_claim_delivery(self):
        alert = monitor.classify_event(event("We are loading a banked reset for paid accounts.", group="credits", banked_state="unknown"))
        self.assertEqual(alert.stage, "announced")

    def test_generic_credits_and_unknown_banked_claims_are_ignored(self):
        self.assertIsNone(monitor.classify_event(event("Additional API credits are available.", group="credits", banked_state="available")))
        self.assertIsNone(monitor.classify_event(event("Maybe a banked reset tomorrow.", group="credits")))

    def test_missing_fields_or_timezone_are_ignored(self):
        for kwargs in ({"id": None}, {"announced_at": "yesterday"}, {"announced_at": "2026-10-14T03:00:00"}, {"summary": None}):
            with self.subTest(kwargs=kwargs):
                self.assertIsNone(monitor.classify_event(event(**kwargs)))

    def test_raw_tweet_is_labelled_as_evidence_not_classifier_metadata(self):
        tweet = {"id": "post-1", "text": "An English announcement.", "kind": "other"}
        alert = monitor.classify_event(event(), tweet)
        self.assertEqual(alert.evidence, tweet["text"])
        self.assertEqual(alert.evidence_label, "Tibo 영어 원문")

    def test_payload_mentions_are_disabled_and_kst_is_correct(self):
        alert = monitor.classify_event(event("A global reset will arrive tomorrow. @everyone"))
        payload = monitor.build_payload(alert)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        fields = payload["embeds"][0]["fields"]
        self.assertEqual(fields[0]["value"], "2026-10-14 11:00 KST")
        self.assertNotIn("@everyone", fields[2]["value"])


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = monitor.read_json(Path(__file__).parent / "examples/demo-input.json")

    def test_demo_contains_exactly_three_categories(self):
        monitor.validate_snapshot(self.snapshot, NOW)
        self.assertEqual({a.category for a in monitor.collect_alerts(self.snapshot)}, {"reset_notice", "reset_done", "banked"})

    def test_old_posts_do_not_mean_fetch_is_stale(self):
        self.snapshot["feed"]["newest_post_at"] = "2020-01-01T00:00:00Z"
        monitor.validate_snapshot(self.snapshot, NOW)

    def test_stale_feed_stops_delivery(self):
        self.snapshot["feed"]["stale"] = True
        with self.assertRaises(monitor.MonitorError):
            monitor.validate_snapshot(self.snapshot, NOW)

    def test_old_or_invalid_refresh_time_stops_delivery(self):
        for value in ("2026-10-14T01:00:00Z", None, "2026-10-15T03:00:00Z"):
            with self.subTest(value=value):
                self.snapshot["timeline"]["updated_at"] = value
                with self.assertRaises(monitor.MonitorError):
                    monitor.validate_snapshot(self.snapshot, NOW)

    def test_schema_change_stops_delivery(self):
        self.snapshot["timeline"]["events"] = {}
        with self.assertRaises(monitor.MonitorError):
            monitor.validate_snapshot(self.snapshot, NOW)


class DeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "state.json"
        self.messages = []

    def initialize(self, alerts=()):
        result = monitor.process(list(alerts), self.state, NOW, self.messages.append)
        self.assertEqual(result, (0, True))

    def test_first_run_sets_baseline_without_historical_spam(self):
        self.initialize([monitor.classify_event(event())])
        self.assertEqual(self.messages, [])

    def test_new_notice_is_sent_once(self):
        self.initialize()
        alert = monitor.classify_event(event())
        self.assertEqual(monitor.process([alert], self.state, NOW, self.messages.append), (1, False))
        self.assertEqual(monitor.process([alert], self.state, NOW, self.messages.append), (0, False))
        self.assertEqual(len(self.messages), 1)

    def test_same_post_notice_to_done_produces_new_alert(self):
        notice = monitor.classify_event(event())
        done = monitor.classify_event(event(announcement_state="announced"))
        self.initialize([notice])
        self.assertEqual(monitor.process([done], self.state, NOW, self.messages.append), (1, False))

    def test_previously_unclassified_post_can_become_verified(self):
        self.initialize()
        done = monitor.classify_event(event("Previously ambiguous, now verified.", announcement_state="announced"))
        self.assertEqual(monitor.process([done], self.state, NOW, self.messages.append), (1, False))

    def test_partial_failure_keeps_prior_acknowledgements(self):
        self.initialize()
        alerts = [monitor.classify_event(event(id=str(i))) for i in range(2)]

        def fail_second(payload):
            if self.messages:
                raise monitor.MonitorError("simulated delivery failure")
            self.messages.append(payload)

        with self.assertRaises(monitor.MonitorError):
            monitor.process(alerts, self.state, NOW, fail_second)
        self.assertEqual(monitor.load_state(self.state)["seen"], [alerts[0].key])
        self.assertEqual(monitor.process(alerts, self.state, NOW, self.messages.append), (1, False))
        self.assertEqual(len(self.messages), 2)

    def test_old_unseen_event_is_not_replayed(self):
        self.initialize()
        old = monitor.classify_event(event(announced_at="2026-10-12T02:00:00Z"))
        self.assertEqual(monitor.process([old], self.state, NOW, self.messages.append), (0, False))

    def test_corrupt_state_is_not_silently_reset(self):
        self.state.write_text('{"version": 2}', encoding="utf-8")
        with self.assertRaises(monitor.MonitorError):
            monitor.process([], self.state, NOW, self.messages.append)


class NetworkTests(unittest.TestCase):
    def test_connection_test_sends_one_without_fetching_or_saving_state(self):
        with patch("sys.argv", ["monitor.py", "--test-webhook"]), patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/test-token"}), patch("sys.stdout", new_callable=io.StringIO), patch("monitor.send_payload") as send, patch("monitor.fetch_snapshot") as fetch, patch("monitor.save_state") as save:
            self.assertEqual(monitor.main(), 0)
        send.assert_called_once()
        self.assertIn("실제 리셋 소식이 아닙니다", send.call_args.args[1]["embeds"][0]["title"])
        fetch.assert_not_called()
        save.assert_not_called()

    def test_connection_test_cannot_be_combined_with_send(self):
        with patch("sys.argv", ["monitor.py", "--test-webhook", "--send"]), patch("sys.stderr", new_callable=io.StringIO), patch("monitor.send_payload") as send:
            self.assertEqual(monitor.main(), 1)
        send.assert_not_called()

    def test_webhook_url_is_not_exposed_in_http_error(self):
        secret_url = "https://discord.com/api/webhooks/123/private-token"
        request = monitor.Request(secret_url)
        error = HTTPError(secret_url, 429, "rate limited", {"Retry-After": "60"}, None)
        with patch("monitor.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.request_json(request, "Discord 전송")
        self.assertNotIn("private-token", str(caught.exception))
        self.assertIn("Retry-After=60", str(caught.exception))
        self.assertEqual(caught.exception.retry_after_seconds, 60)

    def test_network_error_does_not_expose_request_details(self):
        with patch("monitor.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("secret-token")
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.request_json(monitor.Request("https://discord.com"), "Discord 전송")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_rate_limit_http_date_is_honored(self):
        deadline = datetime.now(timezone.utc) + timedelta(hours=2)
        error = HTTPError("https://codex-reset.com/api/feed", 429, "rate limited", {"Retry-After": format_datetime(deadline)}, None)
        with patch("monitor.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.request_json(monitor.Request("https://codex-reset.com/api/feed"), "공개 API feed")
        self.assertGreater(caught.exception.retry_after_seconds, 7100)

    def test_cooldown_skips_network_fetch(self):
        with tempfile.TemporaryDirectory() as folder:
            state_path = Path(folder) / "state.json"
            monitor.save_state(Path(folder) / "cooldown.json", {
                "next_allowed_check_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            })
            with patch("sys.argv", ["monitor.py", "--send", "--state", str(state_path)]), patch("monitor.fetch_snapshot") as fetch, patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(monitor.main(), 0)
            fetch.assert_not_called()

    def test_demo_cannot_be_sent(self):
        with patch("sys.argv", ["monitor.py", "--demo", "--send"]), patch("sys.stderr", new_callable=io.StringIO), patch("monitor.send_payload") as send:
            self.assertEqual(monitor.main(), 1)
        send.assert_not_called()

    def test_webhook_receipt_required_and_wait_enabled(self):
        with patch("monitor.request_json", return_value={}) as request:
            with self.assertRaises(monitor.MonitorError):
                monitor.send_payload("https://discord.com/api/webhooks/123/test-token", {})
        self.assertIn("wait=true", request.call_args.args[0].full_url)

    def test_webhook_does_not_accept_arbitrary_hosts(self):
        with self.assertRaises(monitor.MonitorError):
            monitor.send_payload("https://example.com/api/webhooks/123/test-token", {})


if __name__ == "__main__":
    unittest.main()
