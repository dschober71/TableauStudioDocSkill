"""macperms.py - macOS permission checks and window lookup, stdlib only (ctypes + plistlib, no pyobjc)."""
import ctypes, ctypes.util, plistlib, subprocess, sys
from pathlib import Path

_FW = "/System/Library/Frameworks/%s.framework/%s"


def _lib(name):
    return ctypes.CDLL(_FW % (name, name))


def screen_recording_granted():
    """True if THIS process tree (the terminal/app that launched us) may capture other apps' windows."""
    try:
        cg = _lib("CoreGraphics")
        cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool
        return bool(cg.CGPreflightScreenCaptureAccess())
    except Exception:
        return None


def request_screen_recording():
    """Shows the system prompt once and registers the app in System Settings > Screen Recording."""
    cg = _lib("CoreGraphics")
    cg.CGRequestScreenCaptureAccess.restype = ctypes.c_bool
    return bool(cg.CGRequestScreenCaptureAccess())


def on_screen_windows():
    """List of dicts: owner, title, id, layer, w, h for all on-screen windows (needs no permission)."""
    cg, cf = _lib("CoreGraphics"), _lib("CoreFoundation")
    cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
    cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    cf.CFPropertyListCreateData.restype = ctypes.c_void_p
    cf.CFPropertyListCreateData.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long, ctypes.c_ulong, ctypes.c_void_p]
    cf.CFDataGetLength.restype = ctypes.c_long
    cf.CFDataGetLength.argtypes = [ctypes.c_void_p]
    cf.CFDataGetBytePtr.restype = ctypes.c_void_p
    cf.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    arr = cg.CGWindowListCopyWindowInfo(1 | 16, 0)  # OnScreenOnly | ExcludeDesktopElements
    data = cf.CFPropertyListCreateData(None, arr, 200, 0, None)  # 200 = binary plist
    raw = ctypes.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data))
    cf.CFRelease(data), cf.CFRelease(arr)
    out = []
    for w in plistlib.loads(raw):
        b = w.get("kCGWindowBounds", {})
        out.append({"owner": w.get("kCGWindowOwnerName", ""), "title": w.get("kCGWindowName", ""),
                    "id": w.get("kCGWindowNumber"), "layer": w.get("kCGWindowLayer"),
                    "w": int(b.get("Width", 0)), "h": int(b.get("Height", 0))})
    return out


def find_window(owner="Tableau", title_contains=None):
    """Largest normal window of the app named `owner` (Tableau Studio/Desktop's process is called 'Tableau')."""
    ws = [w for w in on_screen_windows() if w["owner"] == owner and w["layer"] == 0
          and (title_contains is None or title_contains in w["title"])]
    return max(ws, key=lambda w: w["w"] * w["h"], default=None)


def capture_window(window_id, dest):
    """screencapture -l captures one window even when other windows cover it (no focus change)."""
    r = subprocess.run(["screencapture", "-x", "-o", "-l", str(window_id), str(dest)], capture_output=True)
    return r.returncode == 0 and Path(dest).exists() and Path(dest).stat().st_size > 0


def open_settings():
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])


def responsible_app():
    """Name of the app macOS will ask you to grant (walks up to the first .app in the process ancestry)."""
    import os
    pid = os.getpid()
    for _ in range(12):
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not out:
            break
        ppid, _, cmd = out.strip().partition(" ")
        cmd = cmd.strip()
        if ".app/Contents/MacOS" in cmd:
            return cmd.split(".app/")[0].split("/")[-1] + ".app"
        pid = int(ppid)
    return None


if __name__ == "__main__":
    print("platform", sys.platform, "| launched from:", responsible_app())
    print("screen recording granted:", screen_recording_granted())
    w = find_window()
    print("Tableau window:", w)
    if w:
        d = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/tableau_window_test.png")
        print("window capture ok:", capture_window(w["id"], d), d)
