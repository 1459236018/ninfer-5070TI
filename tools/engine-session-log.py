"""引擎启停日志 —— 记录每次启停，保留最近 3 份，自动淘汰。

为什么需要它：
  引擎平时只往自己的控制台窗口输出，那个窗口一关就什么都不剩。
  崩溃（0xc0000409 / CUDA error / TDR 连带）之后控制台内容一并消失，
  事后完全无法判断"它到底是怎么没的"。

设计：
  - 每次引擎启动写一个 session 文件：logs/engine-sessions/session-YYYYmmdd-HHMMSS-pid.log
  - 启动时记录：时间、命令行、PID、显卡/显存快照、启动结果
  - 运行中每 N 秒采一次：显存、GPU util、功耗、温度（可选，避免太吵）
  - 退出时记录：正常 / 异常、退出码、运行时长、退出前的资源快照
  - **只保留最近 MAX_SESSIONS 份**（默认 3），多了删最旧的

退出码判定（Windows）：
  0                     正常退出
  0xC0000005 (3221225477) 访问违例
  0xC0000409 (3221226505) 栈缓冲溢出 / __fastfail（引擎崩过一次就是它）
  0xC0000135 (3221225781) DLL 未找到
  其他非 0               异常

用法（由 ninfer-tray.py 调用，也可独立运行）：
  python tools/engine-session-log.py --pid 12345 --args "..."   监视一个已有进程
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSDIR = os.path.join(ROOT, "logs", "engine-sessions")
MAX_SESSIONS = 3

# 已知退出码 -> 人话（都是 Windows NTSTATUS）
EXIT_CODES = {
    0: "正常退出",
    0xC0000005: "访问违例 (access violation)",
    0xC000001D: "非法指令 (illegal instruction)",
    0xC0000094: "整数除零",
    0xC00000FD: "栈溢出",
    0xC0000135: "DLL 未找到",
    0xC0000142: "DLL 初始化失败",
    0xC0000409: "栈缓冲溢出 / __fastfail（引擎自身崩溃的典型码）",
    0xC0000602: "fail-fast 异常",
    0xC000013A: "收到 CTRL_C / CTRL_BREAK —— 优雅退出的正常结果，不是崩溃",
    0x80000003: "断点",
    3221225477: "访问违例 (access violation)",
    3221226505: "栈缓冲溢出 / __fastfail（引擎自身崩溃的典型码）",
    3221225781: "DLL 未找到",
    3221225786: "收到 CTRL_C / CTRL_BREAK —— 优雅退出的正常结果，不是崩溃",
}

# 这些退出码代表"被正常叫停"，不算崩溃
GRACEFUL_CODES = {0, 0xC000013A, 3221225786}


def is_graceful(code):
    """退出码是否代表正常/优雅结束（含被 Ctrl-Break 叫停）。"""
    return code is not None and code in GRACEFUL_CODES


def _ensure_dir():
    os.makedirs(SESSDIR, exist_ok=True)


def rotate():
    """只保留最近 MAX_SESSIONS 份会话日志，多了删最旧的。

    按文件名里的时间戳排序（session-YYYYmmdd-HHMMSS-*.log），
    不依赖 mtime —— 用户手动改文件不会打乱顺序。
    """
    _ensure_dir()
    files = []
    for n in os.listdir(SESSDIR):
        if n.startswith("session-") and n.endswith(".log"):
            p = os.path.join(SESSDIR, n)
            try:
                files.append((os.path.getmtime(p), p))
            except OSError:
                pass
    files.sort()                       # 最旧在前
    removed = []
    while len(files) > MAX_SESSIONS:
        _, p = files.pop(0)
        try:
            os.remove(p)
            removed.append(os.path.basename(p))
        except OSError:
            pass
    return removed


def gpu_snapshot():
    """一行式显卡快照；取不到就返回空 dict（不让日志本身成为失败点）。"""
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.used,memory.free,"
             "utilization.gpu,power.draw,power.limit,clocks.sm,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8).stdout.strip()
        if not out:
            return {}
        p = [x.strip() for x in out.split(",")]
        return {"gpu": p[0], "mem_total_mib": p[1], "mem_used_mib": p[2],
                "mem_free_mib": p[3], "util_pct": p[4], "power_w": p[5],
                "power_limit_w": p[6], "clk_sm": p[7], "temp_c": p[8]}
    except Exception:
        return {}


def proc_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/fi", "PID eq %d" % pid, "/nh"],
                             capture_output=True, timeout=8).stdout or b""
        return str(pid).encode() in out
    except Exception:
        return False


def _ps_exit_code(pid):
    """PowerShell 查进程退出码。

    只能查到**仍在运行的**进程（Get-Process 对已退出进程返回空），
    所以对引擎崩溃没用 —— 保留它给"进程还活着但想提前读状态"的场景。
    """
    try:
        ps = ("$p=Get-Process -Id %d -ErrorAction SilentlyContinue;"
              "if($p -and $p.HasExited){$p.ExitCode}else{-999}" % pid)
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out and out != "-999":
            return int(out)
    except Exception:
        pass
    return None


def _wevt_exit_code(pid, since_epoch):
    """从 Windows 事件日志里捞退出码 —— 这是唯一可靠的途径。

    进程一消失，内存里的退出码就没了；Get-Process 查不到。
    但 Windows 每次进程退出都会写一条 Security 4689
    （event 4689: "进程已退出"，含 ProcessId 与 Status/退出状态）。
    普通用户读 Security 日志通常没权限，所以退一步用 System 日志里的
    Application Error (1000) / Application Hang (1002) 判断"是不是硬崩"。
    """
    try:
        ps = (
            "$since=(Get-Date).AddHours(-2);"
            "$e=Get-WinEvent -FilterHashtable @{LogName='Application';"
            "ProviderName=@('Application Error','Windows Error Reporting','Application Hang');"
            "StartTime=$since} -ErrorAction SilentlyContinue |"
            "Where-Object { $_.Message -match '\\\\%(pid)d' } | Select-Object -First 1;"
            "if($e){$e.Id}else{'0'}" % {"pid": pid}
        )
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=15).stdout.strip()
        if out in ("1000", "1002"):
            return "crash"
    except Exception:
        pass
    return None


def _watch_exit_unused(proc_handle_pid):
    """用 tasklist 无法拿到退出码；改用 CREATE 时持有的句柄不行（跨进程）。

    真正可行的办法：启动时就开一个 ctypes 的 OpenProcess + WaitForSingleObject
    拿 GetExitCodeProcess。见 watch_and_log()。
    """
    return None


def grab_exit_code(pid, timeout=0):
    """★ 盯一个**已在运行**的进程，等它退出并返回真实退出码。

    为什么不能事后查：进程一消失，退出码就从内存里没了，
    Get-Process / tasklist / CIM 全都查不到（实测确实如此）。
    唯一可靠的办法是在它还活着时 OpenProcess 拿句柄，
    然后 WaitForSingleObject + GetExitCodeProcess。

    本函数**阻塞**，给托盘起一个 daemon 线程调用。
    返回 None 表示拿不到（没权限 / 句柄没拿到），调用方应按"未知"处理。
    """
    try:
        import ctypes
        from ctypes import wintypes as wt
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wt.HANDLE
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not h:
            return None
        try:
            k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
            k32.WaitForSingleObject(h, 0xFFFFFFFF if not timeout else int(timeout * 1000))
            ec = wt.DWORD(0)
            k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
            if k32.GetExitCodeProcess(h, ctypes.byref(ec)):
                return int(ec.value)
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None
    return None


def wait_and_log(pid, args_text="", poll_seconds=15, quiet=False, proc=None):
    """阻塞直到进程结束，然后写日志。返回 (日志路径, 是否异常)。

    ★ 退出码怎么拿（这是本模块唯一有难度的点）：
      进程一退出，内存里的退出码就没了，之后 Get-Process / tasklist 全查不到。
      唯一可靠的做法是**在进程还活着时**用 OpenProcess 拿到句柄，
      等它退出后调 GetExitCodeProcess。
      所以如果调用方手里有 subprocess.Popen 对象（proc），必须先传进来；
      传不进来就只能靠事件日志兜底判"崩没崩"。
    """
    logger = SessionLogger(pid, args_text, poll_seconds).open()
    if not quiet:
        print("监视 PID %d，日志 -> %s" % (pid, logger.path))

    # 尽早拿句柄（进程可能几十毫秒后就没了）
    handle = None
    if proc is not None and hasattr(proc, "_handle") and proc._handle:
        handle = int(proc._handle)

    last_sample = time.time()
    while proc_alive(pid):
        time.sleep(2)
        if time.time() - last_sample >= logger.poll:
            logger.sample()
            last_sample = time.time()

    code = _read_exit_code(handle, proc, pid)
    crashed = code is None and _wevt_exit_code(pid, time.time()) == "crash"
    abnormal = crashed or (code is not None and code != 0)
    note = "由 engine-session-log.py 独立监视"
    if code is None and crashed:
        note += "；事件日志中有 Application Error/Hang 记录，判定为崩溃"
    path, dropped = logger.close(exit_code=code, abnormal=abnormal, note=note)
    if dropped and not quiet:
        print("轮转删除旧日志: %s" % ", ".join(dropped))
    return path, abnormal


def _read_exit_code(handle, proc, pid):
    """拿到真实退出码；拿不到返回 None。"""
    # ① 调用方给了 Popen：等它收尸，返回码最准
    if proc is not None and hasattr(proc, "wait"):
        try:
            return int(proc.wait(timeout=10))
        except Exception:
            pass
    # ② 手里有 ctypes 句柄
    if handle:
        try:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.WaitForSingleObject(ctypes.c_void_p(handle), 5000)
            ec = ctypes.c_ulong(0)
            if k32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(ec)):
                return int(ec.value)
        except Exception:
            pass
    # ③ 最后的常规途径（基本没用，进程已回收）
    return _ps_exit_code(pid)


class SessionLogger:
    def __init__(self, pid, args_text="", poll_seconds=15):
        self.pid = int(pid)
        self.args_text = args_text
        self.poll = max(3, int(poll_seconds))
        self.path = None
        self.t0 = time.time()
        self._fh = None

    def open(self):
        _ensure_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(SESSDIR, "session-%s-%d.log" % (stamp, self.pid))
        self._fh = open(self.path, "a", encoding="utf-8")
        self.write("=" * 70)
        self.write("引擎会话开始")
        self.write("时间      : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        self.write("PID       : %d" % self.pid)
        if self.args_text:
            self.write("命令行    : %s" % self.args_text)
        snap = gpu_snapshot()
        if snap:
            self.write("启动显存  : used %s MiB / total %s MiB / free %s MiB"
                       % (snap.get("mem_used_mib"), snap.get("mem_total_mib"),
                          snap.get("mem_free_mib")))
            self.write("显卡      : %s" % snap.get("gpu"))
        else:
            self.write("启动显存  : (nvidia-smi 不可用)")
        self.write("=" * 70)
        return self

    def write(self, line):
        if self._fh:
            try:
                self._fh.write(line + "\n")
                self._fh.flush()
            except Exception:
                pass

    def close(self, exit_code=None, abnormal=None, note=""):
        dur = time.time() - self.t0
        if abnormal is None:
            abnormal = exit_code is not None and exit_code != 0
        self.write("-" * 70)
        if abnormal:
            self.write("★ 引擎异常结束")
        else:
            self.write("引擎正常结束")
        self.write("时间      : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
        self.write("运行时长  : %s%s"
                   % (time.strftime("%H:%M:%S", time.gmtime(dur)), " (%.0f s)" % dur))
        if exit_code is None:
            self.write("退出码    : 未知（进程已回收，取不到 ExitCode）")
        else:
            human = EXIT_CODES.get(exit_code, "")
            self.write("退出码    : %d (0x%08X) %s" % (exit_code, exit_code & 0xFFFFFFFF, human))
        snap = gpu_snapshot()
        if snap:
            self.write("退出显存  : used %s MiB / free %s MiB"
                       % (snap.get("mem_used_mib"), snap.get("mem_free_mib")))
        if note:
            self.write("备注      : %s" % note)
        self.write("=" * 70)
        if self._fh:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        dropped = rotate()
        return self.path, dropped

    def sample(self):
        snap = gpu_snapshot()
        if snap:
            self.write("[%s] 显存 %s MiB / util %s%% / 功耗 %s W / 时钟 %s MHz / %s°C"
                       % (time.strftime("%H:%M:%S"), snap.get("mem_used_mib"),
                          snap.get("util_pct"), snap.get("power_w"),
                          snap.get("clk_sm"), snap.get("temp_c")))


def wait_and_log_legacy(pid, args_text="", poll_seconds=15, quiet=False):
    """【已废弃】不传 proc 时取不到真实退出码，判定不可靠，保留仅作说明。"""
    logger = SessionLogger(pid, args_text, poll_seconds).open()
    if not quiet:
        print("监视 PID %d，日志 -> %s" % (pid, logger.path))
    last_sample = time.time()
    while proc_alive(pid):
        time.sleep(2)
        if time.time() - last_sample >= logger.poll:
            logger.sample()
            last_sample = time.time()
    code = proc_exit_code(pid)
    path, dropped = logger.close(exit_code=code,
                                 note="由 engine-session-log.py 独立监视")
    if dropped and not quiet:
        print("轮转删除旧日志: %s" % ", ".join(dropped))
    return path, (code is not None and code != 0)


def main():
    ap = argparse.ArgumentParser(description="引擎启停日志（保留最近 3 份）")
    ap.add_argument("--pid", type=int, required=True, help="要监视的引擎进程 PID")
    ap.add_argument("--args", default="", help="引擎命令行（写进日志便于回溯）")
    ap.add_argument("--poll", type=int, default=15, help="采样间隔秒，默认 15")
    ap.add_argument("--rotate-only", action="store_true", help="只执行一次轮转然后退出")
    a = ap.parse_args()
    if a.rotate_only:
        print("删除: %s" % (", ".join(rotate()) or "(无需删除)"))
        return 0
    try:
        path, bad = wait_and_log(a.pid, a.args, a.poll)
        print("日志: %s" % path)
        return 1 if bad else 0
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main())
