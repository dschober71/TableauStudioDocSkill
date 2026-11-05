# Tableau Studio Demo Documenter

Turn a Tableau Studio agent demo into **one shareable HTML page**: the full agent chat, every step it took, and screenshots
of what it looked like - no manual screenshots, no video editing.

**Example:** open [`examples/wildlife-strikes-demo/index.html`](examples/wildlife-strikes-demo/index.html) in a browser
(a real run: 9 prompts, 10 captures).

## What you get
- Chat on the left; scrolling swaps the screenshot on the right. Agent chatter between steps is muted, the final answer to each prompt is highlighted.
- Per prompt: an **Analyzed** dropdown (every tool call with input and result; workbook-changing ones in green) and a **Data & fields used** dropdown.
- Screenshots in two flavors: the **sheet** exported through the Tableau MCP (clean, full resolution) and the **Studio window** (agent pane, chat, selections).
  A header toggle switches **Both / Sheet / Studio window** (key `t` cycles; remembered).
- **Edit mode**: tick prompts, steps, chatter, screenshots; delete them (undo available); save a cleaned page.
- Optional: your screen recording and Whisper narration, synced to the chat.

## How it works
Studio's agent (`TableauAgent`) is Claude-based and writes a timestamped transcript of every session
(`~/.claude/projects/...TableauAgent-versions-<ver>/<session-id>.jsonl`): prompts, replies, every MCP tool call and result - but no images.
A small background **watchdog** takes the pictures; `studio_replay.py` joins both by timestamp. The watchdog is a plain script, no LLM,
so it costs nothing while it runs.

## Requirements
- macOS with Tableau Studio / Desktop (the build that bundles `TableauAgent` and `tableau-mcp-desktop`)
- Python 3.10+ and `pip install -r requirements.txt` (the `mcp` package)
- Screen Recording permission for the app you launch the watchdog from (Terminal, iTerm, VS Code, Claude...)
- For the skill: [Claude Code](https://claude.com/claude-code) opened in this folder
- Optional: OBS (video), `whisper` and `ffmpeg` (narration, frame cutting)

## Quick start (as a Claude skill)
In Claude Code, in this folder:

1. **"start the demo watchdog"** (or `/studio-demo-doc`). It checks permissions and the Tableau window, then watches in the background.
2. Run your demo in Studio.
3. **"stop and build"** -> `packages/<name>/index.html` and a `.zip`, opened in your browser.
4. Optional **clean-up**: in the page click **Edit**, tick what to remove, **Delete selected**, **Save cleaned HTML**; then say
   **"apply the cleaned HTML"** (the skill looks for `~/Downloads/index.cleaned.html`) to update the package and zip.

The skill is [`.claude/skills/studio-demo-doc/SKILL.md`](.claude/skills/studio-demo-doc/SKILL.md).

## Quick start (command line)
```bash
python3 snapshot_watchdog.py --out snaps --check     # permissions + Tableau window + test shot
python3 snapshot_watchdog.py --out snaps/demo1       # run during the demo, Ctrl-C afterwards
python3 studio_replay.py --session auto --snapshots snaps/demo1 --out packages/demo1 --title "My demo" --zip
open packages/demo1/index.html
```
`python3 studio_replay.py --list` shows recent sessions. With video: add `--video rec.mp4 --video-start 2026-11-05T15:18:00Z`
(UTC time the recording began) and `--whisper rec.json` (from `whisper rec.mp4 --output_format json`).

## When screenshots are taken
| Trigger | When |
|---|---|
| `baseline` | once, at start |
| `task-done` | the agent ends a turn and Tableau is IDLE |
| `progress` | every `--progress` s (default 6) while the agent works, so in-between steps show up |
| `sheet-change` | tab switch or new / renamed / removed view, by agent or user (checked every `--watch` s, default 3); new views that are not active are exported too |
| `user-change` | the window changed while the agent is idle (e.g. the user clicked a filter); `--no-pixel-watch` turns it off |
| `change` | after each workbook-changing tool call (only with `--on-change`) |

Mid-task, the MCP export runs only if Tableau is idle at that moment; otherwise the last known sheet image is reused. Identical images are stored once.
The watchdog uses its own read-only MCP connection (`run-tableau-desktop-mcp.sh`), so it needs no extra setup.

## Cleaning up after the build
Open the page, click **Edit**, tick parts, **Delete selected**, **Save cleaned HTML** (downloads `index.cleaned.html` to your browser's download folder). Then:
```bash
python3 studio_prune.py packages/<name> --html ~/Downloads/index.cleaned.html --zip
```
This replaces `index.html` (the old one is kept as `index.original.html`, never zipped), deletes screenshots nothing references any more and rewrites the zip.

## Files
| File | Purpose |
|---|---|
| `snapshot_watchdog.py` | background capture (window shot + MCP export) |
| `studio_replay.py` | builds the HTML package from session log + snapshots (+ video, narration) |
| `studio_prune.py` | applies a cleaned page to the package |
| `studio_logs.py` | finds the Studio session log (`--log-dir` / `$STUDIO_LOG_DIR`, running `TableauAgent` process, folder names, content sniffing; honors `$CLAUDE_CONFIG_DIR`) |
| `macperms.py` | Screen Recording check and window lookup (stdlib only) |
| `run-tableau-desktop-mcp.sh`, `.mcp.json` | launch / register the bundled `tableau-desktop` MCP server |
| `sample/`, `examples/` | fake-data test page and a real example run |

## Good to know
- **Privacy:** transcripts and screenshots can contain customer or workbook data. Review the page (use Edit to remove parts) before sharing it.
- **Thinking is not available:** the agent log stores only encrypted or empty thinking blocks; the tool-call trail is the visible reasoning.
- **Window not found:** if Studio is minimized or on another Space, shots fall back to the whole screen and the watchdog says so.
- **Several sessions:** `--session auto` uses the session seen most often in the snapshots and tells you if there were others.
- **Permission:** if `--check` says Screen Recording is not granted, run `--grant`, enable the app in System Settings, **restart that app**, re-check.

## Status
Tested live: log discovery, task-done and sheet captures with both screenshot types, build, toggle, markdown rendering, Edit mode in a browser,
pruning. Newer and only lightly tested: mid-task `progress` frames, `user-change` detection and extra-view export on long demos;
log discovery beyond the first two strategies.
Ideas: smarter frame de-duplication (keep a mid-task frame only if the sheet export or view list changed, or the window changed beyond a small tolerance with the chat pane masked; collapse runs of identical frames in the replay), crop helper, clock-sync helper, per-turn workbook XML diff, direct-save from the page.
