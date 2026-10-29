# youtube-cli

A highly restricted version of youtube made for me.

A tiny CLI that can browse a whitelisted channel's last 5 videos.

## Features

- Whitelist channels that you can view.
- View whitelisted channel's 5 most recently uploaded videos.
- Save videos to watch later.

## Requirements

- Python 3
- A YouTube Data API v3 keys (see .env.example)

## Usage

```sh
.venv/bin/python ytcli.py
```

Menus are navigated with the **arrow keys**: up/down to move, Enter/right to
select (open a video or search a channel), left/Esc to go back, and `q` to
quit. On video lists, press:

- `s` — save the highlighted video to `watch-later.txt`
- `w` — toggle the highlighted video as watched (search results)
- `d` — remove the highlighted video from `watch-later.txt`

Long lists scroll automatically as the cursor moves. When input is not a
terminal (e.g. piping), the menus fall back to the older numbered prompts
(`s1,s3` batch syntax still works there).

## Notes

This project was made with AI, hence the repo name.
