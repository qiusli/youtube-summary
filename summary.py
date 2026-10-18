#!/usr/bin/env python3
"""Daily email digest of new videos from followed YouTube channels. Run ./youtube-summary help."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, time as clock_time, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
import fcntl
import glob
import html
import io
import json
import os
from pathlib import Path
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import unquote
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
API = "https://www.googleapis.com/youtube/v3"
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
           "Accept-Language": "en-US,en;q=0.9"}
COOKIES = {"SOCS": "CAI"}  # skips the EU cookie-consent interstitial
NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
CHANNEL_ID = re.compile(r"UC[\w-]{22}")
MAX_TRANSCRIPT_CHARS = 300_000
MEMBERS_ONLY = "members-only"
CJK = re.compile(r"[\u3400-\u9fff]")
CHINESE_CAPTIONS = ["zh-Hans", "zh-CN", "zh-SG", "zh", "zh-Hant", "zh-TW", "zh-HK"]

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_points"],
}


def system_prompt(language):
    return f"""You summarize YouTube videos for a busy reader who will not watch them.
Write in {language}. Write any Chinese in Simplified Chinese (简体中文), even if the transcript is in
Traditional Chinese or Cantonese. Reply with ONLY the JSON object.
- summary: 2-4 sentences on what the video is actually about and its main conclusion or argument.
- key_points: 3-7 short bullet points with the concrete substance: claims, numbers, names, recommendations,
  predictions, and any dates mentioned. Skip intros, sponsor reads, calls to subscribe, and filler.
