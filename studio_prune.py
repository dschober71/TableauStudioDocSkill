#!/usr/bin/env python3
"""studio_prune.py - apply a cleaned replay page to its package: drop unused screenshots, refresh the zip.

Workflow: open packages/<name>/index.html -> Edit -> tick parts -> Delete selected -> Save cleaned HTML
(downloads index.cleaned.html), then:

  studio_prune.py packages/<name> --html ~/Downloads/index.cleaned.html [--zip]

Without --html it only prunes assets that the current index.html no longer references.
"""
import argparse, shutil
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", help="package folder containing index.html and assets/")
    ap.add_argument("--html", help="cleaned page saved from the browser; replaces index.html (old one kept as index.original.html)")
    ap.add_argument("--zip", action="store_true", help="rewrite <package>.zip")
    a = ap.parse_args()
    pkg = Path(a.package)
    index = pkg / "index.html"
    if a.html:
        src = Path(a.html).expanduser()
        if not src.exists():
            raise SystemExit(f"not found: {src}")
        if not (pkg / "index.original.html").exists():
            shutil.copy(index, pkg / "index.original.html")
        shutil.copy(src, index)
    text = index.read_text()
    removed = freed = 0
    for f in sorted((pkg / "assets").glob("*")):
        if f.name not in text:
            freed += f.stat().st_size
            removed += 1
            f.unlink()
    print(f"removed {removed} unused file(s), freed {freed/1e6:.1f} MB")
    if a.zip:
        bak = pkg / "index.original.html"      # keep the backup out of the shared zip
        tmp = pkg.parent / (pkg.name + ".original.html.tmp")
        if bak.exists():
            shutil.move(bak, tmp)
        z = shutil.make_archive(str(pkg), "zip", pkg.parent, pkg.name)
        if tmp.exists():
            shutil.move(tmp, bak)
        print(f"zip -> {z}")


if __name__ == "__main__":
    main()
