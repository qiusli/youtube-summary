# YouTube summary

Emails a daily digest of new videos from the YouTube channels you follow. Each
video gets a short summary and key points, written by Claude from the video's
transcript. Everything runs on this Mac.

## Run

```sh
youtube-summary start
```

`start` (or `yts start`) keeps running in Ghostty and emails the digest at
`send_time` (default 7:00 a.m. America/Vancouver) each day. If the Mac was
asleep or the command wasn't running at that time, it sends as soon as it can
that day. Press Ctrl+C to stop.

```sh
youtube-summary once      # Print summaries of new videos; no email
youtube-summary email     # Summarize and email now
youtube-summary daily     # Email at most once today, then exit
youtube-summary last      # Show the last saved report
youtube-summary key       # Save a YouTube Data API key to Keychain
youtube-summary config    # Open config.toml
```

For a new installation use Python 3.11 or newer:
`PYTHON_BIN=/path/to/python3 ./setup`.

## Channels

List channels in `config.toml` as `@handle`, a channel URL, or a `UC…` ID.
Changes take effect at the next run.

## How it works

1. **Find new videos.** It uses the official YouTube Data API when a key is
   saved, and falls back to the channel's RSS feed, then to `yt-dlp` (the
   channel's Videos tab). A failed source is noted in the email.
2. **Skip what doesn't fit.** Shorts and members-only videos are skipped.
   Members-only videos are found through the channel's members playlist (API),
   yt-dlp's availability flag, or the "Join this channel" error when the audio
   download is refused (RSS lists them without saying so); those IDs are
   remembered in `data/members_only.json`. Live and upcoming streams are held
   until they finish.
3. **Get the transcript.** `youtube-transcript-api` fetches YouTube's
   captions. When a video has none yet (auto-captions can take hours), its
   audio is downloaded with `yt-dlp` and transcribed on this Mac with
   `mlx-whisper` (large-v3-turbo, about 40 s for a 12-minute video on the
   M2 Pro; the audio is deleted afterwards). These are labeled "transcribed
   locally". If both fail, the video waits up to `transcript_wait_hours`
   (default 24) and is then summarized from its description, labeled "from
   description only". Whisper needs `ffmpeg` (`brew install ffmpeg`); its
   model (~1.6 GB) downloads on first use.
4. **Summarize.** `claude -p` (your Claude login, no API key) writes the
   summary. Summaries are cached in `data/summaries/`.
5. **Email.** Gmail SMTP, reusing Insider Buy Alert's app password from macOS
   Keychain. Sent video IDs are recorded in `data/state.json` so nothing is
   sent twice. A run with no new videos sends nothing unless
   `send_when_empty = true`.

Each run looks back to 36 hours before the last email (24 for the first one,
at most 7 days), so late-appearing videos and the Mac being off for a few days
don't cause misses. Every run saves `reports/latest.html`, `.txt` and `.json`.

## YouTube Data API key (optional, recommended)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a
   project, enable **YouTube Data API v3**, and create an API key under
   *Credentials*. Restricting the key to that API is a good idea.
2. Run `youtube-summary key` and paste the key. It is stored in Keychain, not
   in this folder.

The free quota is 10,000 units a day; each channel uses about 2 per run.

## Claude login for unattended runs (recommended)

`claude -p` normally uses your interactive Claude login, which can expire and
fail to refresh while nobody is at the Mac. Create a token that lasts a year
and save it to Keychain:

```sh
claude setup-token
youtube-summary claude-token
```

When the token is present it is passed to `claude -p` as
`CLAUDE_CODE_OAUTH_TOKEN`; otherwise the regular login is used.

## Verification

```sh
.venv/bin/python -m unittest -v test_summary.py
```
