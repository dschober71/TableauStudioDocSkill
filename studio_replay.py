#!/usr/bin/env python3
"""studio_replay.py - turn a Tableau Studio (TableauAgent) session into a scrollable HTML replay.

Sources combined:
  1. Session transcript  ~/.claude/projects/<...TableauAgent-versions-X...>/<session>.jsonl
     (timestamped prompts, replies, MCP tool calls = the authoritative "what happened")
  2. Screen recording (optional) -> frames cut at the moment each change landed
  3. Whisper transcript (optional) -> spoken narration shown on the same timeline
  4. Live snapshots folder (optional) -> PNGs from snapshot_daemon.py, used instead of video frames

Usage:
  studio_replay.py --list
  studio_replay.py --session <id|path|auto> --out demo_pkg [--video rec.mp4 --video-start 2026-11-04T15:18:00Z]
                   [--whisper rec.json] [--snapshots snaps/] [--title "..."]
"""
import argparse, bisect, html, json, re, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

import studio_logs
LOG_DIR = None  # set from --log-dir
# Tools whose success means the workbook visibly changed -> take a snapshot after them.
MUTATING = re.compile(r"__(apply-|add-field|remove-field|add-worksheet|add-dashboard|add-storyboard|"
                      r"bind-template|build-worksheets|compose-story|delete-sheet|format-|refine-|"
                      r"set-member-colors|sort-worksheet|undo|redo|author-|show-me|write-cached)")
SETTLE_SECONDS = 2.0   # let Tableau finish rendering before grabbing the frame


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def find_sessions():
    return studio_logs.find_sessions(LOG_DIR)[0]


def resolve_session(arg):
    p = Path(arg)
    if p.exists():
        return p
    for f in find_sessions():
        if f.stem.startswith(arg):
            return f
    sys.exit(f"session not found: {arg}")


def strip_context(text):
    """Pull the <live-tableau-context> header out of the prompt; return (ctx dict, clean prompt)."""
    ctx = {}
    m = re.search(r"<live-tableau-context>(.*?)</live-tableau-context>", text, re.S)
    if m:
        for line in m.group(1).strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                ctx[k.strip()] = v.strip()
        text = text.replace(m.group(0), "")
    return ctx, text.strip()


def short_result(content, limit=4000):
    if isinstance(content, list):
        content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    content = str(content or "")
    return content if len(content) <= limit else content[:limit] + f"... [{len(content)} chars]"


def parse_session(path):
    """-> list of turns: {t0, t1, ctx, prompt, events:[{kind,ts,...}], snap_ts:[...]}"""
    turns, cur, calls = [], None, {}
    for line in path.open():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ, t = r.get("type"), ts(r.get("timestamp"))
        msg = r.get("message") or {}
        content = msg.get("content")
        if typ == "user" and isinstance(content, str) and "<live-tableau-context>" in content or \
           typ == "user" and isinstance(content, str) and not content.startswith("<"):
            ctx, prompt = strip_context(content)
            cur = {"t0": t, "t1": t, "ctx": ctx, "prompt": prompt, "events": [], "snap_ts": []}
            turns.append(cur)
        elif cur is None or t is None:
            continue
        elif typ == "assistant" and isinstance(content, list):
            for b in content:
                if b.get("type") == "text" and b.get("text", "").strip():
                    cur["events"].append({"kind": "say", "ts": t, "text": b["text"]})
                elif b.get("type") == "tool_use":
                    ev = {"kind": "tool", "ts": t, "name": b["name"], "input": b.get("input", {}), "result": None}
                    calls[b["id"]] = ev
                    cur["events"].append(ev)
        elif typ == "user" and isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                    ev = calls[b["tool_use_id"]]
                    ev["result"] = short_result(b.get("content"), 40000 if ev["name"] == "Read" else 4000)
                    ev["error"] = bool(b.get("is_error"))
                    ev["done"] = t
                    if MUTATING.search(ev["name"]) and not ev["error"]:
                        cur["snap_ts"].append(t)
        if t and cur:
            cur["t1"] = max(cur["t1"], t)
    return turns


def load_whisper(path):
    d = json.loads(Path(path).read_text())
    return [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in d.get("segments", [])]


def grab_frame(video, sec, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(sec, 0):.2f}", "-i", str(video),
                    "-frames:v", "1", "-vf", "scale='min(1600,iw)':-2", "-q:v", "3", str(dest)], check=True)


def nearest_snapshot(snaps, when):
    """snaps: sorted list of (datetime, Path); return the first one at/after `when`, else the last."""
    if not snaps:
        return None
    i = bisect.bisect_left([s[0] for s in snaps], when)
    return snaps[min(i, len(snaps) - 1)][1]


