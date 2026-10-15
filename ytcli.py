#!/usr/bin/env python3
"""ytcli — list a YouTube channel's recent videos and open them in the browser.

Usage:
    python ytcli.py                   # full-screen menu: search, watch later,
                                      # quit at any point
    python ytcli.py search [channel]  # list the 5 most recent videos; omit
                                      # the channel to pick from whitelist.txt
    python ytcli.py open <video>      # open a video (ID or URL) in the browser
    python ytcli.py later             # list videos saved in watch-later.txt
                                      # and open one
    python ytcli.py grabbag           # import new videos from the "AI Grab Bag"
                                      # playlist into watch-later.txt, then remove
                                      # them from the playlist (--keep to import
                                      # only, --dry-run to preview)
    python ytcli.py menu              # same as no arguments

Browsing is restricted to the comma-separated channels listed in whitelist.txt.
Requires a YouTube Data API v3 key in the YOUTUBE_API_KEY environment variable
or a .env file (see .env.example). Free key: https://console.cloud.google.com/
Account commands (grabbag) additionally need OAuth client credentials
(YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET, type "Desktop app") and open a
browser once for authorization; the token is cached in token.json.
"""

import argparse
import http.server
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

API_BASE = "https://www.googleapis.com/youtube/v3"
MAX_RESULTS = 5
FETCH_LIMIT = 25
SHORTS_MAX_SECONDS = 60
WATCH_URL = "https://youtube.com/watch?v={}"
WHITELIST_FILE = "whitelist.txt"
WATCH_LATER_FILE = "watch-later.txt"
GRAB_BAG_PLAYLIST = "AI Grab Bag"
TOKEN_FILE = "token.json"
OAUTH_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
OAUTH_TIMEOUT_SECONDS = 300
CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")
VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")
DURATION_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
CLEAR_SCREEN = "\033[H\033[2J\033[3J"
QUIT_WORDS = {"q", "quit", "exit"}
BACK_WORDS = {"b", "back"}


class CliError(Exception):
    pass


class ApiError(CliError):
    pass


class QuitApp(Exception):
    pass


def get_api_key():
    load_dotenv()
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise CliError(
            "Missing YOUTUBE_API_KEY. Set it in your environment or in a .env file "
            "(copy .env.example). Get a free key: https://console.cloud.google.com/"
        )
    return key


def make_session():
    session = requests.Session()
    session.params["key"] = get_api_key()
    return session


def oauth_client_credentials():
    load_dotenv()
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "").strip()
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise CliError(
            "Missing YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET. Create an OAuth "
            "client ID (type \"Desktop app\") at https://console.cloud.google.com/ "
            "and add both values to your .env file (see .env.example). They are "
            "needed for account commands such as grabbag."
        )
    return client_id, client_secret


def token_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), TOKEN_FILE)


def load_stored_token():
    path = token_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("access_token"):
                return data
        except (OSError, ValueError):
            pass
    return None


def save_stored_token(token):
    path = token_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(token, fh)
    os.chmod(path, 0o600)


def _token_response(resp):
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != 200 or "access_token" not in data:
        message = (
            data.get("error_description") or data.get("error")
            or resp.text.strip() or resp.reason
        )
        raise ApiError(
            f"OAuth token request failed (HTTP {resp.status_code}): {message}"
        )
    data["expires_at"] = int(time.time()) + int(data.get("expires_in", 3600))
    return data


def exchange_code_for_token(client_id, client_secret, code, redirect_uri):
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=15,
    )
    return _token_response(resp)


def refresh_access_token(client_id, client_secret, refresh_token):
    resp = requests.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    return _token_response(resp)


class _AuthRedirectHandler(http.server.BaseHTTPRequestHandler):
    result = None

    def do_GET(self):
        _AuthRedirectHandler.result = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><h2>Authorized!</h2>You can close this tab and "
            b"return to the terminal.</body></html>"
        )

    def log_message(self, *args):
        pass


