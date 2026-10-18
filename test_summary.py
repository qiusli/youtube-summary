import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import summary

CFG = {"first_run_hours": 24, "max_lookback_days": 7, "transcript_wait_hours": 24, "send_time": "07:00",
       "whisper": True, "whisper_max_minutes": 240}

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <entry>
  <yt:videoId>abc123def45</yt:videoId>
  <title>Normal video</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=abc123def45"/>
  <published>2026-10-12T15:00:00+00:00</published>
  <media:group><media:description>About things</media:description></media:group>
 </entry>
 <entry>
  <yt:videoId>short000001</yt:videoId>
  <title>A short</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/short000001"/>
  <published>2026-10-12T16:00:00+00:00</published>
  <media:group><media:description/></media:group>
 </entry>
</feed>"""


class ParsingTests(unittest.TestCase):
    def test_parse_channel_forms(self):
        cid = "UCBJycsmduvYEL83R_U4JriQ"
        self.assertEqual(summary.parse_channel(cid), ("id", cid))
        self.assertEqual(summary.parse_channel(f"https://www.youtube.com/channel/{cid}/videos"), ("id", cid))
        self.assertEqual(summary.parse_channel("@mkbhd"), ("handle", "@mkbhd"))
        self.assertEqual(summary.parse_channel("https://www.youtube.com/@lexfridman/featured"), ("handle", "@lexfridman"))
        self.assertEqual(summary.parse_channel("mkbhd"), ("handle", "@mkbhd"))
        self.assertEqual(summary.parse_channel("https://www.youtube.com/@%E5%8D%8E%E5%B0%94%E8%A1%97%E6%B6%A8%E5%93%A5"),
                         ("handle", "@华尔街涨哥"))
        self.assertEqual(summary.parse_channel("@华尔街涨哥"), ("handle", "@华尔街涨哥"))
        with self.assertRaises(ValueError):
            summary.parse_channel("https://example.com/not a channel")

    def test_iso_duration(self):
        self.assertEqual(summary.iso_duration("PT1H2M3S"), 3723)
        self.assertEqual(summary.iso_duration("PT45S"), 45)
        self.assertEqual(summary.iso_duration("P1DT1M"), 86460)
        self.assertIsNone(summary.iso_duration(None))

    def test_parse_feed_marks_shorts_and_handles_empty_description(self):
        videos = summary.parse_feed(FEED, {"title": "Chan"})
        self.assertEqual([v["id"] for v in videos], ["abc123def45", "short000001"])
        self.assertIs(videos[0]["short"], None)
        self.assertTrue(videos[1]["short"])
        self.assertEqual(videos[0]["description"], "About things")
        self.assertEqual(videos[1]["description"], "")


class ScheduleTests(unittest.TestCase):
    def test_first_run_window(self):
        now = datetime(2026, 10, 13, 14, tzinfo=timezone.utc)
        self.assertEqual(summary.window_start({}, now, CFG), now - timedelta(hours=24))

    def test_later_runs_overlap_previous_send_but_cap_catch_up(self):
        now = datetime(2026, 10, 13, 14, tzinfo=timezone.utc)
        state = {"last_sent_at": "2026-10-12T14:00:00+00:00"}
        self.assertEqual(summary.window_start(state, now, CFG), datetime(2026, 10, 11, 2, tzinfo=timezone.utc))
        state = {"last_sent_at": "2026-09-01T14:00:00+00:00"}
        self.assertEqual(summary.window_start(state, now, CFG), now - timedelta(days=7))

    def test_next_send_catches_up_when_today_not_sent(self):
        zone = ZoneInfo("America/Vancouver")
        now = datetime(2026, 10, 13, 15, tzinfo=zone)
        self.assertEqual(summary.next_send(now, CFG, {}), datetime(2026, 10, 13, 7, tzinfo=zone))
        sent = {"last_sent_date": "2026-10-13"}
        self.assertEqual(summary.next_send(now, CFG, sent), datetime(2026, 10, 14, 7, tzinfo=zone))


class ReportTests(unittest.TestCase):
    REPORT = {"generated_at": "2026-10-13T07:00:00-07:00", "date": "2026-10-13", "channels": 1,
              "skipped_shorts": 2, "deferred": ["Chan · Live (upcoming stream)"], "notes": ["RSS <failed>"],
              "videos": [{"id": "x", "title": "<script>alert(1)</script>", "channel": "A & B",
                          "published": "2026-10-12T15:00:00+00:00", "duration": 3723,
                          "url": "https://www.youtube.com/watch?v=x", "summary": "S", "key_points": ["1 < 2"],
                          "source": "description"}]}

    def test_html_escapes_untrusted_text(self):
        page = summary.html_report(self.REPORT, "America/Vancouver")
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("A &amp; B", page)
        self.assertIn("1 &lt; 2", page)
        self.assertIn("from description only", page)
        self.assertIn("Skipped 2 Shorts", page)

    def test_chinese_videos_come_first(self):
        base = {"published": "2026-10-12T15:00:00+00:00", "summary": ""}
        videos = [{**base, "title": "Market recap", "channel": "A"}, {**base, "title": "美股复盘", "channel": "Z"},
                  {**base, "title": "Stocks", "channel": "B"}, {**base, "title": "财报", "channel": "C"}]
        self.assertEqual([v["channel"] for v in sorted(videos, key=summary.video_order)], ["C", "Z", "A", "B"])

    def test_record_sent_prunes_old_ids(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        state = {"sent": {"old": old}}
        with mock.patch.object(summary, "atomic_json"):
            summary.record_sent(state, self.REPORT)
        self.assertEqual(set(state["sent"]), {"x"})
        self.assertEqual(state["last_sent_date"], "2026-10-13")


class MembersOnlyTests(unittest.TestCase):
    CHANNEL = {"id": "UCabcdefghijklmnopqrstuv", "title": "C"}

    def test_api_uploads_exclude_members_only(self):
        def fake(session, key, endpoint, **params):
            if endpoint == "playlistItems" and params["playlistId"].startswith("UUMO"):
                return {"items": [{"contentDetails": {"videoId": "paid"}}]}
            if endpoint == "playlistItems":
                return {"items": [{"contentDetails": {"videoId": v}} for v in ("free", "paid")]}
            self.assertEqual(params["id"], "free")
            return {"items": [{"id": "free", "snippet": {"title": "T", "publishedAt": "2026-10-13T00:00:00Z"},
                               "contentDetails": {"duration": "PT10M"}}]}
        with mock.patch.object(summary, "api_get", side_effect=fake):
            self.assertEqual([v["id"] for v in summary.uploads_api(None, "k", self.CHANNEL)], ["free"])

    def test_channel_without_members_playlist(self):
        error = RuntimeError("YouTube Data API playlistItems: playlistNotFound")
        with mock.patch.object(summary, "api_get", side_effect=error):
            self.assertEqual(summary.members_only_ids(None, "k", self.CHANNEL), set())


class WhisperTests(unittest.TestCase):
    def test_skips_disabled_and_overlong_videos(self):
        self.assertEqual(summary.whisper_transcript({**CFG, "whisper": False}, {"id": "v"})[0], None)
        text, reason = summary.whisper_transcript(CFG, {"id": "v", "duration": 241 * 60})
        self.assertIsNone(text)
        self.assertIn("240 minutes", reason)


class SummarizeTests(unittest.TestCase):
    VIDEO = {"id": "v", "title": "T", "channel": "C", "description": "D"}

    def run_summarize(self, age_hours, transcript_result, whisper_result=(None, "failed")):
        now = datetime(2026, 10, 13, 14, tzinfo=timezone.utc)
        video = {**self.VIDEO, "published": (now - timedelta(hours=age_hours)).isoformat()}
        cfg = {**CFG, "transcript_languages": ["en"]}
        notes, deferred = [], []
        with mock.patch.object(summary, "read_json", return_value=None), \
             mock.patch.object(summary, "atomic_json"), \
             mock.patch.object(summary, "transcript", return_value=transcript_result), \
             mock.patch.object(summary, "whisper_transcript", return_value=whisper_result), \
             mock.patch.object(summary, "claude_summary", return_value={"summary": "S", "key_points": []}) as claude:
            result = summary.summarize(cfg, video, now, notes, deferred)
        return result, deferred, claude

    def test_missing_captions_are_transcribed_locally(self):
        result, deferred, claude = self.run_summarize(1, (None, "NoTranscriptFound"), ("spoken words", None))
        self.assertEqual(result["source"], "whisper")
        self.assertEqual(claude.call_args.args[2], "spoken words")
        self.assertEqual(deferred, [])

    def test_members_only_video_is_skipped_not_summarized(self):
        result, deferred, claude = self.run_summarize(30, (None, "VideoUnplayable"), (None, summary.MEMBERS_ONLY))
        self.assertEqual(result, summary.MEMBERS_ONLY)
        self.assertEqual(deferred, [])
        claude.assert_not_called()

    def test_new_video_without_any_transcript_waits(self):
        result, deferred, claude = self.run_summarize(2, (None, "NoTranscriptFound"))
        self.assertIsNone(result)
        self.assertEqual(len(deferred), 1)
        claude.assert_not_called()

    def test_old_video_without_captions_uses_description(self):
        result, deferred, claude = self.run_summarize(30, (None, "TranscriptsDisabled"))
        self.assertEqual(result["source"], "description")
        self.assertEqual(claude.call_args.args[2], "D")

    def test_chinese_title_prefers_simplified_captions(self):
        now = datetime(2026, 10, 13, 14, tzinfo=timezone.utc)
        video = {**self.VIDEO, "title": "美股复盘", "published": now.isoformat()}
        with mock.patch.object(summary, "read_json", return_value=None), mock.patch.object(summary, "atomic_json"), \
             mock.patch.object(summary, "transcript", return_value=("文字", None)) as transcript, \
             mock.patch.object(summary, "claude_summary", return_value={"summary": "S", "key_points": []}):
            summary.summarize({**CFG, "transcript_languages": ["en"]}, video, now, [], [])
        self.assertEqual(transcript.call_args.args[1][0], "zh-Hans")

    def test_transcript_is_preferred(self):
        result, _, claude = self.run_summarize(2, ("hello world", None))
        self.assertEqual(result["source"], "captions")
        self.assertEqual(claude.call_args.args[2], "hello world")


if __name__ == "__main__":
    unittest.main()
