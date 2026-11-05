# Documenting Tableau Studio demos

## Key finding
Tableau Studio's agent (`TableauAgent`) is Claude-based and writes a **timestamped session
transcript** for every conversation:

    ~/.claude/projects/-Users-<you>-Library-Application-Support-Tableau-Tableau-WorkspaceExtensionApplications-TableauAgent-versions-<ver>/<session-id>.jsonl

It contains every prompt (with a `<live-tableau-context>` header: workbook + active sheet), every
reply, and every MCP tool call/result (`apply-worksheet`, `add-field`, `apply-dashboard`...).
It does **not** contain images. So: transcript = *what happened and when*; video/snapshots = *what it looked like*.
Joining the two by timestamp is the whole trick.

## As a Claude skill
Open Claude Code in this folder and say "start the demo watchdog" (or `/studio-demo-doc`). Do the demo in Studio, then say "stop and build":
you get `packages/<name>/index.html` (+ zip) from the agent chat and all captured screenshots. See `.claude/skills/studio-demo-doc/SKILL.md`.

## Example
A real run (Wildlife Strikes, 9 prompts, 10 captures): open `examples/wildlife-strikes-demo/index.html` in a browser
(local user paths in the transcript are replaced by `/Users/USER`). Try the Both / Sheet / Studio window toggle in the header.

## Cleaning up after the build
Open the page, click **Edit** (top right): every prompt, step, chatter block, answer, screenshot and narration line gets a checkbox.
Tick what to remove, **Delete selected** (Undo is available), then **Save cleaned HTML** (downloads `index.cleaned.html`; the header's prompt count is updated). Apply it to the package:

    python3 studio_prune.py packages/<name> --html ~/Downloads/index.cleaned.html --zip

This replaces `index.html` (the old one is kept as `index.original.html`, never zipped), deletes screenshots nothing references any more and rewrites the zip.

## When frames are taken
`task-done` (agent ends a turn, after Tableau is IDLE), `progress` (every `--progress` s, default 6, while the agent works on a prompt;
MCP export only if Tableau is idle at that moment, otherwise the last known sheet image is reused), `sheet-change` (tab switch or
new/renamed/removed view, polled every `--watch` s; views created alongside but not active are exported too), `user-change`
(window changed while the agent is idle, stable for one tick; `--no-pixel-watch` turns it off). Identical images are stored once.
The HTML has a **Both / Sheet / Studio window** toggle in the header (key `t` cycles; remembered).

## Pipeline (manual)
1. **Record** the screen (OBS is installed) plus mic. Note the wall-clock start (UTC). Tip: say
   "start" while clapping the first prompt in; or use the file's creation time.
2. **Transcribe** narration: `whisper rec.mp4 --output_format json` (whisper is installed).
3. **Build**:
   `python3 studio_replay.py --session <id> --video rec.mp4 --video-start 2026-11-04T15:18:00Z --whisper rec.json --out my_demo`
4. Open `my_demo/index.html`: chat on the left, scrolling swaps the visual on the right; click a
   screenshot to seek the video; spoken narration appears inline (amber).

Frames are cut `SETTLE_SECONDS` (2s) after each workbook-changing tool call finishes
(`apply-*`, `add-field`, `bind-template`, ...), so you get one picture per actual change, not per second.

`python3 studio_replay.py --list` shows recent sessions.

## Watchdog (recommended): capture on "task done", no LLM involved
`python3 snapshot_watchdog.py --out snaps/` before the demo, Ctrl-C afterwards. A plain script (a skill would burn
tokens polling) that checks the session log once a second (a cheap file read). When the agent ends a turn
(`stop_reason: end_turn`) it waits until `get-desktop-state` says IDLE (+1.5s), then captures:
- **MCP export** of the *active* sheet, using the right tool for its type (worksheet / dashboard / storyboard,
  from `get-workbook-inventory`) - so single vizzes work, not just dashboards
- **Screen shot** of the Tableau window (the agent pane, chat, selections), via `screencapture`
Both go to `snaps/manifest.jsonl`; `--on-change` additionally captures after each workbook-changing tool call.
It uses its own MCP connection, read-only, only while Studio is idle.
Replay: `studio_replay.py --session <id> --snapshots snaps/ [--video ... --whisper ...]`.

## Finding the log on other machines (`studio_logs.py`)
Order: `--log-dir` / `$STUDIO_LOG_DIR` -> the running `TableauAgent` process (cwd -> Claude project folder name)
-> project folders named like TableauAgent/tab-agent -> any recent `.jsonl` containing `<live-tableau-context>`.
Honors `$CLAUDE_CONFIG_DIR`. The script prints how it found the log. Only the first two strategies were tested here.

## In the HTML
Per prompt: a "Analyzed" dropdown (every tool call with input/result, workbook-changing ones green) and a
"Data & fields used" dropdown (field searches, summary data, queries). **Thinking is not available:** the agent log
stores only encrypted/empty thinking blocks, so the tool-call trail is the visible reasoning.

## Status / caveats
- Tested: log discovery, MCP export of the active dashboard through the watchdog's own connection, replay build with
  fake snapshots (`sample/`), JS syntax. **Not tested:** a full live task-done trigger, a real video, opening the page in a browser.
- **Screenshot permission:** run `python3 snapshot_watchdog.py --out snaps --check` first. It reports whether Screen Recording is granted
  to the app you launched it from (Terminal, iTerm, VS Code, Claude...), finds the Tableau window and takes a test shot. If not granted,
  `--grant` triggers the macOS prompt and opens the Settings pane; enable the app, **restart that app**, re-run `--check`.
  The Studio window is captured by window id (`macperms.py`, stdlib only), so it works even when other windows cover it and needs no
  Accessibility permission. If Studio isn't found it falls back to the whole screen and says so.
- Next: crop helper, clock-sync helper, per-turn workbook XML diff, zip/share packaging.

The dashboard-agent (Tableau Cloud) exporter lives in `/Users/dschober/Claude/TableauAgentDocumenter`.

## MCP configuration
`.mcp.json` registers the `tableau-desktop` server via `run-tableau-desktop-mcp.sh` (picks the newest bundled `tableau-mcp-desktop` under `~/Library/Application Support/Tableau/Tableau/WorkspaceExtensionApplications/TableauAgent/versions`, stdio, `TABLEAU_EXTERNAL_API=true`). The watchdog launches the same script itself over stdio, so it needs no extra setup; the `.mcp.json` is only for using the tools from Claude Code in this folder.