DATA_TOOLS = re.compile(r"__(get-summary-data|get-worksheet-underlying-data|query-datasource|get-view-data|"
                        r"get-datasource-metadata|list-available-fields|search-workbook-fields|resolve-field|"
                        r"get-datasource-info|list-workbook-datasources|get-custom-view-data)")


def load_manifest(folder):
    """snapshot_watchdog output -> list of {ts, mcp, screen, sheet, trigger} sorted by time."""
    out = []
    mf = Path(folder) / "manifest.jsonl"
    if mf.exists():
        for line in mf.open():
            r = json.loads(line)
            out.append({"ts": ts(r["ts"]), "mcp": r.get("mcp_img"), "screen": r.get("screen_img"),
                        "sheet": r.get("sheet"), "trigger": r.get("trigger"), "dir": Path(folder)})
    return sorted(out, key=lambda r: r["ts"])


def session_from_manifest(folder):
    """--session auto: the Studio session the watchdog saw most often (falls back to the newest session)."""
    mf = Path(folder) / "manifest.jsonl"
    ids = [json.loads(l).get("session") for l in mf.open()] if mf.exists() else []
    ids = [i for i in ids if i]
    if not ids:
        print("manifest names no session - using the newest Studio session")
        return find_sessions()[0]
    best = max(set(ids), key=ids.count)
    if len(set(ids)) > 1:
        print(f"note: snapshots span {len(set(ids))} sessions {sorted(set(i[:8] for i in ids))}; using {best[:8]}")
    return resolve_session(best)


def build(args):
    if args.session == "auto":
        if not args.snapshots:
            sys.exit("--session auto needs --snapshots")
        session = session_from_manifest(args.snapshots)
    else:
        session = resolve_session(args.session)
    turns = parse_session(session)
    out = Path(args.out)
    (out / "assets").mkdir(parents=True, exist_ok=True)
    video_start = ts(args.video_start) if args.video_start else None
    video = Path(args.video) if args.video else None
    whisper = load_whisper(args.whisper) if args.whisper else []
    manifest = load_manifest(args.snapshots) if args.snapshots else []
    if video and not video_start:
        sys.exit("--video needs --video-start (UTC ISO time at which the recording began)")

    n = 0
    for ti, turn in enumerate(turns):
        turn["shots"] = []
        if manifest:  # watchdog snapshots: everything captured between this prompt and the next one
            end = turns[ti + 1]["t0"] if ti + 1 < len(turns) else datetime.max.replace(tzinfo=timezone.utc)
            start = datetime.min.replace(tzinfo=timezone.utc) if ti == 0 else turn["t0"]  # first turn also gets the "before" baseline
            for m in manifest:
                if start <= m["ts"] < end:
                    shot = {"ts": m["ts"], "trigger": m["trigger"], "sheet": m["sheet"], "mcp": None, "screen": None,
                            "video_sec": (m["ts"].timestamp() - video_start.timestamp()) if video_start else None}
                    for key in ("mcp", "screen"):
                        if m[key] and (m["dir"] / m[key]).exists():
                            shutil.copy(m["dir"] / m[key], out / "assets" / m[key])
                            shot[key] = m[key]
                    turn["shots"].append(shot)
                    n += 1
        elif video:  # fallback: cut frames from the recording after each workbook-changing tool call
            pruned = []
            for m in sorted(set(turn["snap_ts"])) or [turn["t1"]]:
                if not pruned or (m - pruned[-1]).total_seconds() > 3:
                    pruned.append(m)
            for m in pruned:
                n += 1
                name = f"shot_{n:03d}.jpg"
                grab_frame(video, m.timestamp() + SETTLE_SECONDS - video_start.timestamp(), out / "assets" / name)
                turn["shots"].append({"ts": m, "trigger": "change", "sheet": None, "mcp": None, "screen": name,
                                      "video_sec": m.timestamp() - video_start.timestamp()})
    if video:
        shutil.copy(video, out / "assets" / video.name)
    page = render(turns, args.title or session.stem, video.name if video else None, whisper, video_start)
    (out / "index.html").write_text(page)
    print(f"{len(turns)} turns, {n} snapshots -> {out/'index.html'}")
    if args.zip:
        z = shutil.make_archive(str(out), "zip", out.parent, out.name)
        print(f"zip -> {z}")