State the video's views and claims directly, as plain statements, without attributing them to the speaker:
write "Semiconductors are near long-term support", not "He thinks semiconductors are near support" or
"The author says..."; in Chinese write "半导体处于长期支撑区", not "Andy认为…", "博主表示…" or "作者觉得…".
Never refer to the video, speaker, host, author or channel at all: no "the video argues", "the author owns",
"视频认为" or "作者持有". For the speaker's own trades or holdings, drop the subject: "Owns none of the
neoclouds", "Buying quality AI names that have fallen", "未持有新云股", "正在买入回调的优质AI股".
If you only have the description (no transcript), summarize what it says and do not invent content."""


# ── Config and storage ───────────────────────────────────────────────────
def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temp.replace(path)


def read_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def load_config(path):
    if not path.exists():
        path = ROOT / "config.example.toml"
    with path.open("rb") as stream:
        return tomllib.load(stream)


def validate(cfg):
    if not isinstance(cfg.get("channels"), list) or not all(isinstance(c, str) and c.strip() for c in cfg["channels"]):
        raise ValueError("channels must be a list of @handles, channel URLs or UC… channel IDs")
    if not cfg["channels"]:
        raise ValueError("No channels configured; add them to config.toml")
    send_time(cfg)
    ZoneInfo(cfg["timezone"])
    cfg.setdefault("whisper_timeout_minutes", 60)
    for key in ("first_run_hours", "max_lookback_days", "transcript_wait_hours", "timeout_seconds", "whisper_max_minutes",
                "whisper_timeout_minutes"):
        if type(cfg[key]) is not int or cfg[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")


def send_time(cfg):
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", cfg["send_time"])
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError('send_time must look like "07:00"')
    return clock_time(int(match[1]), int(match[2]))


def keychain(service, account):
    result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
                            capture_output=True, text=True, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else None


# ── Channels and uploads ─────────────────────────────────────────────────
def parse_channel(entry):
    """Return ("id", UC…) or ("handle", @name) for an @handle, channel URL or channel ID."""
    entry = unquote(entry.strip())  # browsers copy non-Latin handles percent-encoded
    match = re.search(r"(?:^|/channel/)(UC[\w-]{22})(?:$|[/?])", entry)
    if match:
        return "id", match[1]
    match = re.search(r"(?:^|youtube\.com/)(@[\w.\-·]+)", entry)
    if match:
        return "handle", match[1]
    if re.fullmatch(r"[\w.\-]+", entry):
        return "handle", "@" + entry
    raise ValueError(f"Unrecognized channel: {entry} (use @handle, a channel URL or a UC… ID)")


def api_get(session, key, endpoint, **params):
    response = session.get(f"{API}/{endpoint}", params={**params, "key": key}, timeout=20)
    if response.status_code != 200:
        try:
            reason = response.json()["error"]["errors"][0]["reason"]
        except (ValueError, KeyError, IndexError):
            reason = f"HTTP {response.status_code}"
        raise RuntimeError(f"YouTube Data API {endpoint}: {reason}")
    return response.json()


def resolve_channel(session, key, entry, cache):
    if entry in cache:
        return cache[entry]
    kind, value = parse_channel(entry)
    channel = None
    if key:
        try:
            params = {"id": value} if kind == "id" else {"forHandle": value}
            items = api_get(session, key, "channels", part="snippet", **params).get("items", [])
            if items:
                channel = {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}
        except Exception:  # noqa: BLE001 - fall through to the public page
            pass
    if channel is None:
        url = f"https://www.youtube.com/channel/{value}" if kind == "id" else f"https://www.youtube.com/{value}"
        page = session.get(url, headers=HEADERS, cookies=COOKIES, timeout=20)
        page.raise_for_status()
        found = (re.search(r'<meta itemprop="identifier" content="(UC[\w-]{22})"', page.text)
                 or re.search(r'"externalId":"(UC[\w-]{22})"', page.text)
                 or re.search(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"', page.text))
        if not found:
            raise RuntimeError(f"Could not find the channel ID for {entry}")
        title = re.search(r'<meta property="og:title" content="([^"]*)"', page.text)
        channel = {"id": found[1], "title": html.unescape(title[1]) if title else value}
    cache[entry] = channel
    return channel


def iso_duration(value):
    match = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(v or 0) for v in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def members_only_ids(session, key, channel):
    """The channel's members-only uploads, from YouTube's (unofficial) UUMO playlist."""
    try:
        items = api_get(session, key, "playlistItems", part="contentDetails", playlistId="UUMO" + channel["id"][2:],
                        maxResults=50).get("items", [])
    except RuntimeError as exc:
        if "playlistNotFound" in str(exc):
            return set()  # the channel has no members-only videos
        raise
    return {item["contentDetails"]["videoId"] for item in items}


def uploads_api(session, key, channel):
    playlist = "UU" + channel["id"][2:]
    items = api_get(session, key, "playlistItems", part="contentDetails", playlistId=playlist,
                    maxResults=20).get("items", [])
    members = members_only_ids(session, key, channel)
    ids = [item["contentDetails"]["videoId"] for item in items if item["contentDetails"]["videoId"] not in members]
    if not ids:
        return []
    videos = api_get(session, key, "videos", part="snippet,contentDetails", id=",".join(ids)).get("items", [])
    result = []
    for video in videos:
        snippet = video["snippet"]
        result.append({"id": video["id"], "title": snippet["title"], "channel": channel["title"],
                       "published": snippet["publishedAt"], "description": snippet.get("description", ""),
                       "duration": iso_duration(video["contentDetails"].get("duration")),
                       "live": snippet.get("liveBroadcastContent", "none"), "source": "api"})
    return result


def parse_feed(text, channel):
    root = ET.fromstring(text)
    result = []
    for entry in root.findall("atom:entry", NS):
        link = entry.find("atom:link", NS)
        href = link.get("href", "") if link is not None else ""
        description = entry.find("media:group/media:description", NS)
        result.append({"id": entry.findtext("yt:videoId", namespaces=NS), "title": entry.findtext("atom:title", "", NS),
                       "channel": channel["title"], "published": entry.findtext("atom:published", namespaces=NS),
                       "description": (description.text or "") if description is not None else "",
                       "duration": None, "live": "unknown", "short": "/shorts/" in href or None, "source": "rss"})
    return result


def uploads_rss(session, channel):
    for attempt in range(2):
        try:
            response = session.get("https://www.youtube.com/feeds/videos.xml",
                                   params={"channel_id": channel["id"]}, headers=HEADERS, timeout=20)
            response.raise_for_status()
            return parse_feed(response.text, channel)
        except Exception:
            if attempt == 1:
                raise
            time.sleep(2)


class QuietLogger:
    """yt-dlp prints errors even when quiet; failures are reported through exceptions instead."""
    def debug(self, message): pass
    def info(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def uploads_ytdlp(channel, skip, cache):
    """Latest uploads from the channel's Videos tab (no Shorts or streams); details cached per video."""
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": QuietLogger()}
    with yt_dlp.YoutubeDL({**options, "extract_flat": "in_playlist", "playlistend": 8}) as ydl:
        listing = ydl.extract_info(f"https://www.youtube.com/channel/{channel['id']}/videos", download=False)
    result = []
    with yt_dlp.YoutubeDL(options) as ydl:
        for entry in listing.get("entries") or []:
            video_id = entry.get("id")
            if not video_id or video_id in skip or entry.get("availability") in ("subscriber_only", "premium_only"):
                continue
            if video_id not in cache:
                try:
                    info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
                except yt_dlp.utils.DownloadError:
                    continue  # members-only, private or region-locked
                if not info.get("timestamp"):
                    continue
                live = {"is_live": "live", "is_upcoming": "upcoming"}.get(info.get("live_status"), "none")
                details = {"id": video_id, "title": info.get("title") or entry.get("title", ""),
                           "published": datetime.fromtimestamp(info["timestamp"], timezone.utc).isoformat(),
                           "description": info.get("description") or "", "duration": info.get("duration"),
                           "live": live, "short": False, "source": "yt-dlp"}
                if live != "none":
                    result.append({**details, "channel": channel["title"]})
                    continue  # details change once the stream ends
                cache[video_id] = details
            result.append({**cache[video_id], "channel": channel["title"]})
    return result


