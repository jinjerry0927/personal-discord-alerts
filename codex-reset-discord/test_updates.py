"""Regression coverage for the October 15 missed advance notice and raw relay."""
import io
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import monitor

POSTED = datetime(2026, 10, 15, 4, 56, 7, tzinfo=timezone.utc)
CHECKED = datetime(2026, 10, 15, 5, 0, 20, tzinfo=timezone.utc)
PARENT = "2110595484180214059"
REPLY = "2110595554036400151"


def post(identity, text, at=POSTED, parent=None):
    return {"id": identity, "text": text, "at": at.isoformat(),
            "url": f"https://x.com/thsottiaux/status/{identity}",
            "in_reply_to_tweet_id": parent, "conversation_id": parent or identity}


def snapshot(observed=False, done=False):
    tweets = [post(PARENT, "The reports of my reset button being lost are greatly exaggerated\n\nDay 10/ Reset", POSTED - timedelta(seconds=16)),
              post(REPLY, "Landing within the next 2 hours", parent=PARENT)]
    events = []
    if observed:
        events.append({"id": REPLY, "group": "reset", "announcement_state": "announced",
                       "announced_at": "2026-10-15T06:13:01Z", "summary": tweets[1]["text"],
                       "url": tweets[1]["url"], "source": "operator-observed",
                       "reset_verification_status": "pending"})
    if done:
        tweets.append(post("2110600000000000000", "Done and dusted", POSTED + timedelta(hours=2), REPLY))
    return {"timeline": {"updated_at": CHECKED.isoformat(), "events": events},
            "feed": {"fetched_at": CHECKED.isoformat(), "stale": False, "tweets": tweets}}


class ThreadClassificationTests(unittest.TestCase):
    def test_oct15_reply_is_advance_notice_before_timeline_registration(self):
        alerts = monitor.collect_alerts(snapshot())
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual((alert.category, alert.stage), ("reset_notice", "announced"))
        self.assertEqual(alert.source_id, REPLY)
        self.assertEqual(alert.related_url, f"https://x.com/thsottiaux/status/{PARENT}")
        self.assertEqual(alert.deadline, POSTED + timedelta(hours=2))

    def test_timing_fragment_without_parent_is_not_guessed(self):
        data = snapshot()
        data["feed"]["tweets"] = data["feed"]["tweets"][1:]
        self.assertEqual(monitor.collect_alerts(data), [])

    def test_same_phrase_under_personal_post_is_not_reset_notice(self):
        data = snapshot()
        data["feed"]["tweets"][0]["text"] = "I reset my router. My flight leaves soon."
        self.assertEqual(monitor.collect_alerts(data), [])

    def test_unrelated_codex_product_does_not_turn_landing_into_reset(self):
        data = snapshot()
        data["feed"]["tweets"][0]["text"] = "We are building a new Codex feature."
        self.assertEqual(monitor.collect_alerts(data), [])

    def test_other_authors_parent_is_not_used(self):
        data = snapshot()
        data["feed"]["tweets"][0]["url"] = f"https://x.com/another/status/{PARENT}"
        self.assertEqual(monitor.collect_alerts(data), [])

    def test_no_inference_from_time_proximity(self):
        data = snapshot()
        data["feed"]["tweets"][1]["in_reply_to_tweet_id"] = None
        self.assertEqual(monitor.collect_alerts(data), [])

    def test_reply_cycle_is_bounded(self):
        data = snapshot()
        data["feed"]["tweets"][0]["in_reply_to_tweet_id"] = REPLY
        self.assertEqual(len(monitor.collect_alerts(data)), 1)

    def test_tracker_observation_is_labelled_separately_and_suppresses_old_notice(self):
        alerts = monitor.collect_alerts(snapshot(observed=True))
        self.assertEqual([(a.category, a.stage) for a in alerts], [("reset_done", "observed")])
        payload = monitor.build_payload(alerts[0])
        embed = payload["embeds"][0]
        self.assertEqual(embed["title"], "✅ 리셋 적용")
        self.assertIn("Tibo의 직접 확인과는 구분", embed["description"])
        self.assertIn("추적 서비스 관측 · Tibo 확인 대기", str(embed["fields"]))

    def test_linked_tibo_done_reply_supersedes_tracker_observation(self):
        alerts = monitor.collect_alerts(snapshot(observed=True, done=True))
        self.assertEqual([(a.category, a.stage) for a in alerts], [("reset_done", "tibo_confirmed")])

    def test_unrelated_done_is_not_applied(self):
        data = snapshot()
        data["feed"]["tweets"] = [post("123", "Done and dusted")]
        self.assertEqual(monitor.collect_alerts(data), [])

    def test_future_completed_word_is_not_treated_as_already_applied(self):
        for text in ("A global reset will be completed tomorrow.", "A global reset should be completed soon."):
            data = snapshot()
            data["feed"]["tweets"] = [post("123", text)]
            self.assertFalse(any(a.category == "reset_done" for a in monitor.collect_alerts(data)))

    def test_explicit_tibo_usage_reset_confirmation(self):
        data = snapshot()
        data["feed"]["tweets"] = [post("123", "We have reset everyone's usage limits.")]
        self.assertEqual(monitor.collect_alerts(data)[0].stage, "tibo_confirmed")

    def test_negation_is_not_an_applied_reset(self):
        data = snapshot()
        data["feed"]["tweets"] = [post("123", "A global reset is not done.")]
        self.assertEqual(monitor.collect_alerts(data), [])


