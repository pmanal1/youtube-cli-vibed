# YouTube Channel CLI — Plan

## Goal
A simple CLI tool (future GUI/TUI candidate) that:
1. Searches the 5 most recent videos from a given YouTube channel
2. Lets the user pick one and opens it in the browser to watch

## Approach
Built in **Python**, using the official YouTube Data API v3 (free tier, API key
from Google Cloud Console). Videos are opened in the browser rather than
downloaded — no ToS gray area, no yt-dlp dependency, minimal code. A
`download` command can be added later if offline viewing ever matters.

## Architecture

1. **Config**: YouTube API key stored in `.env` (never committed)
2. **`search <channel>`**:
   - Resolve channel handle/name → channel ID (`channels.list` with `forHandle`, or search fallback)
   - Get the channel's "uploads" playlist ID (derived from channel resource)
   - Call `playlistItems.list` (maxResults=5) on the uploads playlist
   - Display: title, published date, video ID, duration, thumbnail URL
3. **`open <video>`**:
   - Build URL: `https://youtube.com/watch?v=VIDEO_ID`
   - Launch in default browser

> Quota note: `playlistItems.list` costs ~1 unit vs ~100 for `search.list` —
> important on the free tier (10,000 units/day).

## Stack

- **Language**: Python 3
- **API calls**: `requests`
- **CLI framework**: `argparse` (stdlib) — can migrate to `typer` if it grows
- **Browser opening**: `webbrowser` (stdlib, cross-platform)
- **Config**: `python-dotenv`

## MVP Scope

`search <channel>` → list 5 recent videos → user picks one → opens in browser.

That's the whole v1.