def is_short(session, video):
    """Shorts URLs answer 200 for Shorts and redirect for normal videos."""
    if video.get("short") is not None:
        return video["short"]
    if video["duration"] is not None and video["duration"] > 180:
        return False
    try:
        response = session.head(f"https://www.youtube.com/shorts/{video['id']}", headers=HEADERS,
                                cookies=COOKIES, allow_redirects=False, timeout=15)
        return response.status_code == 200
    except requests.RequestException:
        return False


def fetch_videos(cfg, session, key, notes, skip):
    """Uploads per channel from the Data API, falling back to RSS, then yt-dlp."""
    cache_path, details_path = DATA / "channels.json", DATA / "videos.json"
    cache, details = read_json(cache_path, {}), read_json(details_path, {})
    videos = {}
    for entry in cfg["channels"]:
        try:
            channel = resolve_channel(session, key, entry, cache)
        except Exception as exc:
            notes.append(f"{entry}: channel lookup failed ({exc})")
            continue
        found, failures = None, []
        if key:
            try:
                found = uploads_api(session, key, channel)
            except Exception as exc:
                failures.append(f"API: {exc}")
                notes.append(f"{channel['title']}: Data API failed ({exc}); used a fallback")
        for name, source in (("RSS", lambda: uploads_rss(session, channel)),
                             ("yt-dlp", lambda: uploads_ytdlp(channel, skip, details))):
            if found is not None:
                break
            try:
                found = source()
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}")
        if found is None:
            notes.append(f"{channel['title']}: uploads unavailable ({'; '.join(failures)})")
            continue
        for video in found:
            videos.setdefault(video["id"], video)
    atomic_json(cache_path, cache)
    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    atomic_json(details_path, {k: v for k, v in details.items() if parse_time(v["published"]) > cutoff})
    return list(videos.values())


# ── Transcripts and summaries ────────────────────────────────────────────
def transcript(video_id, languages):
    """Return (text, None) or (None, reason)."""
    try:
        listing = YouTubeTranscriptApi().list(video_id)
        try:
            chosen = listing.find_transcript(languages)
        except Exception:  # noqa: BLE001 - any language beats none
            available = list(listing)
            if not available:
                return None, "no captions"
            chosen = sorted(available, key=lambda t: t.is_generated)[0]
        text = " ".join(snippet.text.replace("\n", " ") for snippet in chosen.fetch())
        return text[:MAX_TRANSCRIPT_CHARS], None
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


