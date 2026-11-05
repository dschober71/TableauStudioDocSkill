"""studio_logs.py - locate Tableau Studio (TableauAgent) session logs on any machine.

Order of strategies (first that yields session files wins, unless --log-dir given which is exclusive):
  1. explicit path     --log-dir / $STUDIO_LOG_DIR (a dir of *.jsonl, or a parent holding such dirs)
  2. running agent     find the live TableauAgent process, read its cwd (lsof), map to Claude's project-dir name
  3. folder name       any <config>/projects/* whose name mentions TableauAgent / tab-agent / WorkspaceExtension
  4. content sniffing  any recent *.jsonl under <config>/projects whose records carry <live-tableau-context>
<config> candidates: $CLAUDE_CONFIG_DIR, ~/.claude, ~/.config/claude (what the agent uses can differ per install).
"""
import os, re, subprocess
from pathlib import Path

NAME_HINTS = re.compile(r"tableau.?agent|tab-agent|workspaceextension", re.I)
MARKER = b"<live-tableau-context>"


def config_roots():
    cands = [os.environ.get("CLAUDE_CONFIG_DIR"), "~/.claude", "~/.config/claude"]
    seen, out = set(), []
    for c in cands:
        if c:
            p = Path(c).expanduser()
            if (p / "projects").is_dir() and p not in seen:
                seen.add(p), out.append(p / "projects")
    return out


def encode_cwd(path):
    """Claude names a project folder after its cwd with every non-alphanumeric char replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def agent_process_dirs():
    """cwd's of running TableauAgent processes (macOS/Linux lsof), as encoded project-dir names."""
    names = set()
    try:
        pids = subprocess.run(["pgrep", "-x", "TableauAgent"], capture_output=True, text=True).stdout.split()
        for pid in pids:
            out = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if line.startswith("n/"):
                    names.add(encode_cwd(line[1:]))
            # the binary path itself (versions/<ver>/bin/TableauAgent) -> its version dir is the agent's cwd
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True).stdout
            m = re.search(r"(/.*?/versions/[^/]+)/bin/", cmd)
            if m:
                names.add(encode_cwd(m.group(1)))
    except Exception:
        pass
    return names


def sniff(f):
    try:
        with f.open("rb") as fh:
            return MARKER in fh.read(200_000)
    except OSError:
        return False


def _jsonls(dirs):
    return [f for d in dirs if d.is_dir() for f in d.glob("*.jsonl")]


def find_sessions(explicit=None, max_sniff=60):
    """-> (list of session files newest first, human-readable note on how they were found)"""
    explicit = explicit or os.environ.get("STUDIO_LOG_DIR")
    if explicit:
        p = Path(explicit).expanduser()
        files = list(p.glob("*.jsonl")) or list(p.glob("*/*.jsonl"))
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True), f"explicit: {p}"
    roots = config_roots()
    live = agent_process_dirs()
    files = _jsonls(r / n for r in roots for n in live)
    if files:
        return _sorted(files), "running TableauAgent process"
    files = _jsonls(d for r in roots for d in r.iterdir() if NAME_HINTS.search(d.name))
    if files:
        return _sorted(files), "project-folder name"
    recent = _sorted(_jsonls(d for r in roots for d in r.iterdir()))[:max_sniff * 4]
    files = [f for f in recent if sniff(f)][:max_sniff]
    return files, "content match (<live-tableau-context>)"


def _sorted(files):
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def newest_session(explicit=None):
    files, how = find_sessions(explicit)
    return (files[0] if files else None), how


if __name__ == "__main__":
    fs, how = find_sessions()
    print(f"found {len(fs)} sessions via {how}")
    for f in fs[:5]:
        print(" ", f)
