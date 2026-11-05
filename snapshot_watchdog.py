#!/usr/bin/env python3
"""snapshot_watchdog.py - zero-LLM watchdog for a Tableau Studio demo.

Tails the newest Studio session log and captures when:
  * task-done     the agent finishes a task (stop_reason == end_turn) - waits until Tableau is IDLE first
  * progress      every --progress seconds while the agent works on a prompt (frames between the steps)
  * sheet-change  a tab switch, new, renamed or removed view (agent or user, polled every --watch seconds);
                  new views that are not active are exported too ("extras")
  * user-change   the window changed while the agent is idle (user clicked a filter, selection, ...); --no-pixel-watch disables
  * baseline      once at start
Each capture holds, side by side:
  * MCP image   - export of the ACTIVE sheet via the tableau-desktop MCP (worksheet, dashboard or storyboard,
                  chosen from get-workbook-inventory) - crisp, full resolution, no UI chrome
  * Screen shot - the real Tableau window (agent pane, chat, selections) via macOS screencapture
Everything is appended to <out>/manifest.jsonl for studio_replay.py (--snapshots <out>).

It runs its own MCP connection and only calls read-only tools while Studio is idle, so it does not
compete with the agent's MCP calls. Needs: pip mcp; Screen Recording permission for your terminal.

  snapshot_watchdog.py --out snaps/            # run before the demo, Ctrl-C afterwards
  snapshot_watchdog.py --out snaps/ --on-change  # also capture after each workbook-changing tool call
"""
import argparse, asyncio, base64, hashlib, json, re, subprocess, time
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


def md5(path):
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def is_prompt(rec):
    """A real user prompt (not a tool_result echo) in a session record."""
    if rec.get("type") != "user":
        return False
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return True
    return isinstance(c, list) and any(b.get("type") == "text" for b in c) and not any(b.get("type") == "tool_result" for b in c)