class RelayAndTimingTests(unittest.TestCase):
    def test_notice_then_application_are_two_separate_deduplicated_deliveries(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            messages = []
            monitor.process([], path, CHECKED-timedelta(minutes=10), messages.append)
            notices = monitor.collect_alerts(snapshot())
            self.assertEqual(monitor.process(notices, path, CHECKED, messages.append), (1, False))
            self.assertEqual(monitor.process(notices, path, CHECKED+timedelta(minutes=10), messages.append), (0, False))
            applications = monitor.collect_alerts(snapshot(observed=True))
            applied_at = datetime(2026, 10, 15, 6, 20, tzinfo=timezone.utc)
            self.assertEqual(monitor.process(applications, path, applied_at, messages.append), (1, False))
            self.assertEqual(monitor.process(applications, path, applied_at+timedelta(minutes=10), messages.append), (0, False))
            self.assertEqual([m['embeds'][0]['title'] for m in messages], ['🔔 리셋 예정', '✅ 리셋 적용'])

    def test_personal_posts_are_relayed_without_reset_classification(self):
        data = snapshot()
        data["feed"]["tweets"].append(post("123", "A nice cup of coffee. @everyone `hello`"))
        posts = monitor.collect_posts(data)
        self.assertEqual(len(posts), 3)
        personal = next(p for p in posts if p.source_id == "123")
        payload = monitor.build_payload(personal)
        self.assertEqual(payload["embeds"][0]["description"], personal.evidence)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_relay_preserves_line_breaks_and_duplicate_ids_are_collapsed(self):
        data = snapshot()
        data["feed"]["tweets"].append(data["feed"]["tweets"][0])
        posts = monitor.collect_posts(data)
        self.assertEqual(len(posts), 2)
        self.assertIn("\n\nDay 10/ Reset", posts[0].evidence)

    def test_bad_url_does_not_enter_raw_relay(self):
        data = snapshot()
        data["feed"]["tweets"][0]["url"] = "https://example.com/"
        self.assertEqual(len(monitor.collect_posts(data)), 1)

    def test_post_observed_detected_and_sent_times_are_distinct(self):
        alert = monitor.collect_alerts(snapshot(observed=True))[0]
        detected = datetime(2026, 10, 15, 6, 20, tzinfo=timezone.utc)
        alert = replace(alert, detected_at=detected, api_fetched_at=detected-timedelta(seconds=30))
        payload = monitor.build_payload(alert, sent_at=detected+timedelta(seconds=5))
        fields = {f["name"]: f["value"] for f in payload["embeds"][0]["fields"]}
        self.assertEqual(fields["X 게시 시각"], "2026-10-15 13:56:07 KST")
        self.assertEqual(fields["원문/항목 최초 감지"], "2026-10-15 15:20:00 KST")
        self.assertEqual(fields["추적 서비스 기록 시각"], "2026-10-15 15:13:01 KST")
        self.assertTrue(fields["게시 → 원문 감지"].startswith("83분 53초"))
        self.assertEqual(fields["이번 전송 요청 시각"], "2026-10-15 15:20:05 KST")

    def test_relative_deadline_is_from_post_time_not_detection_time(self):
        alert = monitor.collect_alerts(snapshot())[0]
        fields = {f["name"]: f["value"] for f in monitor.build_payload(alert)["embeds"][0]["fields"]}
        self.assertTrue(fields["원문 기준 적용 예정 기한"].startswith("2026-10-15 15:56:07 KST까지"))

    def test_first_detection_is_not_changed_on_later_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "observations.json"
            posts = monitor.collect_posts(snapshot())
            first = monitor.record_first_seen(posts, path, CHECKED)
            again = monitor.record_first_seen(posts, path, CHECKED+timedelta(minutes=10))
            self.assertEqual(first, again)

    def test_later_application_keeps_original_post_detection_and_tracks_new_state_time(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "observations.json"
            notices = monitor.collect_alerts(snapshot())
            monitor.record_first_seen(notices + monitor.collect_posts(snapshot()), path, CHECKED)
            applied_at = datetime(2026, 10, 15, 6, 20, tzinfo=timezone.utc)
            application = monitor.collect_alerts(snapshot(observed=True))[0]
            entries = monitor.record_first_seen([application], path, applied_at)
            self.assertEqual(entries[REPLY], CHECKED.isoformat())
            self.assertEqual(entries[f"state:{application.key}"], applied_at.isoformat())
            application = replace(application, detected_at=CHECKED, state_detected_at=applied_at)
            fields = {f['name']:f['value'] for f in monitor.build_payload(application)['embeds'][0]['fields']}
            self.assertEqual(fields['원문/항목 최초 감지'], '2026-10-15 14:00:20 KST')
            self.assertEqual(fields['이번 리셋 상태 최초 감지'], '2026-10-15 15:20:00 KST')

    def test_old_state_migration_does_not_replay_newly_recognized_notices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            monitor.save_state(path, {"version": 1, "started_at": CHECKED.isoformat(), "seen": []})
            messages = []
            self.assertEqual(monitor.process(monitor.collect_alerts(snapshot()), path, CHECKED, messages.append), (0, True))
            self.assertEqual(messages, [])

    def test_raw_channel_has_separate_baseline_and_sends_only_new_post(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "feed-state.json"
            messages = []
            posts = monitor.collect_posts(snapshot())
            self.assertEqual(monitor.process(posts, path, CHECKED, messages.append), (0, True))
            new_post = post("123", "Coffee", CHECKED)
            data = snapshot()
            data["feed"]["tweets"].append(new_post)
            self.assertEqual(monitor.process(monitor.collect_posts(data), path, CHECKED, messages.append), (1, False))
            self.assertEqual(len(messages), 1)

    def test_one_channel_failure_still_attempts_the_other(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            monitor.process([], state, CHECKED, lambda _: None)
            monitor.process([], Path(folder) / "feed-state.json", CHECKED, lambda _: None)
            urls = []
            def send(url, payload):
                urls.append(url)
                if url.endswith('/alerts'):
                    raise monitor.MonitorError('simulated failure')
            env = {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/alerts",
                   "TIBO_FEED_WEBHOOK_URL": "https://discord.com/api/webhooks/456/feed"}
            with patch('sys.argv', ['monitor.py','--send','--state',str(state)]), patch.dict('os.environ', env), patch('monitor.fetch_snapshot', return_value=snapshot()), patch('monitor.send_payload', side_effect=send), patch('monitor.validate_snapshot'), patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(monitor.main(), 1)
            self.assertIn(env['TIBO_FEED_WEBHOOK_URL'], urls)
            self.assertEqual(len(monitor.load_state(Path(folder) / 'feed-state.json')['seen']), 2)

    def test_feed_connection_test_uses_only_feed_secret(self):
        env = {"TIBO_FEED_WEBHOOK_URL": "https://discord.com/api/webhooks/456/feed"}
        with patch('sys.argv', ['monitor.py','--test-feed-webhook']), patch.dict('os.environ', env), patch('monitor.send_payload') as send, patch('monitor.fetch_snapshot') as fetch, patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(monitor.main(), 0)
        self.assertEqual(send.call_args.args[0], env['TIBO_FEED_WEBHOOK_URL'])
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