def find_claude(cfg):
    if cfg.get("claude_path"):
        return cfg["claude_path"]
    on_path = shutil.which("claude") or (str(Path.home() / ".local/bin/claude")
                                         if os.access(Path.home() / ".local/bin/claude", os.X_OK) else None)
    if on_path:
        return on_path
    root = Path.home() / "Library/Application Support/Claude/claude-code"
    found = sorted((f for f in glob.glob(str(root / "**" / "claude.app/Contents/MacOS/claude"), recursive=True)
                    if os.access(f, os.X_OK)), key=os.path.getmtime)
    if not found:
        raise RuntimeError("Claude Code CLI not found; install it or set claude_path in config.toml")
    return found[-1]


def claude_summary(cfg, video, text, from_transcript):
    prompt = (f"Channel: {video['channel']}\nTitle: {video['title']}\nPublished: {video['published']}\n\n"
              + ("TRANSCRIPT:\n" if from_transcript else "DESCRIPTION (no transcript available):\n") + text)
    cmd = [find_claude(cfg), "-p", "--model", cfg["model"], "--output-format", "json",
           "--system-prompt", system_prompt(cfg["summary_language"]), "--tools", "", "--strict-mcp-config",
           "--no-session-persistence", "--json-schema", json.dumps(SCHEMA)]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    with tempfile.TemporaryDirectory() as cwd:  # keep project CLAUDE.md/settings out of the call
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=env,
                              timeout=cfg["timeout_seconds"])
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude CLI returned non-JSON: {proc.stdout[:200]} {proc.stderr[:200]}") from exc
    if body.get("is_error"):
        raise RuntimeError(f"claude CLI: {body.get('result')}")
    result = body.get("structured_output")
    if not isinstance(result, dict) or not isinstance(result.get("summary"), str):
        raise RuntimeError("claude CLI returned no structured summary")
    return {"summary": result["summary"], "key_points": [str(p) for p in result.get("key_points", [])]}


WHISPER_CHILD = """
import json, sys
import mlx_whisper
result = mlx_whisper.transcribe(sys.argv[1], path_or_hf_repo=sys.argv[2])
print(json.dumps({"text": result["text"]}))
"""


def whisper_transcript(cfg, video):
    """Download the audio and transcribe it on this Mac. Return (text, None) or (None, reason)."""
    if not cfg["whisper"]:
        return None, "Whisper disabled"
    if video.get("duration") and video["duration"] > cfg["whisper_max_minutes"] * 60:
        return None, f"longer than {cfg['whisper_max_minutes']} minutes"
    os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin" + os.pathsep + "/usr/local/bin"  # ffmpeg
    if not shutil.which("ffmpeg"):
        return None, "ffmpeg not installed"
    try:
        with tempfile.TemporaryDirectory() as folder:
            options = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": QuietLogger(),
                       "format": "bestaudio[ext=m4a]/bestaudio", "outtmpl": f"{folder}/audio.%(ext)s"}
            with yt_dlp.YoutubeDL(options) as ydl:
                try:
                    ydl.download([f"https://www.youtube.com/watch?v={video['id']}"])
                except yt_dlp.utils.DownloadError as exc:
                    message = str(exc).lower()
                    if "members" in message or "join this channel" in message:
                        return None, MEMBERS_ONLY
                    raise
            audio = next(Path(folder).glob("audio.*"))
            # A child process: MLX keeps the model and GPU buffers until its process exits,
            # which grew a long-running --watch process to 15 GB.
            proc = subprocess.run([sys.executable, "-c", WHISPER_CHILD, str(audio), cfg["whisper_model"]],
                                  capture_output=True, text=True, timeout=cfg["whisper_timeout_minutes"] * 60,
                                  env={**os.environ, "HF_HUB_DISABLE_PROGRESS_BARS": "1"})
            if proc.returncode:
                return None, f"Whisper failed: {proc.stderr.strip().splitlines()[-1][:150] if proc.stderr.strip() else proc.returncode}"
            text = json.loads(proc.stdout)["text"].strip()
        return (text[:MAX_TRANSCRIPT_CHARS], None) if text else (None, "no speech found")
    except Exception as exc:  # noqa: BLE001
        return None, f"Whisper: {type(exc).__name__}"