def run_authorization_flow(client_id, client_secret):
    server = http.server.HTTPServer(("127.0.0.1", 0), _AuthRedirectHandler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"
    state = secrets.token_urlsafe(16)
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    url = f"{AUTH_URL}?{query}"
    print("A browser window will open to authorize access to your YouTube account.")
    print(f"If it does not open, visit this URL manually:\n{url}")
    webbrowser.open(url)
    print("\nWaiting for authorization (times out after 5 minutes)...")
    server.timeout = 1
    deadline = time.time() + OAUTH_TIMEOUT_SECONDS
    try:
        while _AuthRedirectHandler.result is None and time.time() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        raise QuitApp()
    finally:
        server.server_close()
    result = _AuthRedirectHandler.result
    _AuthRedirectHandler.result = None
    if not result:
        raise CliError("No authorization received from the browser.")
    if result.get("state", [""])[0] != state:
        raise CliError("OAuth state mismatch; aborting.")
    if "error" in result:
        raise CliError(f"Authorization failed: {result['error'][0]}")
    if "code" not in result:
        raise CliError("No authorization code returned by Google.")
    token = exchange_code_for_token(
        client_id, client_secret, result["code"][0], redirect_uri
    )
    if not token.get("refresh_token"):
        raise CliError(
            "Google did not return a refresh token. If this keeps happening, "
            "remove this app's access at "
            "https://myaccount.google.com/connections and try again."
        )
    return token


def get_access_token():
    client_id, client_secret = oauth_client_credentials()
    token = load_stored_token()
    if token and token.get("expires_at", 0) > time.time() + 60:
        return token["access_token"]
    if token and token.get("refresh_token"):
        try:
            fresh = refresh_access_token(
                client_id, client_secret, token["refresh_token"]
            )
        except (ApiError, requests.RequestException):
            fresh = None
        if fresh:
            fresh["refresh_token"] = (
                fresh.get("refresh_token") or token["refresh_token"]
            )
            save_stored_token(fresh)
            return fresh["access_token"]
    token = run_authorization_flow(client_id, client_secret)
    save_stored_token(token)
    return token["access_token"]


def make_oauth_session():
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {get_access_token()}"
    return session


def load_whitelist():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for base in (script_dir, os.getcwd()):
        path = os.path.join(base, WHITELIST_FILE)
        if os.path.isfile(path):
            entries = []
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    for part in line.split("#", 1)[0].split(","):
                        entry = part.strip()
                        if entry and entry not in entries:
                            entries.append(entry)
            if not entries:
                raise CliError(
                    f"{path} has no channels. Add a comma-separated list of "
                    "allowed channels to it."
                )
            return entries
    raise CliError(
        f"{WHITELIST_FILE} not found. Create it next to ytcli.py with a "
        "comma-separated list of channels you're allowed to browse."
    )


def check_whitelist(query, whitelist):
    target = query.strip().lstrip("@").lower()
    for entry in whitelist:
        if entry.strip().lstrip("@").lower() == target:
            return entry
    raise CliError(
        f"Channel not allowed: {query}\n"
        f"Allowed channels ({WHITELIST_FILE}): {', '.join(whitelist)}"
    )


def clear_screen():
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.flush()


def read_input(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise QuitApp()


def api_get(session, endpoint, **params):
    resp = session.get(f"{API_BASE}/{endpoint}", params=params, timeout=15)
    if resp.status_code != 200:
        try:
            message = resp.json()["error"]["message"]
        except (ValueError, KeyError):
            message = resp.text.strip() or resp.reason
        raise ApiError(f"{endpoint} failed (HTTP {resp.status_code}): {message}")
    return resp.json()


def api_call(session, method, endpoint, json_body=None, **params):
    query = {key: value for key, value in params.items() if value not in ("", None)}
    resp = session.request(
        method,
        f"{API_BASE}/{endpoint}",
        json=json_body,
        params=query or None,
        timeout=15,
    )
    if resp.status_code not in (200, 201, 204):
        try:
            message = resp.json()["error"]["message"]
        except (ValueError, KeyError):
            message = resp.text.strip() or resp.reason
        raise ApiError(
            f"{endpoint} {method} failed (HTTP {resp.status_code}): {message}"
        )
    return resp.json() if resp.content else {}


def resolve_channel_id(session, query):
    query = query.strip()
    if CHANNEL_ID_RE.fullmatch(query):
        data = api_get(session, "channels", part="id", id=query)
        if data.get("items"):
            return query

    handle = query if query.startswith("@") else f"@{query}"
    data = api_get(session, "channels", part="id", forHandle=handle)
    if data.get("items"):
        return data["items"][0]["id"]

    data = api_get(
        session, "search", part="snippet", type="channel", maxResults=1, q=query
    )
    items = data.get("items")
    if items:
        return items[0]["snippet"]["channelId"]
    raise ApiError(f"Channel not found: {query}")


def duration_to_seconds(iso_duration):
    match = DURATION_RE.fullmatch(iso_duration or "")
    if not match:
        return None
    days, hours, minutes, seconds = (int(x) if x else 0 for x in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def format_duration(iso_duration):
    total = duration_to_seconds(iso_duration)
    if total is None:
        return "?"
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def fetch_recent_videos(session, channel_id):
    data = api_get(session, "channels", part="contentDetails", id=channel_id)
    items = data.get("items")
    if not items:
        raise ApiError(f"Channel not found: {channel_id}")
    uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    data = api_get(
        session,
        "playlistItems",
        part="snippet,contentDetails",
        playlistId=uploads,
        maxResults=FETCH_LIMIT,
    )
    videos = [
        {
            "id": item["contentDetails"]["videoId"],
            "title": item["snippet"]["title"],
            "published": item["snippet"]["publishedAt"][:10],
            "thumbnail": item["snippet"]["thumbnails"].get("medium", {}).get("url", ""),
        }
        for item in data.get("items", [])
    ]
    if not videos:
        raise ApiError("No videos found on this channel.")

    ids = ",".join(v["id"] for v in videos)
    data = api_get(session, "videos", part="contentDetails", id=ids)
    durations = {
        item["id"]: item["contentDetails"]["duration"] for item in data.get("items", [])
    }
    regular = []
    for video in videos:
        seconds = duration_to_seconds(durations.get(video["id"], ""))
        video["duration"] = format_duration(durations.get(video["id"], ""))
        if seconds is None or seconds > SHORTS_MAX_SECONDS:
            regular.append(video)
    if not regular:
        raise ApiError(
            f"Only shorts found among the channel's {len(videos)} most recent uploads."
        )
    return regular[:MAX_RESULTS]


def print_videos(videos):
    for i, video in enumerate(videos, 1):
        print(f"{i}. {video['title']}")
        line = f"   {video['published']}  {video['duration']}"
        if video.get("thumbnail"):
            line += f"  thumb={video['thumbnail']}"
        print(f"{line}  id={video['id']}")


def extract_video_id(text):
    text = text.strip()
    if VIDEO_ID_RE.fullmatch(text):
        return text
    parsed = urlparse(text)
    if parsed.netloc:
        params = parse_qs(parsed.query)
        if "v" in params:
            return params["v"][0]
        last = parsed.path.rstrip("/").split("/")[-1]
        if last and last != "watch":
            return last
    return None


def open_video(video_id):
    url = WATCH_URL.format(video_id)
    if webbrowser.open(url):
        return f"Opened {url}"
    return f"Could not launch a browser. Open this URL manually:\n{url}"


def parse_picks(text, count):
    picks = []
    warnings = []
    for part in text.replace(" ", "").split(","):
        match = re.search(r"\d+", part)
        if match and 1 <= int(match.group()) <= count:
            picks.append(int(match.group()))
        elif part:
            warnings.append(f"Not a valid pick, skipping: {part}")
    return picks, warnings


def watch_later_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), WATCH_LATER_FILE)


def load_watch_later():
    videos = []
    path = watch_later_path()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                parts = [part.strip() for part in line.split("|")]
                if parts and VIDEO_ID_RE.fullmatch(parts[0]):
                    videos.append(
                        {
                            "id": parts[0],
                            "title": parts[1] if len(parts) > 1 else parts[0],
                            "published": parts[2] if len(parts) > 2 else "?",
                            "duration": parts[3] if len(parts) > 3 else "?",
                        }
                    )
    return videos


def append_watch_later(videos):
    with open(watch_later_path(), "a", encoding="utf-8") as fh:
        for video in videos:
            fh.write(
                f"{video['id']} | {video['title'].replace('|', '/')} | "
                f"{video['published']} | {video['duration']}\n"
            )


def save_to_watch_later(video):
    if any(v["id"] == video["id"] for v in load_watch_later()):
        return f"Already saved: {video['title']}"
    append_watch_later([video])
    return f"Saved to {WATCH_LATER_FILE}: {video['title']}"


def remove_from_watch_later(video):
    remaining = [v for v in load_watch_later() if v["id"] != video["id"]]
    with open(watch_later_path(), "w", encoding="utf-8") as fh:
        for v in remaining:
            fh.write(
                f"{v['id']} | {v['title'].replace('|', '/')} | "
                f"{v['published']} | {v['duration']}\n"
            )
    return f"Removed from {WATCH_LATER_FILE}: {video['title']}"


def video_list_view(videos, title, allow_remove=False):
    status = ""
    while True:
        clear_screen()
        print(f"=== {title} ===\n")
        if videos:
            print_videos(videos)
            actions = f"Open [1-{len(videos)}], save [s1,s3]"
            if allow_remove:
                actions += ", remove [d1,d3]"
            prompt = f"\n{actions}, Enter=back, q=quit: "
        else:
            print(f"{WATCH_LATER_FILE} is empty. Run Search and save videos with s1,2.")
            prompt = "\nEnter=back, q=quit: "
        if status:
            print(f"\n{status}")
            status = ""
        choice = read_input(prompt).lower()
        if not choice or choice in BACK_WORDS:
            return
        if choice in QUIT_WORDS:
            raise QuitApp()
        if videos and choice[0] == "s":
            rest = choice[4:] if choice.startswith("save") else choice[1:]
            if not rest.replace(" ", "").rstrip(","):
                status = f"Usage: s1,s3 to save videos to {WATCH_LATER_FILE}."
                continue
            picks, warnings = parse_picks(rest, len(videos))
            messages = warnings + [save_to_watch_later(videos[i - 1]) for i in picks]
            status = "\n".join(messages)
            continue
        if videos and allow_remove and choice[0] == "d":
            rest = choice[6:] if choice.startswith("delete") else choice[1:]
            if not rest.replace(" ", "").rstrip(","):
                status = f"Usage: d1,d3 to remove videos from {WATCH_LATER_FILE}."
                continue
            picks, warnings = parse_picks(rest, len(videos))
            messages = warnings + [
                remove_from_watch_later(videos.pop(i - 1))
                for i in sorted(set(picks), reverse=True)
            ]
            status = "\n".join(messages)
            continue
        if videos and choice.isdigit() and 1 <= int(choice) <= len(videos):
            status = open_video(videos[int(choice) - 1]["id"])
            continue
        status = "Not a valid choice."


def watch_later_view():
    videos = load_watch_later()
    video_list_view(videos, f"Watch later ({WATCH_LATER_FILE})", allow_remove=True)


def find_account_playlist(session, title):
    page_token = ""
    while True:
        data = api_call(
            session,
            "GET",
            "playlists",
            part="snippet",
            mine="true",
            maxResults=50,
            pageToken=page_token,
        )
        for playlist in data.get("items", []):
            if playlist["snippet"]["title"].strip().lower() == title.strip().lower():
                return playlist
        page_token = data.get("nextPageToken")
        if not page_token:
            raise CliError(
                f'No playlist titled "{title}" found on your YouTube account.'
            )


def fetch_playlist_videos(session, playlist_id):
    videos = []
    page_token = ""
    while True:
        data = api_call(
            session,
            "GET",
            "playlistItems",
            part="snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=page_token,
        )
        for item in data.get("items", []):
            snippet = item["snippet"]
            video_id = item.get("contentDetails", {}).get("videoId", "")
            published = (
                item.get("contentDetails", {}).get("videoPublishedAt")
                or snippet.get("publishedAt")
                or ""
            )[:10]
            videos.append(
                {
                    "id": video_id if VIDEO_ID_RE.fullmatch(video_id) else "",
                    "title": snippet["title"],
                    "published": published or "?",
                    "item_id": item["id"],
                }
            )
        page_token = data.get("nextPageToken")
        if not page_token:
            return videos


def fill_durations(session, videos):
    for start in range(0, len(videos), 50):
        chunk = videos[start:start + 50]
        data = api_call(
            session,
            "GET",
            "videos",
            part="contentDetails",
            id=",".join(video["id"] for video in chunk),
        )
        durations = {
            item["id"]: item["contentDetails"]["duration"]
            for item in data.get("items", [])
        }
        for video in chunk:
            video["duration"] = format_duration(durations.get(video["id"], ""))
    for video in videos:
        video.setdefault("duration", "?")


def build_grab_bag_plan(session):
    playlist = find_account_playlist(session, GRAB_BAG_PLAYLIST)
    items = fetch_playlist_videos(session, playlist["id"])
    saved_ids = {video["id"] for video in load_watch_later()}
    fresh = []
    seen = set()
    already_saved = 0
    invalid = 0
    for item in items:
        if not item["id"]:
            invalid += 1
        elif item["id"] in saved_ids or item["id"] in seen:
            already_saved += 1
        else:
            seen.add(item["id"])
            fresh.append(item)
    return {
        "playlist": playlist,
        "items": items,
        "fresh": fresh,
        "already_saved": already_saved,
        "invalid": invalid,
    }


def describe_plan(plan, remove=True):
    fresh = plan["fresh"]
    lines = [
        f'Playlist "{GRAB_BAG_PLAYLIST}" on your account: '
        f"{len(plan['items'])} video(s)."
    ]
    if fresh:
        lines.append(f"To import into {WATCH_LATER_FILE}:")
        lines.extend(f"  {i}. {video['title']}" for i, video in enumerate(fresh, 1))
    else:
        lines.append(f"Nothing new to import into {WATCH_LATER_FILE}.")
    if plan["already_saved"]:
        lines.append(
            f"{plan['already_saved']} video(s) already in {WATCH_LATER_FILE} "
            "(skipped, no duplicates added)."
        )
    if plan["invalid"]:
        lines.append(
            f"{plan['invalid']} unusable item(s) (deleted or private videos)."
        )
    if remove and plan["items"]:
        lines.append(
            f"All {len(plan['items'])} item(s) will be removed from the "
            "playlist afterwards."
        )
    return "\n".join(lines)


def delete_playlist_items(session, item_ids):
    removed = 0
    failed = []
    for item_id in item_ids:
        try:
            api_call(session, "DELETE", "playlistItems", id=item_id)
            removed += 1
        except (ApiError, requests.RequestException) as err:
            failed.append(f"{item_id}: {err}")
    return removed, failed


def execute_grab_bag_plan(session, plan, remove=True):
    fresh = plan["fresh"]
    result = {"imported": 0, "removed": 0, "failed": []}
    if fresh:
        fill_durations(session, fresh)
        append_watch_later(fresh)
        result["imported"] = len(fresh)
    if remove and plan["items"]:
        result["removed"], result["failed"] = delete_playlist_items(
            session, [item["item_id"] for item in plan["items"]]
        )
    return result


def grab_bag_view():
    session = make_oauth_session()
    plan = build_grab_bag_plan(session)
    clear_screen()
    print(f'=== Sync "{GRAB_BAG_PLAYLIST}" -> {WATCH_LATER_FILE} ===\n')
    print(describe_plan(plan))
    if not plan["items"]:
        print("\nNothing to do.")
    else:
        choice = read_input(
            f"\nImport {len(plan['fresh'])} video(s) and clear the playlist? "
            "[y/N]: "
        ).lower()
        if choice not in ("y", "yes"):
            print("Cancelled.")
        else:
            result = execute_grab_bag_plan(session, plan)
            print(
                f"\nImported {result['imported']} video(s) into "
                f"{WATCH_LATER_FILE}, removed {result['removed']} from the "
                "playlist."
            )
            for failure in result["failed"]:
                print(f"Failed to remove: {failure}")
    read_input("\nPress Enter to go back... ")


def grab_bag_command(keep=False, dry_run=False):
    session = make_oauth_session()
    plan = build_grab_bag_plan(session)
    print(describe_plan(plan, remove=not keep))
    if not plan["items"]:
        print("\nNothing to do.")
        return
    if dry_run:
        print("\nDry run: nothing was changed.")
        return
    result = execute_grab_bag_plan(session, plan, remove=not keep)
    print(
        f"\nDone. Imported {result['imported']} video(s) into "
        f"{WATCH_LATER_FILE}, removed {result['removed']} from the playlist."
    )
    for failure in result["failed"]:
        print(f"Failed to remove: {failure}")


def search_view():
    whitelist = load_whitelist()
    status = ""
    while True:
        clear_screen()
        print("=== Search ===\n")
        print("Allowed channels:")
        for i, entry in enumerate(whitelist, 1):
            print(f"  {i}. {entry}")
        if status:
            print(f"\n{status}")
            status = ""
        choice = read_input(
            f"\nChannel? [1-{len(whitelist)}, name/@handle/ID, Enter=back, q=quit] "
        )
        if not choice or choice.lower() in BACK_WORDS:
            return
        if choice.lower() in QUIT_WORDS:
            raise QuitApp()
        if choice.isdigit() and 1 <= int(choice) <= len(whitelist):
            channel = whitelist[int(choice) - 1]
        else:
            try:
                channel = check_whitelist(choice, whitelist)
            except CliError as err:
                status = str(err)
                continue
        try:
            session = make_session()
            channel_id = resolve_channel_id(session, channel)
            videos = fetch_recent_videos(session, channel_id)
        except (CliError, requests.RequestException) as err:
            status = f"Error: {err}"
            continue
        video_list_view(
            videos, f"{len(videos)} most recent videos - {channel} ({channel_id})"
        )


def run_menu():
    status = ""
    while True:
        clear_screen()
        print("=== ytcli ===")
        print("  1. Search a channel")
        print("  2. Watch later")
        print(f'  3. Sync "{GRAB_BAG_PLAYLIST}" -> watch later')
        print("  q. Quit")
        if status:
            print(f"\n{status}")
            status = ""
        try:
            choice = read_input("\nChoose an option [1-3, Enter=q]: ").lower()
            if not choice or choice in QUIT_WORDS:
                raise QuitApp()
            if choice == "1":
                search_view()
            elif choice == "2":
                watch_later_view()
            elif choice == "3":
                grab_bag_view()
            else:
                status = "Unknown option."
        except (CliError, requests.RequestException) as err:
            status = f"Error: {err}"


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ytcli",
        description="List a YouTube channel's recent videos and open them in your browser.",
    )
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser(
        "search",
        help="list the 5 most recent videos of an allowed channel (shorts excluded);"
        " omit the channel to pick from the whitelist",
    )
    p_search.add_argument(
        "channel",
        nargs="?",
        help="allowed channel handle, name, or ID (see whitelist.txt)",
    )

    p_open = sub.add_parser("open", help="open a video (ID or URL) in the browser")
    p_open.add_argument("video", help="video ID or URL")

    sub.add_parser(
        "later",
        aliases=["watch-later"],
        help=f"list videos saved in {WATCH_LATER_FILE} and open one",
    )

    p_grab = sub.add_parser(
        "grabbag",
        aliases=["sync-ai-grab-bag"],
        help=f'import new videos from the "{GRAB_BAG_PLAYLIST}" playlist into '
        f"{WATCH_LATER_FILE}, then remove them from the playlist",
    )
    p_grab.add_argument(
        "--keep",
        action="store_true",
        help="import the videos but leave them in the playlist",
    )
    p_grab.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be imported and removed without changing anything",
    )

    sub.add_parser("menu", help="interactive menu (default when no command is given)")

    args = parser.parse_args(argv)
    command = args.command or "menu"

    try:
        if command == "open":
            video_id = extract_video_id(args.video)
            if not video_id:
                sys.exit(f"Not a valid video ID or URL: {args.video}")
            print(open_video(video_id))
        elif command in ("later", "watch-later"):
            watch_later_view()
        elif command in ("grabbag", "sync-ai-grab-bag"):
            grab_bag_command(keep=args.keep, dry_run=args.dry_run)
        elif command == "search":
            if args.channel:
                whitelist = load_whitelist()
                channel = check_whitelist(args.channel, whitelist)
                session = make_session()
                channel_id = resolve_channel_id(session, channel)
                videos = fetch_recent_videos(session, channel_id)
                video_list_view(
                    videos,
                    f"{len(videos)} most recent videos - {channel} ({channel_id})",
                )
            else:
                search_view()
        else:
            run_menu()
    except QuitApp:
        clear_screen()
        print("Bye!")
    except (CliError, requests.RequestException) as err:
        sys.exit(f"Error: {err}")


if __name__ == "__main__":
    main()
