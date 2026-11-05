#!/usr/bin/env python3
"""snapshot_watchdog.py - zero-LLM watchdog for a Tableau Studio demo.

Tails the newest Studio session log. When the agent finishes a task (assistant stop_reason == end_turn)
it waits until Tableau is IDLE, then captures, side by side:
  * MCP image   - export of the ACTIVE sheet via the tableau-desktop MCP (worksheet, dashboard or storyboard,
                  chosen from get-workbook-inventory) - crisp, full resolution, no UI chrome
  * Screen shot - the real Tableau window (agent pane, chat, selections) via macOS screencapture
Everything is appended to <out>/manifest.jsonl for studio_replay.py (--snapshots <out>).

It runs its own MCP connection and only calls read-only tools while Studio is idle, so it does not
compete with the agent's MCP calls. Needs: pip mcp; Screen Recording permission for your terminal.

  snapshot_watchdog.py --out snaps/            # run before the demo, Ctrl-C afterwards
  snapshot_watchdog.py --out snaps/ --on-change  # also capture after each workbook-changing tool call
"""
import argparse, asyncio, json, re, subprocess, time
from datetime import datetime, timezone
from pathlib import Path

import studio_logs, macperms
_here = Path(__file__).resolve().parent
LAUNCHER = _here / "run-tableau-desktop-mcp.sh" if (_here / "run-tableau-desktop-mcp.sh").exists() else _here.parent / "run-tableau-desktop-mcp.sh"
MUTATING = re.compile(r"__(apply-|add-field|remove-field|add-worksheet|add-dashboard|bind-template|"
                      r"build-worksheets|compose-story|delete-sheet|format-|refine-|set-member-colors|sort-worksheet)")
EXPORT_TOOL = {"WORKSHEET": ("export-worksheet-image", "worksheetName"),
               "DASHBOARD": ("export-dashboard-image", "dashboardName"),
               "STORYBOARD": ("export-storyboard-image", "storyboardName")}


def newest_session(log_dir=None):
    return studio_logs.newest_session(log_dir)[0]


def text_of(res):
    return "".join(getattr(c, "text", "") for c in res.content)


def screen_capture(dest):
    """Capture the Tableau window (even if covered by other windows); fall back to the whole screen."""
    w = macperms.find_window("Tableau")
    if w and macperms.capture_window(w["id"], dest):
        return "window"
    return "screen" if subprocess.run(["screencapture", "-x", str(dest)], capture_output=True).returncode == 0 and dest.exists() else None


def preflight(fix=False):
    """Print permission status; returns True if screenshots will work."""
    ok = macperms.screen_recording_granted()
    app = macperms.responsible_app() or "your terminal app"
    w = macperms.find_window("Tableau")
    print(f"Screen Recording for {app}: {'granted' if ok else 'NOT granted' if ok is False else 'unknown'}")
    print(f"Tableau window: {w['title'].splitlines()[0][:70] if w else 'not found (is Studio open and not minimized?)'}")
    if ok is False:
        print(f"  -> System Settings > Privacy & Security > Screen & System Audio Recording > enable {app}, then restart {app}.")
        if fix:
            macperms.request_screen_recording()
            macperms.open_settings()
    return bool(ok) and bool(w)