class Watchdog:
    def __init__(self, a):
        self.a, self.out = a, Path(a.out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.session, self.offset = None, 0
        self.busy = False                 # agent is working on a prompt (prompt seen, no end_turn yet)
        self.sig = None                   # (active sheet id, ((id, name, type), ...)) from get-workbook-inventory
        self.last_screen_md5 = None       # last window shot that went into the manifest
        self.prev_tick_md5 = None
        self.last_mcp = {}                # sheet id -> (md5, file name) so identical exports are stored once
        self.last_progress = 0.0

    async def wait_idle(self, mcp, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            if await self.is_idle(mcp):
                return True
            await asyncio.sleep(1)
        return False

    async def is_idle(self, mcp):
        try:
            return json.loads(text_of(await mcp.call_tool("get-desktop-state", {})) or "{}").get("state") == "IDLE"
        except Exception:
            return False

    async def inventory(self, mcp):
        """-> (sheets, active, title) or None if Desktop is not answering."""
        try:
            inv = json.loads(text_of(await mcp.call_tool("get-workbook-inventory", {})))
        except Exception:
            return None
        sheets = inv.get("worksheets", []) + inv.get("dashboards", []) + inv.get("storyboards", [])
        return sheets, next((x for x in sheets if x.get("isActiveSheet")), None), inv.get("title")

    async def export_sheet(self, mcp, sheet, dest):
        """Export one sheet via MCP (to file, then inline). Returns (file name or None, error or None)."""
        tool, key = EXPORT_TOOL.get(sheet.get("type", "WORKSHEET"), EXPORT_TOOL["WORKSHEET"])
        err = None
        for args in ({key: sheet["name"], "filePath": str(dest)}, {key: sheet["name"]}):
            try:
                res = await mcp.call_tool(tool, args)
            except Exception as e:
                err = repr(e)[:200]
                continue
            if res.isError:
                err = text_of(res)[:200] or "tool error"
                continue
            for c in res.content:  # inline mode returns the image as base64
                if getattr(c, "type", "") == "image":
                    dest.write_bytes(base64.b64decode(c.data))
            if dest.exists():
                h = md5(dest)
                old = self.last_mcp.get(sheet.get("id"))
                if old and old[0] == h:      # same picture as last time: reuse, don't store twice
                    dest.unlink()
                    return old[1], None
                self.last_mcp[sheet.get("id")] = (h, dest.name)
                return dest.name, None
        print(f"  ! MCP export failed for '{sheet['name']}': {err or 'no image'}")
        return None, err or "export produced no image"

    async def capture(self, mcp, trigger, detail="", wait=True, keep_duplicates=True):
        """wait=True: let Tableau go IDLE first. wait=False (mid-task): never block; MCP export only if Tableau is idle right now."""
        idle = (await self.wait_idle(mcp)) if wait else (await self.is_idle(mcp))
        if wait:
            await asyncio.sleep(self.a.settle)  # let rendering finish
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S")
        rec = {"ts": now.isoformat(), "trigger": trigger, "detail": detail[:200], "session": self.session and self.session.stem,
               "busy": self.busy}
        screen = self.out / f"{stamp}_screen.png"
        kind = screen_capture(screen)
        h = md5(screen) if kind else None
        if kind:
            if not keep_duplicates and h == self.last_screen_md5 and not detail:
                screen.unlink()
                return False
            rec["screen_img"], rec["screen_kind"] = screen.name, kind
            self.last_screen_md5 = h
            if kind == "screen":
                print("  ! Tableau window not found - captured the whole screen instead")
        else:
            print("  ! screenshot failed - run with --check")
        inv = await self.inventory(mcp)
        if inv:
            sheets, active, title = inv
            rec["workbook"] = title
            self.sig = self.signature(sheets)
            if active:
                rec.update(sheet=active["name"], sheet_type=active.get("type"))
                if idle:
                    rec["mcp_img"], err = await self.export_sheet(mcp, active, self.out / f"{stamp}_mcp.png")
                    if err:
                        rec["mcp_error"] = err
                else:
                    rec["mcp_img"] = self.last_mcp.get(active.get("id"), (None, None))[1]  # busy: show the last known picture
                    rec["mcp_stale"] = True
            if idle:  # views the agent created alongside (not the active one): export them too
                rec["extras"] = []
                for sh in sheets:
                    if sh.get("id") in self.known_ids or sh is active or sh.get("id") == (active or {}).get("id"):
                        continue
                    f, _ = await self.export_sheet(mcp, sh, self.out / f"{stamp}_view_{len(rec['extras'])}.png")
                    if f:
                        rec["extras"].append({"name": sh["name"], "type": sh.get("type"), "img": f})
                self.known_ids |= {sh.get("id") for sh in sheets}
        with (self.out / "manifest.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")
        extra = f" +{len(rec.get('extras', []))} new view(s)" if rec.get("extras") else ""
        print(f"[{now:%H:%M:%S}] {trigger}{' - ' + detail if detail else ''}: sheet={rec.get('sheet')} "
              f"mcp={rec.get('mcp_img')} screen={rec.get('screen_img')}{extra}")
        return True

    known_ids = set()

    @staticmethod
    def signature(sheets):
        active = next((x.get("id") for x in sheets if x.get("isActiveSheet")), None)
        return active, tuple(sorted((x.get("id"), x.get("name"), x.get("type")) for x in sheets))

    def describe_change(self, old, new, sheets):
        who = "agent" if self.busy else "user"
        names = {i: n for i, n, _ in new[1]}
        added = [n for i, n, _ in new[1] if i not in {o[0] for o in old[1]}]
        gone = [n for i, n, _ in old[1] if i not in names]
        renamed = [n for i, n, _ in new[1] if i in {o[0] for o in old[1]} and (i, n) not in {(o[0], o[1]) for o in old[1]}]
        if added:
            return f"{who}: new view {', '.join(added)[:80]}"
        if gone:
            return f"{who}: removed view {', '.join(gone)[:80]}"
        if renamed:
            return f"{who}: renamed {', '.join(renamed)[:80]}"
        return f"{who}: switched to {names.get(new[0], '?')}"

    async def tick(self, mcp):
        """Every --watch seconds: catch tab switches / new views (inventory), pixel changes by the user, and mid-task progress."""
        now = time.time()
        probe = self.out / ".probe.png"
        kind = screen_capture(probe) if not self.a.no_pixel_watch else None
        h = md5(probe) if kind else None
        probe.unlink(missing_ok=True)
        inv = await self.inventory(mcp)
        if inv and self.sig is not None and self.signature(inv[0]) != self.sig:
            new = self.signature(inv[0])
            await self.capture(mcp, "sheet-change", self.describe_change(self.sig, new, inv[0]), wait=not self.busy)
            self.last_progress = now
        elif self.busy and now - self.last_progress >= self.a.progress:
            self.last_progress = now
            await self.capture(mcp, "progress", wait=False, keep_duplicates=False)
        elif (not self.busy and h and h != self.last_screen_md5 and h == self.prev_tick_md5):  # changed, and stable for one tick
            await self.capture(mcp, "user-change", "user: changed the view", wait=False, keep_duplicates=False)
        self.prev_tick_md5 = h

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
                inv = await self.inventory(mcp)
                if inv:  # sheets that exist now are not "new" views
                    self.known_ids = {x.get("id") for x in inv[0]}
                await self.capture(mcp, "baseline")
                print(f"watching: task-done, sheet changes, user changes, progress every {self.a.progress:g}s while the agent works (Ctrl-C to stop)")
                tool_names = {}
                last_tick = 0.0
                while True:
                    trigger = None
                    for rec in self.new_records():
                        m = rec.get("message") or {}
                        c = m.get("content")
                        if is_prompt(rec):
                            self.busy, self.last_progress = True, time.time()
                        if rec.get("type") == "assistant" and isinstance(c, list):
                            for b in c:
                                if b.get("type") == "tool_use":
                                    tool_names[b["id"]] = b["name"]
                            if m.get("stop_reason") == "end_turn" and any(b.get("type") == "text" for b in c):
                                trigger = ("task-done", next(b["text"] for b in c if b.get("type") == "text"))
                                self.busy = False
                        elif rec.get("type") == "user" and isinstance(c, list) and self.a.on_change and not trigger:
                            for b in c:
                                n = tool_names.get(b.get("tool_use_id"), "")
                                if b.get("type") == "tool_result" and not b.get("is_error") and MUTATING.search(n):
                                    trigger = ("change", n.split("__")[-1])
                    if trigger:
                        await self.capture(mcp, *trigger)
                        last_tick = time.time()
                    elif time.time() - last_tick >= self.a.watch:
                        last_tick = time.time()
                        await self.tick(mcp)
                    await asyncio.sleep(self.a.poll)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--log-dir", help="folder with Studio session *.jsonl (default: auto-detect)")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between log checks (cheap file read)")
    ap.add_argument("--settle", type=float, default=1.5, help="extra wait after Tableau reports IDLE")
    ap.add_argument("--watch", type=float, default=3.0, help="seconds between checks for tab switches, new views and user changes")
    ap.add_argument("--progress", type=float, default=6.0, help="while the agent works: take a frame at least this often (seconds)")
    ap.add_argument("--no-pixel-watch", action="store_true", help="only detect tab/view changes, not other visible changes by the user")
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
