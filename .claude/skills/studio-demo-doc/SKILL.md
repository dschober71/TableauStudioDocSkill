---
name: studio-demo-doc
description: Document a Tableau Studio demo. Starts a background watchdog that captures screenshots (MCP export + window shot) each time the Studio agent finishes a task, then after the demo builds one shareable HTML package (agent chat + tool trail + screenshots, optional video/narration). Use when the user says "start the demo watchdog", "document this Studio demo", "stop and build the package", or "/studio-demo-doc".
---

# Studio demo documenter

Project root: the folder containing `snapshot_watchdog.py` and `studio_replay.py` (run everything from there).
The watchdog is plain Python with no LLM in the loop - never poll it, never narrate its output.

Pick the mode from the user's words: **start**, **status**, or **stop/build**. If unclear, ask which.

## start
1. Preflight, once: `python3 snapshot_watchdog.py --out snaps --check`
   - Screen Recording not granted -> tell the user to run `python3 snapshot_watchdog.py --out snaps --grant`, enable the app, restart it. Stop here.
   - "Tableau window: not found" -> ask the user to bring Studio to the front (not minimized, current Space), then re-check. Whole-screen shots are the fallback, so continue only if the user says so.
   - Remove `snaps/check.png` afterwards.
2. Pick the capture folder: `snaps/<YYYYMMDD-HHMM>-<short-topic>` (ask nothing; derive topic from the user's words or use `demo`).
3. Start in the background (Bash with `run_in_background: true`):
   `python3 -u snapshot_watchdog.py --out <folder>` (optional: `--progress <sec>` frame interval while the agent works, default 6; `--watch <sec>` tab/view-change check, default 3; `--no-pixel-watch` to ignore non-tab user changes; `--on-change` for a frame after every workbook-changing tool call).
4. Tell the user in 2 lines: it is watching, run the demo in Studio, say "stop and build" when done. Remember the folder and the background task id.
   Optional: if they will record video, offer `date -u +%Y-%m-%dT%H:%M:%SZ` as the clock reference at recording start.

## status
Read the background task output once (not repeatedly) and report the capture lines, or `ls <folder>` if the task is gone.

## stop / build
1. Stop the background watchdog (TaskStop on its task id; if lost, `pkill -f snapshot_watchdog.py`).
2. Check `<folder>/manifest.jsonl` exists and count lines. If the user did not name a folder, use the newest folder under `snaps/`. Zero captures -> say so, do not build.
3. Build (the title defaults to the session id, so pass a real one, e.g. the workbook name from the manifest or the user's topic):
   `python3 studio_replay.py --session auto --snapshots <folder> --out packages/<name> --title "<title>" --zip`
   Add `--video rec.mp4 --video-start <UTC> [--whisper rec.json]` only if the user provides a recording
   (run `whisper rec.mp4 --output_format json` first if they want narration).
   If the output notes that snapshots span several sessions, tell the user which session was used.
4. `open packages/<name>/index.html`, then report: turns, snapshot count, zip path. Mention any capture lines that fell back to whole-screen shots or had `error`.

## Notes
- Captures: baseline, `progress` (every few seconds while the agent works), `sheet-change` (tab switch / new view, by agent or user; new non-active views are exported as extras), `user-change` (visible change while the agent is idle), `task-done`.
- The HTML has a Both / Sheet / Studio window toggle (also key `t`); the choice is remembered.
- Package = `packages/<name>/index.html` + `assets/` (+ `.zip`). Chat left, screenshots right; "Analyzed" and "Data & fields used" dropdowns hold the tool-call trail. Thinking is not recorded by the agent.
- The baseline shot (before the first prompt) is attached to the first turn.
- Sessions: `python3 studio_replay.py --list`. To rebuild from another session pass `--session <id>`.
- Never commit `snaps/` or `packages/` (gitignored). Details: README.md.
