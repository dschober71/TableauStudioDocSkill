# First test checklist

Preflight (done 2026-11-05): Python 3.14 + `mcp`, whisper, ffmpeg, OBS.app, Studio running (log found via the
TableauAgent process), `studio_replay.py` builds from a real session, watchdog starts and connects to Desktop.

## Before the demo
1. Bring the Tableau Studio window to the front on the current Space, not minimized. The last check reported
   "Tableau window: not found" and fell back to whole-screen shots.
2. Run the checks from the app you will launch the watchdog from (permission is per app):
   `python3 snapshot_watchdog.py --out snaps --check`
   Expect: Screen Recording granted, a Tableau window title, a test shot in `snaps/check.png`.
3. Start OBS (screen + mic) and note the wall-clock start in UTC (`date -u +%Y-%m-%dT%H:%M:%SZ`).
4. Start the watchdog: `python3 snapshot_watchdog.py --out snaps`
   Add `--on-change` for a frame after every workbook-changing tool call.

## Demo
5. Give Studio 1-2 prompts. After each turn ends and Tableau goes IDLE, the watchdog prints a capture line and
   appends to `snaps/manifest.jsonl`. Ctrl-C afterwards.

## Build
6. `whisper rec.mp4 --output_format json`
7. `python3 studio_replay.py --list` to get the session id, then
   `python3 studio_replay.py --session <id> --snapshots snaps/ --video rec.mp4 --video-start <UTC> --whisper rec.json --out my_demo`
8. `open my_demo/index.html`

## What to verify (untested so far)
- A task-done trigger fires (stop_reason end_turn, then IDLE) and writes both an MCP image and a screen shot.
- MCP export works for the sheet type in use (worksheet / dashboard / storyboard).
- Page opens in a browser, scroll swaps the visual, click seeks the video, narration appears in amber.
