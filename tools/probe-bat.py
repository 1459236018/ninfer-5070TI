# -*- coding: utf-8 -*-
"""临时诊断：无窗口跑 start-mtp-slim.bat /silent，收集它的 stdout/stderr，
并检查 15 秒后引擎进程与引擎控制台窗口标题。

用法:  python probe-bat.py            # 跑一次并观察 15 秒后自动收尾
       python probe-bat.py keep       # 观察后不收尾（留给人工看窗口）
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

def _find_root():
    """定位项目根：环境变量 > 标记文件探测 > 兜底上一级。不依赖任何硬编码路径。"""
    _env = os.environ.get("NINFER_ROOT")
    if _env and os.path.exists(os.path.join(_env, "start-mtp-slim.bat")):
        return os.path.abspath(_env)
    _cur = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        _cur = os.path.dirname(_cur)
        if os.path.exists(os.path.join(_cur, "start-mtp-slim.bat")):
            return _cur
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = _find_root()
BAT = os.path.join(ROOT, "start-mtp-slim.bat")
def _find_workdir(root):
    """诊断输出目录：优先 .workbuddy/bench（本机布局），否则回退 logs/。都会确保存在。"""
    for _rel in ((".workbuddy", "bench"), ("logs",)):
        _p = os.path.join(root, *_rel)
        if os.path.isdir(_p):
            return _p
    _p = os.path.join(root, "logs")
    os.makedirs(_p, exist_ok=True)
    return _p


OUT = os.path.join(_find_workdir(ROOT), "_tmptitle", "bat-out.txt")
EXE = "ninfer-serve-120a.exe"
CREATE_NO_WINDOW = 0x08000000


def engine_pids():
    try:
        r = subprocess.run(["tasklist", "/fi", "IMAGENAME eq " + EXE, "/fo", "csv", "/nh"],
                           capture_output=True, text=True, encoding="gbk",
                           errors="replace", timeout=20)
    except Exception:
        return []
    pids = []
    for line in r.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == EXE:
            pids.append(int(parts[1]))
    return pids


def consoles():
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        cls = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, cls, 256)
        if cls.value == "ConsoleWindowClass":
            t = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(h, t, 512)
            out.append((h, pid.value, t.value, bool(u.IsWindowVisible(h)), bool(u.IsIconic(h))))
        return True

    u.EnumWindows(cb, 0)
    return out


if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    print("before: engine pids =", engine_pids())
    with open(OUT, "w", encoding="utf-8", errors="replace") as f:
        p = subprocess.Popen(["cmd", "/c", BAT, "/silent"], cwd=ROOT,
                             stdout=f, stderr=subprocess.STDOUT,
                             creationflags=CREATE_NO_WINDOW)
        rc = p.wait(timeout=120)
    print("bat exit code =", rc)
    print("--- bat output ---")
    print(open(OUT, encoding="utf-8", errors="replace").read()[-3000:])
    time.sleep(2)
    pids = engine_pids()
    print("after : engine pids =", pids)
    for h, pid, title, vis, ico in consoles():
        print("  console hwnd=%-8d pid=%-6d visible=%-5s iconic=%-5s title=%r"
              % (h, pid, vis, ico, title))
    if "keep" not in sys.argv and pids:
        for pid in pids:
            subprocess.run(["taskkill", "/f", "/pid", str(pid)], capture_output=True)
        print("killed:", pids)