def summarize(cfg, video, now, notes, deferred, progress=None):
    """Cached summary, or None when the video should wait for a later run."""
    path = DATA / "summaries" / f"{video['id']}.json"
    cached = read_json(path, None)
    if cached:
        return cached
    # Chinese channels often upload Cantonese and Traditional tracks too; prefer Simplified Mandarin.
    languages = CHINESE_CAPTIONS + cfg["transcript_languages"] if CJK.search(video["title"]) else cfg["transcript_languages"]
    text, reason = transcript(video["id"], languages)
    source = "captions"
    if text is None:
        if progress:
            progress(f"Transcribing audio · {video['channel']} · {video['title'][:60]}")
        text, whisper_reason = whisper_transcript(cfg, video)
        if whisper_reason == MEMBERS_ONLY:  # RSS lists members-only videos without saying so
            return MEMBERS_ONLY
        source = "whisper"
        reason = f"{reason}; {whisper_reason}"
    if text is None:
        age = now - parse_time(video["published"])
        if age < timedelta(hours=cfg["transcript_wait_hours"]):
            deferred.append(f"{video['channel']} · {video['title']} (no transcript yet: {reason})")
            return None
        notes.append(f"No transcript for {video['channel']} · {video['title']} ({reason}); summarized from the description")
        text, source = video["description"].strip() or "(empty description)", "description"
    if progress:
        progress(f"Summarizing · {video['channel']} · {video['title'][:60]}")
    try:
        result = claude_summary(cfg, video, text, source != "description")
    except Exception as exc:
        notes.append(f"Summary failed for {video['channel']} · {video['title']} ({exc}); will retry next run")
        return None
    result["source"] = source
    atomic_json(path, result)
    return result


# ── Report ───────────────────────────────────────────────────────────────
def window_start(state, now, cfg):
    if not state.get("last_sent_at"):
        return now - timedelta(hours=cfg["first_run_hours"])
    # Overlap the previous run: feeds can list a video hours late, and videos may wait for captions.
    overlap = timedelta(hours=cfg["transcript_wait_hours"] + 12)
    return max(parse_time(state["last_sent_at"]) - overlap, now - timedelta(days=cfg["max_lookback_days"]))


def build_report(cfg, state, progress=None):
    zone = ZoneInfo(cfg["timezone"])
    now = datetime.now(timezone.utc)
    notes, deferred, skipped = [], [], 0
    session = requests.Session()
    key = keychain(cfg["youtube"]["keychain_service"], cfg["youtube"]["keychain_account"])
    if not key:
        notes.append("No YouTube Data API key in Keychain; used RSS/yt-dlp fallbacks")
    if progress:
        progress("Checking channels…")
    since = window_start(state, now, cfg)
    sent = state.get("sent", {})
    members_path = DATA / "members_only.json"
    members = read_json(members_path, {})
    candidates = [v for v in fetch_videos(cfg, session, key, notes, {**sent, **members})
                  if v["id"] and v["id"] not in sent and v["id"] not in members and parse_time(v["published"]) >= since]
    videos = []
    for video in sorted(candidates, key=lambda v: v["published"]):
        if video["live"] in ("live", "upcoming"):
            deferred.append(f"{video['channel']} · {video['title']} ({video['live']} stream)")
            continue
        if cfg["skip_shorts"] and is_short(session, video):
            skipped += 1
            continue
        result = summarize(cfg, video, now, notes, deferred, progress)
        if result == MEMBERS_ONLY:
            members[video["id"]] = video["published"]
            atomic_json(members_path, members)
        elif result:
            videos.append({**{k: video[k] for k in ("id", "title", "channel", "published", "duration")},
                           "url": f"https://www.youtube.com/watch?v={video['id']}", **result})
    videos.sort(key=video_order)
    local = now.astimezone(zone)
    return {"generated_at": local.isoformat(timespec="seconds"), "date": str(local.date()),
            "channels": len(cfg["channels"]), "videos": videos, "skipped_shorts": skipped,
            "deferred": deferred, "notes": notes}