class Watchdog:
    def __init__(self, a):
        self.a, self.out = a, Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.session, self.offset = None, 0

    async def wait_idle(self, mcp, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            st = json.loads(text_of(await mcp.call_tool("get-desktop-state", {})) or "{}")
            if st.get("state") == "IDLE":
                return True
            await asyncio.sleep(1)
        return False

    async def capture(self, mcp, trigger, detail=""):
        await self.wait_idle(mcp)
        await asyncio.sleep(self.a.settle)  # let rendering finish
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        rec = {"ts": now.isoformat(), "trigger": trigger, "detail": detail[:200], "session": self.session and self.session.stem}
        screen = self.out / f"{stamp}_screen.png"
        kind = screen_capture(screen)
        if kind:
            rec["screen_img"], rec["screen_kind"] = screen.name, kind
            if kind == "screen":
                print("  ! Tableau window not found - captured the whole screen instead")
        else:
            print("  ! screenshot failed - run with --check")
        try:
            inv = json.loads(text_of(await mcp.call_tool("get-workbook-inventory", {})))
            sheets = inv.get("worksheets", []) + inv.get("dashboards", []) + inv.get("storyboards", [])
            active = next((s for s in sheets if s.get("isActiveSheet")), None)
            rec["workbook"] = inv.get("title")
            if active:
                tool, key = EXPORT_TOOL.get(active.get("type", "WORKSHEET"), EXPORT_TOOL["WORKSHEET"])
                img = self.out / f"{stamp}_mcp.png"
                await mcp.call_tool(tool, {key: active["name"], "filePath": str(img)})
                rec.update(sheet=active["name"], sheet_type=active.get("type"), mcp_img=img.name if img.exists() else None)
        except Exception as e:  # never let a capture error kill the watchdog
            rec["error"] = repr(e)[:200]
        with (self.out / "manifest.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[{now:%H:%M:%S}] {trigger}: sheet={rec.get('sheet')} mcp={rec.get('mcp_img')} screen={rec.get('screen_img')}")

    def new_records(self):
        s = newest_session(self.a.log_dir)
        if s is None:
            return []
        if s != self.session:  # new conversation started -> read from its start
            self.session, self.offset = s, 0 if self.session else s.stat().st_size
            print(f"watching {s.name}")
        with s.open("rb") as f:
            f.seek(self.offset)
            data = f.read()
        if not data.endswith(b"\n") and b"\n" in data:  # keep a partial last line for next poll
            data = data[: data.rfind(b"\n") + 1]
        elif b"\n" not in data:
            return []
        self.offset += len(data)
        out = []
        for line in data.splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    async def run(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        async with stdio_client(StdioServerParameters(command=str(LAUNCHER))) as (r, w):
            async with ClientSession(r, w) as mcp:
                await mcp.initialize()
                f, how = studio_logs.newest_session(self.a.log_dir)
                print(f"session log: {f} (found via {how})")
                self.new_records()  # skip history, start at end of current log
                await self.capture(mcp, "baseline")
                print("watching for finished tasks (Ctrl-C to stop)")
                tool_names = {}
                while True:
                    trigger = None
                    for rec in self.new_records():
                        m = rec.get("message") or {}
                        c = m.get("content")
                        if rec.get("type") == "assistant" and isinstance(c, list):
                            for b in c:
                                if b.get("type") == "tool_use":
                                    tool_names[b["id"]] = b["name"]
                            if m.get("stop_reason") == "end_turn" and any(b.get("type") == "text" for b in c):
                                trigger = ("task-done", next(b["text"] for b in c if b.get("type") == "text"))
                        elif rec.get("type") == "user" and isinstance(c, list) and self.a.on_change and not trigger:
                            for b in c:
                                n = tool_names.get(b.get("tool_use_id"), "")
                                if b.get("type") == "tool_result" and not b.get("is_error") and MUTATING.search(n):
                                    trigger = ("change", n.split("__")[-1])
                    if trigger:
                        await self.capture(mcp, *trigger)
                    await asyncio.sleep(self.a.poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--log-dir", help="folder with Studio session *.jsonl (default: auto-detect)")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between log checks (cheap file read)")
    ap.add_argument("--settle", type=float, default=1.5, help="extra wait after Tableau reports IDLE")
    ap.add_argument("--on-change", action="store_true", help="also capture after workbook-changing tool calls")
    ap.add_argument("--check", action="store_true", help="check permissions + Tableau window, take a test shot, exit")
    ap.add_argument("--grant", action="store_true", help="like --check, but trigger the macOS permission prompt and open Settings")
    a = ap.parse_args()
    if a.check or a.grant:
        ok = preflight(fix=a.grant)
        t = Path(a.out); t.mkdir(parents=True, exist_ok=True)
        print("test shot:", screen_capture(t / "check.png"), "->", t / "check.png")
        raise SystemExit(0 if ok else 1)
    if macperms.screen_recording_granted() is False:
        preflight()
    try:
        asyncio.run(Watchdog(a).run())
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