# ----------------------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2430;--mut:#6b7585;--acc:#0b6fd6;--line:#e3e7ee}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
header{padding:14px 24px;background:#10233d;color:#fff;display:flex;gap:16px;align-items:baseline}
header h1{font-size:17px;margin:0}header span{opacity:.7;font-size:12px}
main{display:grid;grid-template-columns:minmax(380px,40%) 1fr;height:calc(100vh - 50px)}
#chat{overflow-y:auto;padding:18px 20px;scroll-behavior:smooth}
#stage{padding:14px;display:flex;flex-direction:column;gap:8px;border-left:1px solid var(--line);background:#e9edf3;overflow:auto}
#imgs{display:flex;gap:10px;flex:1;min-height:0;align-items:flex-start;flex-wrap:wrap;justify-content:center}
#imgs figure{margin:0;flex:1 1 320px;text-align:center}#imgs figcaption{font-size:11px;color:var(--mut);margin-top:3px}
#imgs img{max-width:100%;max-height:calc(100vh - 170px);object-fit:contain;border-radius:8px;box-shadow:0 2px 14px #0003;background:#fff}
#stage video{max-width:100%;max-height:30vh;align-self:center}
#cap{color:var(--mut);font-size:12px;text-align:center}
.turn{margin-bottom:26px}.msg{padding:9px 13px;border-radius:12px;margin:6px 0;max-width:94%;white-space:pre-wrap;word-wrap:break-word}
.user{background:var(--acc);color:#fff;margin-left:auto;border-bottom-right-radius:3px}
.say{background:var(--card);border:1px solid var(--line);border-bottom-left-radius:3px}
.meta,.ctx{font-size:11px;color:var(--mut);margin:0 4px 4px}
details.group{margin:6px 0;border:1px solid var(--line);border-radius:10px;background:#fff}
details.group>summary{cursor:pointer;padding:6px 12px;font-size:12px;color:var(--mut);font-weight:600}
details.group[open]>summary{border-bottom:1px solid var(--line)}
.tool{font:12px ui-monospace,Menlo,monospace;background:#eef2f8;border:1px solid var(--line);border-radius:8px;margin:5px 8px;padding:0}
.tool summary{cursor:pointer;padding:4px 10px}.tool.mut summary{background:#e4f4e8}.tool.err summary{background:#fde8e8}
.tool summary b{font-weight:600;color:#33415c}.tool pre{margin:0;padding:8px 10px;overflow:auto;max-height:340px;border-top:1px solid var(--line);white-space:pre-wrap}
.shot{margin:8px 0;border:2px solid transparent;border-radius:8px;cursor:pointer;background:#fff;overflow:hidden;max-width:94%;box-shadow:0 1px 4px #0002}
.shot .pair{display:flex;gap:2px}.shot img{width:100%;min-width:0;display:block;flex:1}
.shot .cap{font-size:11px;color:var(--mut);padding:3px 8px}.shot.on,.shot:hover{border-color:var(--acc)}
.voice{border-left:3px solid #f0a800;padding:3px 10px;color:#5a4300;background:#fff8e1;font-style:italic;margin:4px 0;font-size:13px}
"""
JS = """
const shots=[...document.querySelectorAll('.shot')],box=document.getElementById('imgs'),cap=document.getElementById('cap'),vid=document.getElementById('vid');
function show(el){shots.forEach(i=>i.classList.toggle('on',i===el));box.innerHTML='';
  [['mcp','Viz export (MCP)'],['screen','Studio screen']].forEach(([k,l])=>{if(el.dataset[k]){const f=document.createElement('figure');
    f.innerHTML='<img src="assets/'+el.dataset[k]+'"><figcaption>'+l+'</figcaption>';box.appendChild(f)}});cap.textContent=el.dataset.cap}
shots.forEach(i=>i.addEventListener('click',()=>{show(i);if(vid&&i.dataset.t)vid.currentTime=+i.dataset.t}));
const io=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting)show(e.target)})},{root:document.getElementById('chat'),rootMargin:'-35% 0px -55% 0px'});
shots.forEach(i=>io.observe(i));if(shots[0])show(shots[0]);
document.querySelectorAll('.voice[data-t]').forEach(v=>v.addEventListener('click',()=>{if(vid)vid.currentTime=+v.dataset.t}));
"""


def tool_label(ev):
    """Human label like Studio's own 'Analyzed' list: 'Reading file', 'Searching fields', ..."""
    n, i = ev["name"], ev["input"]
    short = n.replace("mcp__tableau-desktop__", "").replace("mcp__", "")
    if n == "Read":
        return "Reading file", Path(i.get("file_path", "")).name
    if n in ("Write", "Edit"):
        return ("Writing file" if n == "Write" else "Editing file"), Path(i.get("file_path", "")).name
    if n == "Skill":
        return "Loading skill", i.get("skill", "")
    if n == "Bash":
        return "Running command", (i.get("command", "")[:60])
    if n == "ToolSearch":
        return "Looking up tools", i.get("query", "")[:60]
    return short.replace("-", " ").capitalize(), ""


def fmt_tool(ev):
    label, sub = tool_label(ev)
    if ev["name"] == "Read":  # show the file the way Studio does: the content itself
        body = f"{ev['input'].get('file_path', '')}\n\n{ev.get('result') or ''}"
    else:
        arg = json.dumps(ev["input"], ensure_ascii=False)
        body = f"INPUT\n{arg[:2000]}" + (f"\n\nRESULT\n{ev['result']}" if ev.get("result") else "")
    cls = "tool" + (" mut" if MUTATING.search(ev["name"]) else "") + (" err" if ev.get("error") else "")
    return (f'<details class="{cls}"><summary>{html.escape(label)} <b>{html.escape(sub)}</b> '
            f'<small>{ev["ts"]:%H:%M:%S}</small></summary><pre>{html.escape(body)}</pre></details>')


def shot_html(s):
    if not (s["mcp"] or s["screen"]):
        return ""
    t = f' data-t="{s["video_sec"]:.1f}"' if s["video_sec"] is not None else ""
    imgs = "".join(f'<img loading="lazy" src="assets/{s[k]}">' for k in ("mcp", "screen") if s[k])
    cap = f'{s["ts"]:%H:%M:%S} UTC &middot; {html.escape(s["trigger"] or "")}' + (f' &middot; {html.escape(s["sheet"])}' if s["sheet"] else "")
    return (f'<div class="shot" data-mcp="{s["mcp"] or ""}" data-screen="{s["screen"] or ""}" data-cap="{html.escape(cap, quote=True)}"{t}>'
            f'<div class="pair">{imgs}</div><div class="cap">{cap}</div></div>')


def render(turns, title, video_name, whisper, video_start):
    parts = []
    for turn in turns:
        t0 = turn["t0"]
        tools = [e for e in turn["events"] if e["kind"] == "tool"]
        data = [e for e in tools if DATA_TOOLS.search(e["name"]) and e.get("result")]
        says = [e for e in turn["events"] if e["kind"] == "say"]
        body = [f'<div class="ctx">{t0:%H:%M:%S} UTC &middot; sheet: {html.escape(turn["ctx"].get("active-sheet-name", "?"))}</div>',
                f'<div class="msg user">{html.escape(turn["prompt"])}</div>']
        if tools:
            nmut = sum(bool(MUTATING.search(e["name"])) for e in tools)
            body.append(f'<details class="group"><summary>Analyzed &middot; {len(tools)} steps, {nmut} changed the workbook</summary>'
                        + "".join(fmt_tool(e) for e in tools) + "</details>")
        if data:
            body.append(f'<details class="group"><summary>Data &amp; fields used &middot; {len(data)} lookups</summary>'
                        + "".join(fmt_tool(e) for e in data) + "</details>")
        body += [f'<div class="msg say">{html.escape(e["text"])}</div>' for e in says]
        body += [shot_html(s) for s in turn.get("shots", [])]
        if video_start and whisper:
            lo, hi = (t0 - video_start).total_seconds(), (turn["t1"] - video_start).total_seconds()
            body += [f'<div class="voice" data-t="{w["start"]:.1f}">&#127908; {html.escape(w["text"])}</div>'
                     for w in whisper if lo <= w["start"] < hi]
        parts.append('<section class="turn">' + "\n".join(body) + "</section>")
    vid = f'<video id="vid" controls src="assets/{video_name}"></video>' if video_name else ""
    return (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><style>{CSS}</style>"
            f'<header><h1>{html.escape(title)}</h1><span>{len(turns)} prompts &middot; replay of a Tableau Studio session</span></header>'
            f'<main><div id="chat">{"".join(parts)}</div><div id="stage"><div id="imgs"></div>'
            f'<div id="cap"></div>{vid}</div></main><script>{JS}</script>')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list recent Studio sessions")
    ap.add_argument("--session"), ap.add_argument("--out", default="demo_pkg")
    ap.add_argument("--video"), ap.add_argument("--video-start"), ap.add_argument("--whisper")
    ap.add_argument("--snapshots"), ap.add_argument("--title")
    ap.add_argument("--zip", action="store_true", help="also write <out>.zip for sharing")
    ap.add_argument("--log-dir", help="folder with Studio session *.jsonl (default: auto-detect)")
    a = ap.parse_args()
    global LOG_DIR
    LOG_DIR = a.log_dir
    if a.list:
        for f in find_sessions()[:15]:
            turns = parse_session(f)
            first = turns[0]["prompt"].splitlines()[0][:70] if turns else "-"
            print(f"{f.stem[:8]}  {datetime.fromtimestamp(f.stat().st_mtime):%m-%d %H:%M}  {len(turns):>2} prompts  {first}")
        return
    if not a.session:
        ap.error("--session or --list required")
    build(a)


if __name__ == "__main__":
    main()