def is_chinese(video):
    return bool(CJK.search(video["title"] + video["summary"]))


def video_order(video):
    """Chinese videos first, then English; grouped by channel, oldest first."""
    return (not is_chinese(video), video["channel"].lower(), video["published"])


def duration_text(seconds):
    if not seconds:
        return ""
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def local_time(value, zone):
    stamp = parse_time(value).astimezone(ZoneInfo(zone))
    return f"{stamp.strftime('%b')} {stamp.day}, {stamp.strftime('%H:%M')}"


def meta_line(video, zone):
    parts = [local_time(video["published"], zone), duration_text(video["duration"])]
    parts.append({"whisper": "transcribed locally", "description": "from description only"}.get(video.get("source"), ""))
    return " · ".join(p for p in parts if p)


def footer_lines(report):
    lines = []
    if report["skipped_shorts"]:
        lines.append(f"Skipped {report['skipped_shorts']} Shorts.")
    lines += [f"Next time: {item}" for item in report["deferred"]]
    lines += [f"Data note: {note}" for note in report["notes"]]
    return lines


def render(report, console, zone, full=True, header=True):
    count = len(report["videos"])
    console.print()
    if header:  # terminal only; the email subject already says this
        console.print("  YOUTUBE SUMMARY", style="bold cyan")
        console.print(f"  {report['generated_at']} · {report['channels']} channels · "
                      f"{count} new {'video' if count == 1 else 'videos'}", style="dim")
    channel = None
    for video in report["videos"]:
        if video["channel"] != channel:
            channel = video["channel"]
            console.print(f"\n  {channel}", style="bold magenta")
        console.print(Padding(Text(video["title"], style="bold"), (0, 0, 0, 4)))
        console.print(Padding(Text(f"{meta_line(video, zone)} · {video['url']}", style="dim"), (0, 0, 0, 4)))
        if full:
            console.print(Padding(Text(video["summary"], overflow="fold"), (0, 0, 0, 4)))
            points = Table.grid(padding=(0, 1))
            points.add_column(no_wrap=True)
            points.add_column(overflow="fold")  # Chinese has no spaces to wrap at
            for point in video["key_points"]:
                points.add_row("•", Text(point))
            console.print(Padding(points, (0, 0, 0, 6)))
            console.print()
    if not count:
        console.print("\n  No new videos.", style="yellow")
    for line in footer_lines(report):
        console.print(Text("  " + line, style="yellow" if line.startswith("Data note") else "dim"))
    console.print()


# Unquoted on purpose: the value sits inside a single-quoted style attribute.
FONT = "-apple-system,BlinkMacSystemFont,PingFang SC,Hiragino Sans GB,Microsoft YaHei,Segoe UI,Helvetica,Arial,sans-serif"
DARK_MODE = """<style>
@media (prefers-color-scheme: dark) {
  .page { background:#111418 !important; }
  .card { background:#1c2026 !important; border-color:#2c323a !important; }
  .title { color:#9fc4cc !important; }
  .text { color:#c9d1d9 !important; }
  .muted { color:#8b949e !important; }
  .channel { color:#d4a27f !important; border-color:#d4a27f !important; }
}
</style>"""


