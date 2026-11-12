#!/usr/bin/env python3
"""studio_comment.py - add highlighted comments to an existing replay package (index.html), e.g. from Claude.

  studio_comment.py packages/<name> --list
  studio_comment.py packages/<name> --turn 3 --text "Here the agent picked the wrong measure" [--author Claude] [--zip]

--turn N is the prompt number shown in the page (the numbered circle). The comment is appended at the end of that prompt.
The previous page is kept once as index.original.html (the same backup studio_prune.py uses).
"""
import argparse, html, re, shutil
from datetime import datetime
from pathlib import Path

SECTION = re.compile(r'<section class="turn">.*?</section>', re.S)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package")
    ap.add_argument("--list", action="store_true", help="list prompt numbers and their text")
    ap.add_argument("--turn", type=int), ap.add_argument("--text"), ap.add_argument("--author", default="Comment")
    ap.add_argument("--zip", action="store_true", help="rewrite <package>.zip")
    a = ap.parse_args()
    pkg = Path(a.package)
    index = pkg / "index.html"
    page = index.read_text()
    secs = list(SECTION.finditer(page))
    if a.list or not (a.turn and a.text):
        for i, m in enumerate(secs, 1):
            u = re.search(r'class="msg user"><div class="lbl">[^<]*</div>(.*?)</div>', m.group(0), re.S)
            print(f"{i:3}  {html.unescape(u.group(1))[:90] if u else '?'}  ({m.group(0).count('class=\"comment\"')} comments)")
        return
    if not 1 <= a.turn <= len(secs):
        raise SystemExit(f"--turn must be 1..{len(secs)}")
    m = secs[a.turn - 1]
    note = (f'<div class="comment"><div class="lbl">&#128172; {html.escape(a.author)} &middot; {datetime.now():%Y-%m-%d %H:%M}</div>'
            f'<div class="ctext">{html.escape(a.text)}</div></div>')
    sec = m.group(0)
    k = sec.rfind('<button class="addc"')
    sec = sec[:k] + note + sec[k:] if k >= 0 else sec[:-len("</section>")] + note + "</section>"
    if not (pkg / "index.original.html").exists():
        shutil.copy(index, pkg / "index.original.html")
    index.write_text(page[:m.start()] + sec + page[m.end():])
    print(f"comment added to prompt {a.turn}")
    if a.zip:
        bak, tmp = pkg / "index.original.html", pkg.parent / (pkg.name + ".original.html.tmp")
        shutil.move(bak, tmp)
        z = shutil.make_archive(str(pkg), "zip", pkg.parent, pkg.name)
        shutil.move(tmp, bak)
        print(f"zip -> {z}")


if __name__ == "__main__":
    main()