def html_report(report, zone):
    esc = html.escape
    body = ["<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='color-scheme' content='light dark'><meta name='supported-color-schemes' content='light dark'>"
            f"{DARK_MODE}</head><body style='margin:0;padding:0'>",
            f"<div class='page' style='background:#f3f5f8;padding:20px 12px;font-family:{FONT};color:#1f2933'>",
            "<div style='max-width:680px;margin:0 auto'>"]
    channel = None
    for video in report["videos"]:
        chinese = is_chinese(video)
        if video["channel"] != channel:
            channel = video["channel"]
            count = sum(v["channel"] == channel for v in report["videos"])
            top = 8 if len(body) == 3 else 32
            body.append(f"<div class='channel' style='margin:{top}px 0 12px;padding-left:10px;border-left:4px solid #d4a27f;"
                        f"font-size:19px;font-weight:700;color:#d4a27f'>{esc(channel)}"
                        f"<span class='muted' style='font-size:13px;font-weight:400;color:#7b8794'>&nbsp;·&nbsp;"
                        f"{count} {'个视频' if chinese else ('video' if count == 1 else 'videos')}</span></div>")
        points = "".join(f"<li style='margin:0 0 9px'>{esc(p)}</li>" for p in video["key_points"])
        body.append(
            "<div class='card' style='background:#ffffff;border:1px solid #e3e8ef;border-radius:12px;padding:20px 22px;margin:0 0 14px'>"
            f"<a class='title' href='{esc(video['url'])}' style='font-size:18px;line-height:1.45;font-weight:700;"
            f"color:#3d6470;text-decoration:none'>{esc(video['title'])}</a>"
            f"<div class='muted' style='font-size:13px;color:#7b8794;margin:6px 0 14px'>{esc(meta_line(video, zone))}</div>"
            f"<p class='text' style='font-size:16px;line-height:1.75;color:#334e68;margin:0 0 16px'>{esc(video['summary'])}</p>"
            f"<ul class='text' style='font-size:15px;line-height:1.7;color:#334e68;margin:0;padding-left:20px'>{points}</ul>"
            "</div>")
    if not report["videos"]:
        body.append("<p class='text' style='font-size:15px'>No new videos.</p>")
    footer = footer_lines(report)
    if footer:
        body.append("<div style='margin-top:26px'>")
        for line in footer:
            color = "#9a6700" if line.startswith("Data note") else "#7b8794"
            body.append(f"<p class='muted' style='color:{color};font-size:12px;line-height:1.5;margin:3px 0'>{esc(line)}</p>")
        body.append("</div>")
    body.append("<p class='muted' style='color:#9aa5b1;font-size:12px;margin:22px 0 0'>Summaries by Claude from video transcripts; "
                "check the video before relying on specific figures.</p></div></div></body></html>")
    return "\n".join(body)


def save_report(report, zone):
    REPORTS.mkdir(exist_ok=True)
    atomic_json(REPORTS / "latest.json", report)
    (REPORTS / "latest.html").write_text(html_report(report, zone))
    output = io.StringIO()
    render(report, Console(file=output, width=110, color_system=None), zone, header=False)
    (REPORTS / "latest.txt").write_text(output.getvalue())
    return output.getvalue()


# ── Email ────────────────────────────────────────────────────────────────
def mail_settings(cfg):
    email = cfg.get("email", {})
    missing = [k for k in ("to", "sender", "host", "port", "username", "keychain_service", "keychain_account") if not email.get(k)]
    if missing:
        raise ValueError("Missing email settings: " + ", ".join(missing))
    return email


def send_report(cfg, report, plain):
    email = mail_settings(cfg)
    password = keychain(email["keychain_service"], email["keychain_account"])
    if not password:
        raise RuntimeError("SMTP password unavailable in macOS Keychain")
    message = EmailMessage()
    count = len(report["videos"])
    message["Subject"] = (f"YouTube summary · {report['date']} · {count} {'video' if count == 1 else 'videos'}"
                          + (" · data gaps" if report["notes"] else ""))
    message["From"] = formataddr((email.get("sender_name", "YouTube Summary"), email["sender"]))
    message["To"] = email["to"]
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()
    message.set_content(plain)
    message.add_alternative(html_report(report, cfg["timezone"]), subtype="html")
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if email["port"] == 465 else smtplib.SMTP
    kwargs = {"context": context} if email["port"] == 465 else {}
    with factory(email["host"], email["port"], timeout=30, **kwargs) as smtp:
        if email["port"] != 465:
            smtp.starttls(context=context)
        smtp.login(email["username"], password)
        smtp.send_message(message)


def record_sent(state, report):
    now = datetime.now(timezone.utc)
    sent = {k: v for k, v in state.get("sent", {}).items() if now - parse_time(v) < timedelta(days=60)}
    for video in report["videos"]:
        sent[video["id"]] = now.isoformat(timespec="seconds")
    state.update(sent=sent, last_sent_at=now.isoformat(timespec="seconds"), last_sent_date=report["date"])
    atomic_json(DATA / "state.json", state)


# ── Runs ─────────────────────────────────────────────────────────────────
@contextmanager
def run_lock(wait_seconds=600):
    DATA.mkdir(exist_ok=True)
    with (DATA / "run.lock").open("a") as lock:
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another run is still going after ten minutes")
                time.sleep(1)
        yield


def run_once(cfg, console, send=False, daily=False):
    with run_lock():
        if send:
            mail_settings(cfg)
        state = read_json(DATA / "state.json", {})
        today = str(datetime.now(ZoneInfo(cfg["timezone"])).date())
        if daily and state.get("last_sent_date") == today:
            return False
        quiet = send and not console.is_terminal
        if quiet:
            report = build_report(cfg, state)
        else:
            with console.status("Checking channels…") as status:
                report = build_report(cfg, state, status.update)
        plain = save_report(report, cfg["timezone"])
        if not quiet:
            render(report, console, cfg["timezone"], full=not send)
        if send:
            if report["videos"] or cfg["send_when_empty"]:
                send_report(cfg, report, plain)
                console.print("Email sent.", style="green")
            else:
                console.print("No new videos; no email sent.", style="dim")
            record_sent(state, report)
        return True


def next_send(now, cfg, state):
    target = datetime.combine(now.date(), send_time(cfg), tzinfo=now.tzinfo)
    if now >= target and state.get("last_sent_date") == str(now.date()):
        target += timedelta(days=1)
    return target


def wait_until(target):
    while True:
        remaining = (target - datetime.now(target.tzinfo)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 60))  # short sleeps so a Mac waking from sleep catches up promptly


def watch(args, console):
    while True:
        cfg = configured(args)
        zone = ZoneInfo(cfg["timezone"])
        target = next_send(datetime.now(zone), cfg, read_json(DATA / "state.json", {}))
        if target > datetime.now(zone):
            console.print(f"Next email: {target.strftime('%A, %B')} {target.day} at {target.strftime('%H:%M')} "
                          f"{cfg['timezone']} · Press Ctrl+C to stop.", style="dim")
            wait_until(target)
            cfg = configured(args)
        try:
            run_once(cfg, console, send=True, daily=True)
        except Exception as exc:
            console.print(Text(f"Run failed: {exc}. Retrying in 30 minutes.", style="red"))
            time.sleep(1800)


def configured(args):
    cfg = load_config(args.config)
    validate(cfg)
    return cfg


def store_api_key(cfg):
    yt = cfg["youtube"]
    print("Paste your YouTube Data API key at the prompt (input is hidden).")
    result = subprocess.run(["/usr/bin/security", "add-generic-password", "-U", "-s", yt["keychain_service"],
                             "-a", yt["keychain_account"], "-w"])
    if result.returncode:
        raise RuntimeError("Keychain did not save the key")
    print("Saved to Keychain.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Summarize new videos from followed YouTube channels.")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="Keep running; email daily at send_time")
    mode.add_argument("--once", action="store_true", help="Print new-video summaries without emailing (the default)")
    mode.add_argument("--email", action="store_true", help="Summarize and email now")
    mode.add_argument("--daily", action="store_true", help="Email at most once per local date")
    mode.add_argument("--last", action="store_true", help="Show the last saved report")
    mode.add_argument("--set-key", action="store_true", help="Save a YouTube Data API key to Keychain")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args(argv)
    console = Console(no_color=args.no_color, highlight=False)
    try:
        if args.last:
            cfg = load_config(args.config)
            render(json.loads((REPORTS / "latest.json").read_text()), console, cfg["timezone"])
        elif args.set_key:
            store_api_key(load_config(args.config))
        elif args.watch:
            watch(args, console)
        else:
            run_once(configured(args), console, send=args.email or args.daily, daily=args.daily)
        return 0
    except KeyboardInterrupt:
        console.print("Stopped.", style="dim")
        return 0
    except Exception as exc:
        console.print(Text(f"Error: {exc}", style="red"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
