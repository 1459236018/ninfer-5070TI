#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
AI引擎 托盘宿主 (tray host)

用途：双击桌面快捷方式后 —— 引擎无窗口启动，任务栏右下角出现一个托盘图标
（默认位于「隐藏的图标」面板里）。右键菜单可开日志、开项目目录、停引擎。

为什么需要它：引擎本身是控制台程序，直接跑会在任务栏留一个窗口；
本脚本以 CREATE_NO_WINDOW 方式拉起 start-*.bat，再由 pystray 提供托盘图标。

约定：
  - 脚本路径固定为  <项目>\.workbuddy\tools\ninfer-tray.py
  - 由 start-mtp-slim-silent.vbs 用 pythonw.exe（无控制台）启动
  - 引擎日志写到      <项目>\logs\engine-mtp-slim.log
  - 本脚本自身日志写  <项目>\logs\tray.log
"""
import os
import subprocess
import sys
import threading
import time
import traceback

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


PROJECT = _find_root()

# 启动器选择（2026-10-16 加）：
#   优先级  --bat <文件名>   >   环境变量 NINFER_BAT   >   默认 start-mtp-slim.bat
# 桌面快捷方式会弹「二选一」并把所选启动器用 --bat 传进来；
# 不传参数时取值与改动前完全一致，行为不变。
_BAT_NAME = None
for _i, _a in enumerate(sys.argv[1:], 1):
    if _a == "--bat" and _i + 1 < len(sys.argv):
        _BAT_NAME = sys.argv[_i + 1]
        break
if not _BAT_NAME:
    _BAT_NAME = os.environ.get("NINFER_BAT") or "start-mtp-slim.bat"
BAT = os.path.join(PROJECT, _BAT_NAME)
LOGDIR = os.path.join(PROJECT, "logs")
ENGLOG = os.path.join(LOGDIR, "engine-mtp-slim.log")
TRAYLOG = os.path.join(LOGDIR, "tray.log")
def _find_assets(root):
    """图标目录：优先包内 assets/，回退 .workbuddy/assets/；都没有就建前者。"""
    for _rel in (("assets",), (".workbuddy", "assets")):
        _p = os.path.join(root, *_rel)
        if os.path.isdir(_p):
            return _p
    _p = os.path.join(root, "assets")
    os.makedirs(_p, exist_ok=True)
    return _p


ASSETS = _find_assets(PROJECT)
ENGINE_EXE = "ninfer-serve-120a.exe"
URL = "http://127.0.0.1:8094/v1/models"
CREATE_NO_WINDOW = 0x08000000

# 托盘图标 = make_icon.py 生成的四个状态图（与桌面 ninfer.ico 同一套设计）
TRAY_PNGS = {
    "starting": "tray-starting.png",
    "ready": "tray-ready.png",
    "failed": "tray-failed.png",
    "stopped": "tray-stopped.png",
}

APPNAME = "AI引擎"
TITLE = "%s (port 8094)" % APPNAME       # 托盘 tooltip
WIN_TITLE = "%s (port 8094)" % APPNAME   # 引擎控制台窗口标题（必须由本进程设置，见 set_window_title）

# 预热代理（2026-10-16）：客户端 -> 127.0.0.1:8094（本代理）-> 引擎 127.0.0.1:8095。
# 为什么要它：引擎一重启，KV 检查点目录与共享前缀目录就全空，而 WorkBuddy 每个请求
# 都顶着 ~36k 的固定前导（system + tools + 技能 + 记忆），于是"重启后的第一句"必须
# 全量 prefill —— 实测 30,389 token / 1,455 tok/s ≈ 22 s，客户端看到 42 s。
# 代理顺手记下真实请求的稳定前导，引擎就绪后回放一次预热，把这 22 s 挪到后台；
# 实测效果：重启后第一句 TTFT 22.2 s -> 0.01 s，cached 30,378/30,394（99.9%）。
# 端口安排：引擎让到 8095（见 start-mtp-slim.bat 的 PORT SWAP 注释），客户端不用改。
PROXY = [None]                            # WarmupProxy 实例（延迟创建）
PROXY_LISTEN = ("127.0.0.1", 8094)        # 客户端指这里（models.json 保持 8094，零配置）
ENGINE_ADDR = ("127.0.0.1", 8095)         # 引擎实际端口（与 start-mtp-slim.bat 一致）

STATE = {"phase": "starting", "text": "启动中…", "detail": "正在加载模型，约 30 秒"}

STOP = threading.Event()        # 置位后监视线程干净退出
CURRENT_ICON = [None]           # 供后台线程弹气泡用
WATCH_INTERVAL = 0.05           # 窗口检查间隔：只调 IsWindowVisible/IsIconic，开销可忽略
PROC_CHECK_EVERY = 20           # 约每 1 秒跑一次 tasklist 检查引擎存活
SHOW_DEBOUNCE = 0.4             # 左键单击/双击会连发 WM_LBUTTONUP，去抖


# ---------------------------------------------------------------- 基础工具
def log(msg):
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(TRAYLOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def engine_ready(timeout=1.5):
    import urllib.request
    try:
        with urllib.request.urlopen(URL, timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def engine_process_running():
    """字节比较，避开中文 Windows 上 tasklist 的 GBK 输出（text=True 会 UnicodeDecodeError）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/fi", "IMAGENAME eq %s" % ENGINE_EXE, "/nh"],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
        return ENGINE_EXE.encode("ascii", "ignore").lower() in (out.stdout or b"").lower()
    except Exception:
        return False


def launch_engine():
    os.makedirs(LOGDIR, exist_ok=True)
    # 2026-10-19: 启动前的第一道闸。前缀缓存只在优雅退出时才写盘，所以这一刻文件
    # 就是上次留下的版本；如果它已经超标，先删掉再启动，restore 的永远是小缓存，
    # 引擎就不会进入"大缓存 + 第一个请求"那个会崩的状态。
    prune_prefix_cache()
    log("launching: %s /silent" % BAT)
    # 引擎现在带自己的控制台窗口启动（.bat 里 start /min）。启动器这个 cmd 进程
    # 本身仍然无窗口（CREATE_NO_WINDOW），只有引擎那个窗口会出现 —— 我们会把它藏起来。
    return subprocess.Popen(
        ["cmd", "/c", BAT, "/silent"],
        cwd=PROJECT,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
    )


# ---------------------------------------------------------------- 引擎控制台窗口
def engine_pids():
    """返回引擎进程的 PID 列表（tasklist 输出按 GBK 解码，避免 UnicodeDecodeError）。"""
    try:
        raw = subprocess.run(
            ["tasklist", "/fi", "IMAGENAME eq %s" % ENGINE_EXE, "/fo", "csv", "/nh"],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        ).stdout or b""
    except Exception:
        return []
    pids = []
    for line in raw.decode("gbk", "replace").splitlines():
        parts = [p.strip().strip('"') for p in line.split(",")]
        if parts and parts[0].lower() == ENGINE_EXE.lower() and len(parts) > 1:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


_U32 = [None]


def user32():
    """带 argtypes 的 user32。

    HWND 在 64 位下是 8 字节指针，不声明 argtypes 的话 ctypes 会按 C int（4 字节）
    传参，句柄值一大就被截断 → 调用静默失败。监控线程每 50 ms 都要调一次，所以
    这里一次性把用到的函数签名定死。
    """
    if _U32[0] is None:
        import ctypes
        from ctypes import wintypes
        u = ctypes.windll.user32
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.ShowWindow.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        u.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        u.SetWindowTextW.restype = wintypes.BOOL
        _U32[0] = u
    return _U32[0]


def set_window_title(hwnd, text=None):
    """跨进程改引擎控制台窗口的标题。

    为什么不在 .bat 里写 "AI引擎"：实测（2026-10-12）在 start-mtp-slim.bat 的
    start "..." 行里放 GBK 中文会让 cmd 的批处理解析直接崩掉 —— 整批命令被拆散，
    只刷 "'M' 不是内部或外部命令" 十几遍，引擎根本没被拉起来。所以 bat 里只留
    ASCII 标题 "AI engine (port 8094)"，中文标题改由这里用 SetWindowTextW 设置：
    改窗口「文本」不像改窗口「过程」(GWLP_WNDPROC)，是允许跨进程的。
    """
    try:
        ok = bool(user32().SetWindowTextW(hwnd, text or WIN_TITLE))
    except Exception:
        log("set_window_title failed:\n" + traceback.format_exc())
        return False
    if ok:
        log("engine console retitled to %r" % (text or WIN_TITLE))
    return ok


def find_engine_console():
    """找到引擎控制台窗口（类名 ConsoleWindowClass）。找不到返回 None。"""
    import ctypes
    from ctypes import wintypes

    pids = set(engine_pids())
    if not pids:
        return None
    u32 = ctypes.windll.user32
    found = []

    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, buf, 256)
            if buf.value == "ConsoleWindowClass":
                found.append(hwnd)
                return False        # 找到一个就够了
        return True

    u32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                       wintypes.LPARAM)(_cb), 0)
    return found[0] if found else None


def show_engine_console():
    """把引擎那个黑框调到前台（隐藏 / 最小化状态都能恢复）。返回是否成功。"""
    hwnd = find_engine_console()
    if not hwnd:
        return False
    u = user32()
    set_window_title(hwnd)                  # 呼出时确保是中文标题
    u.ShowWindow(hwnd, 9)                   # SW_RESTORE
    u.SetForegroundWindow(hwnd)
    log("engine console shown (hwnd=%s)" % hwnd)
    return True


def hide_engine_console():
    """把引擎窗口藏起来（不结束引擎）→ 任务栏上的按钮同时消失。"""
    hwnd = find_engine_console()
    if not hwnd:
        return False
    user32().ShowWindow(hwnd, 0)            # SW_HIDE
    return True


def notify(text, title=None):
    ic = CURRENT_ICON[0]
    if ic is None:
        return
    try:
        ic.notify(text, title or TITLE)
    except Exception:
        log("notify failed:\n" + traceback.format_exc())


_last_show = [0.0]


def on_show_console(icon=None, item=None):
    """左键点托盘图标 / 菜单项：把引擎那个黑框呼到前台。

    引擎不在时只弹一句提示 —— 不再像以前那样默默打开项目文件夹（点几下会
    弹好几层窗口，容易让人以为点错了）。
    """
    now = time.time()
    if now - _last_show[0] < SHOW_DEBOUNCE:      # 双击会连发两次 UP，去抖
        return
    _last_show[0] = now
    if show_engine_console():
        return
    if engine_process_running():
        notify("引擎还在加载，窗口稍后才可用。")
    else:
        notify("引擎当前没有运行 —— 双击桌面快捷方式可重新启动。")


# ---------------------------------------------------------------- 常驻监视
# ---------------- 引擎启停日志（2026-10-21）----------------
# 目的：引擎崩溃后控制台内容随窗口一起消失，事后无法判断它为何消失。
# 做法：引擎一出现就抓 PID 交给 session_log 监视线程；引擎消失时写结论并轮转。
# 只保留最近 3 份（见 tools/engine-session-log.py 的 MAX_SESSIONS）。
SESSION = {"logger": None, "pid": None, "exit_code": None, "finished": False}


def _load_session_log():
    """把 tools/engine-session-log.py 当模块载入（文件名带连字符，不能直接 import）。"""
    import importlib.util
    path = os.path.join(PROJECT, "tools", "engine-session-log.py")
    if not os.path.exists(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location("engine_session_log", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        log("session log module load failed:\n" + traceback.format_exc())
        return None


def start_session_log():
    """引擎进程出现后调用：抓 PID、开一份 session 日志、起退出码监视线程。"""
    if SESSION["logger"] is not None:
        return
    log("session: begin")
    mod = _load_session_log()
    if mod is None:
        log("session: module load FAILED")
        return
    pids = engine_pids()
    if not pids:
        log("session: no engine pid found")
        return
    pid = pids[0]
    try:
        lg = mod.SessionLogger(pid, "由托盘启动（start-mtp-slim.bat）").open()
        SESSION["logger"] = lg
        SESSION["pid"] = pid
        log("session log opened: %s (pid %d)" % (lg.path, pid))
    except Exception:
        log("session log open failed:\n" + traceback.format_exc())
        return

    # 托盘手里没有引擎的 Popen（引擎是 bat 里 start 出来的），
    # 所以必须**趁引擎还活着**抢先拿句柄，否则退出码事后永远查不到。
    def _watch():
        try:
            code = mod.grab_exit_code(pid)      # 阻塞到进程退出
            SESSION["exit_code"] = code
            log("engine pid %d exit code = %s" % (pid, code))
        except Exception:
            log("exit code watch failed:\n" + traceback.format_exc())

    threading.Thread(target=_watch, name="session-exit", daemon=True).start()


def finish_session_log(abnormal, note=""):
    """引擎消失后调用：写退出结论、轮转。幂等 —— 只写一次。

    为什么要幂等：on_quit() 和 engine_gone() 都可能触发收尾，
    先到的写入结论，后到的直接返回，避免一份日志被写两个"结束"。
    """
    lg = SESSION.get("logger")
    if lg is None or SESSION.get("finished"):
        return
    SESSION["finished"] = True
    code = SESSION.get("exit_code")
    # 退出码本身能说明"是正常叫停还是崩了"，比调用方的猜测更可信：
    # 0xC000013A = 收到 CTRL_BREAK/CTRL_C，这是引擎优雅退出的正常结果。
    try:
        mod = _load_session_log()
        if mod is not None and mod.is_graceful(code):
            abnormal = False
    except Exception:
        pass
    if code is None and SESSION.get("pid"):
        try:
            mod = _load_session_log()
            if mod is not None:
                code = mod._ps_exit_code(SESSION["pid"])
        except Exception:
            pass
    try:
        path, dropped = lg.close(exit_code=code, abnormal=abnormal,
                                 note=note or ("异常退出" if abnormal else "正常退出"))
        log("session log closed: %s (%s, exit=%s)%s"
            % (os.path.basename(path), "异常" if abnormal else "正常", code,
               ("，轮转删除 " + ", ".join(dropped)) if dropped else ""))
    except Exception:
        log("session log close failed:\n" + traceback.format_exc())
    SESSION["logger"] = None
    SESSION["pid"] = None
    SESSION["exit_code"] = None
    SESSION["finished"] = False


def engine_gone(icon):
    """引擎进程消失（用户点了窗口的 X）：托盘同步收尾，右下角不留空壳。"""
    log("engine process exited -> tray host shuts down")
    # 先落一份启停日志，再丢缓存 —— 崩溃现场就是这一刻，越早写越好。
    finish_session_log(abnormal=True, note="引擎进程消失（托盘 watchdog 判定）")
    # 异常退出（崩溃 / 被强杀）：这份缓存可能已经坏了（2026-10-19 的 CUDA event 事故
    # 就是坏缓存 + 大盘点触发的），无条件丢掉，下次启动走全量，不赌它。
    try:
        cache = os.path.join(LOGDIR, "prefix-cache.bin")
        if os.path.exists(cache):
            mib = os.path.getsize(cache) / 1048576.0
            os.remove(cache)
            log("prefix cache dropped after abnormal exit: %.1f MiB" % mib)
    except Exception:
        log("cache drop failed:\n" + traceback.format_exc())
    STATE.update(phase="stopped", text="已停止", detail="引擎已关闭")
    try:
        icon.icon = ICONS.get("stopped", ICONS["failed"])
        icon.title = "%s — 已停止" % TITLE
        icon.notify("引擎已关闭，托盘图标即将自行退出。", TITLE)
    except Exception:
        pass
    time.sleep(2.5)
    try:
        icon.stop()
    except Exception:
        pass


def watchdog(icon):
    """常驻监视线程 —— 补齐控制台窗口本身做不到的两件事。

    引擎窗口是 conhost 建的，属于 conhost 进程；Windows 不允许别的进程改写它的
    窗口过程（跨进程 SetWindowLongPtr(GWLP_WNDPROC) 必失败），所以"点最小化"和
    "点 X"没法在窗口内部拦截。这里用外部轮询等价实现：

      ① 窗口被最小化（点了最小化按钮 / Win+D / 显示桌面）→ 立刻 SW_HIDE，
         任务栏上的按钮随之消失 = "回到右下角托盘"。
      ② 引擎进程消失（点了 X）→ 提示一句后托盘图标自行退出。
    """
    u = user32()
    hwnd = None
    ticks = 0
    misses = 0

    while not STOP.is_set():
        ticks += 1
        try:
            if hwnd is not None and not u.IsWindow(hwnd):
                hwnd = None

            if hwnd is None:
                hwnd = find_engine_console()
                if hwnd is None:
                    phase = STATE["phase"]
                    if phase == "ready":
                        # 窗口没了：可能只是还没建好，连续几次都查不到进程才判退出
                        if engine_process_running():
                            misses = 0
                        else:
                            misses += 1
                            if misses >= 3:
                                engine_gone(icon)
                                return
                    elif phase == "failed":
                        return          # 启动失败：留红色图标给用户排查
                    time.sleep(0.4)
                    continue
                misses = 0
                set_window_title(hwnd)          # 窗口一出现就换成中文标题（bat 里只能放 ASCII）

            if u.IsWindowVisible(hwnd) and u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 0)               # SW_HIDE
                log("engine console auto-hidden (minimize -> tray)")
        except Exception:
            log("watchdog window check failed:\n" + traceback.format_exc())

        if ticks % PROC_CHECK_EVERY == 0 and STATE["phase"] == "ready":
            try:
                if not engine_process_running():
                    engine_gone(icon)
                    return
            except Exception:
                log("watchdog process check failed:\n" + traceback.format_exc())

        time.sleep(WATCH_INTERVAL)


# 引擎的 SetConsoleCtrlHandler 只对 CTRL_BREAK 走优雅退出（反汇编 VA 0x78b0：
# ctrl_type==1 -> 调 0x70220 后返回 TRUE；ctrl_type==0 即 CTRL_C 直接返回 FALSE）。
# 而 GenerateConsoleCtrlEvent 只作用于"调用方所在的控制台"，引擎跑在自己的控制台里，
# 所以必须先 AttachConsole 才能把信号送进去。发送方自己也会收到该事件，因此必须由
# 独立进程发，不能在本进程内直接调。
GRACEFUL_BREAK_SRC = (
    "import sys,time,ctypes\n"
    "k=ctypes.WinDLL('kernel32',use_last_error=True)\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(int(sys.argv[1])):\n"
    "    sys.exit(2)\n"
    "time.sleep(0.35)\n"
    "k.GenerateConsoleCtrlEvent(1,0)\n"
    "time.sleep(0.3)\n"
)


def send_ctrl_break(pid):
    """让引擎走 CTRL_BREAK 优雅退出（hybrid 前缀缓存只在干净关闭时落盘）。"""
    try:
        subprocess.run(
            [sys.executable, "-c", GRACEFUL_BREAK_SRC, str(pid)],
            creationflags=CREATE_NO_WINDOW, timeout=20,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        log("ctrl-break helper failed (pid %s):\n%s" % (pid, traceback.format_exc()))
        return False


def stop_engine():
    pids = engine_pids()
    if pids:
        log("stopping engine gracefully, pid(s)=%s" % (pids,))
        for pid in pids:
            send_ctrl_break(pid)
        # 实测优雅退出 0.5 s，写前缀缓存（0.6-1.2 GiB）也只几十毫秒。
        deadline = time.time() + 15
        while time.time() < deadline:
            if not engine_pids():
                log("engine exited gracefully")
                return
            time.sleep(0.25)
        log("graceful stop timed out after 15 s -> forcing")
    else:
        log("stopping engine (no running engine found)")
    try:
        subprocess.run(
            ["taskkill", "/f", "/im", ENGINE_EXE],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        log("stop failed:\n" + traceback.format_exc())


# 2026-10-19: hybrid 前缀缓存只增不删。实测攒到 2.88 GiB / 15 个快照后，引擎在
# restore 时会因为某一层的 CUDA event 句柄失效而在**第一个请求**上退出：
#   CUDA_CHECK(cudaStreamWaitEvent(ctx_.stream, layer_ready_[layer], 0))
#     failed: cudaErrorInvalidResourceHandle
# 引擎代码不可改（预编译），所以在**启动前**把超标的缓存丢掉：下次走全量 prefill
# （慢一次），但不会崩。
#
# 阈值怎么定的（实测点，中间是推断）：
#   642 MiB / 4 snapshots    -> 跑了一上午正常
#   1291 MiB / ~7 snapshots  -> 崩（2026-10-19 20:15 实测）
#   2.88 GiB / 15 snapshots  -> 崩
# 所以安全区很窄，就在 642~1291 MiB 之间。取 768 MiB —— 比已知安全点高 126 MiB，
# 又离已知崩点还有 523 MiB。想改：set NINFER_CACHE_CAP_MIB=640 / 1024 ...
# 注意：一轮 18K 对话大约产生 642 MiB，所以这个阈值等于"每次重启后能加速一轮"，
# 之后就要重攒 —— 这已经是 hybrid 在安全范围内的上限了。
CACHE_PRUNE_MIB = int(os.environ.get("NINFER_CACHE_CAP_MIB", "768") or "768")


def prune_prefix_cache():
    path = os.path.join(LOGDIR, "prefix-cache.bin")
    try:
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        mib = size / 1048576.0
        if size <= CACHE_PRUNE_MIB * 1024 * 1024:
            log("prefix cache kept: %.1f MiB (cap %d MiB)" % (mib, CACHE_PRUNE_MIB))
            return
        os.remove(path)
        log("prefix cache pruned: %.1f MiB > %d MiB cap -> next start is a full prefill"
            % (mib, CACHE_PRUNE_MIB))
    except Exception:
        log("prefix cache prune failed:\n" + traceback.format_exc())


def open_path(p):
    try:
        os.startfile(p)
    except Exception:
        log("open failed: %s\n%s" % (p, traceback.format_exc()))


# ---------------------------------------------------------------- 图标绘制
def make_icon(bg):
    from PIL import Image, ImageDraw
    s = 64
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, s - 3, s - 3), radius=14, fill=bg)
    w = (255, 255, 255, 255)
    d.rectangle((18, 17, 25, 47), fill=w)                                  # 左竖
    d.rectangle((39, 17, 46, 47), fill=w)                                  # 右竖
    d.polygon([(19, 17), (28, 17), (45, 47), (36, 47)], fill=w)            # 斜笔
    return img


COLORS = {
    "starting": (232, 163, 61, 255),   # 橙：加载中
    "ready": (45, 127, 249, 255),      # 蓝：就绪
    "failed": (229, 72, 77, 255),      # 红：失败
    "stopped": (128, 138, 150, 255),   # 灰：已停止（只闪一下就退出）
}
ICONS = {}


def load_tray_icons():
    """用 make_icon.py 生成的状态图；文件缺失时回退到下面的内置画法（不至于没图标）。"""
    from PIL import Image
    out = {}
    for phase, fn in TRAY_PNGS.items():
        try:
            out[phase] = Image.open(os.path.join(ASSETS, fn)).convert("RGBA")
        except Exception:
            log("tray icon %s unusable -> fallback drawing" % fn)
            out[phase] = make_icon(COLORS[phase])
    return out


# ---------------------------------------------------------------- 主流程
# ---------------------------------------------------------------- 预热代理
def start_warmup_proxy():
    """预热代理（2026-10-16 16:30 重新启用）：8094 -> 8095。

    根因（实测）：WorkBuddy 每次请求注入的 prompt 里，`ToolSearch.description`
    带一份随连接器状态变动的 <available_deferred_tools> 清单 —— 9,060 字符 ≈ 2.5k token，
    位于 tools 末尾。它一变，prompt 前缀就在 18k~22k token 处断开，每次对话多算
    18k+ token ≈ 13~15 s（实测 ttft 18.8s / 20.5s；对照 15:27 那批连续对话仅 379~1,476 ms）。

    代理做三件事：
      1) strip_dynamic_tool_list：删掉那份重复清单（system 的 <connector-status> 段已有）
      2) flatten_inline_text：把只含 text 的多模态 content 折叠成字符串（否则前缀不匹配）
      3) warm_up + 标题请求后补预热：引擎就绪后重建共享前缀
    要彻底关掉：在这里 return 即可。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from warmup_proxy import WarmupProxy
    except Exception:
        log("warmup proxy: 模块不可用，跳过\n" + traceback.format_exc())
        return
    try:
        p = WarmupProxy(PROJECT, listen=PROXY_LISTEN, upstream=ENGINE_ADDR, log=log)
        if p.start():
            PROXY[0] = p
    except Exception:
        log("warmup proxy: 启动异常\n" + traceback.format_exc())


def warm_up_async():
    """引擎就绪后回放一次样本；后台线程，绝不阻塞托盘主流程。"""
    p = PROXY[0]
    if not p:
        return
    threading.Thread(target=p.warm_up, name="warmup", daemon=True).start()


def on_proxy_info(icon, item=None):
    """菜单项：说明代理状态 —— 客户端要指到 8093 才会自动预热。"""
    p = PROXY[0]
    if not p or not p.listening:
        text = "预热代理未启动（仍可直连 8094，只是不会自动预热）。"
    else:
        s = p.load_sample()
        warm = p.stats.get("warm_seconds")
        text = ("监听 127.0.0.1:%d -> 引擎 %d\n"
                "把客户端模型地址指向 8093 即可自动预热第一句。\n\n"
                "已见过请求 %d 次\n稳定前导样本：%s\n上次预热：%s"
                % (PROXY_LISTEN[1], ENGINE_ADDR[1], p.stats["requests_seen"],
                   ("已记录 %.0f KiB" % (p.stats["sample_bytes"] / 1024.0)) if s else "尚未记录",
                   ("%.1f 秒" % warm) if warm else "未执行"))
    try:
        icon.notify(text, TITLE)
    except Exception:
        pass


def bootstrap(icon):
    """后台线程：确保引擎在跑、把它的窗口藏好，并跟踪状态。"""
    try:
        if engine_ready():
            hide_engine_console()
            set_state(icon, "ready", "已就绪",
                      "引擎已在运行（左键点图标看日志窗口）")
            start_session_log()                     # 复用已跑的引擎也要记账
            warm_up_async()
            return

        if not engine_process_running():
            launch_engine()
            start_session_log()

        t0 = time.time()
        hidden = False
        seen = False
        deadline = t0 + 240
        while time.time() < deadline:
            if engine_process_running():
                seen = True
                start_session_log()                 # 进程一出现就挂上会话日志
            if not hidden:
                hidden = hide_engine_console()      # 引擎窗口一出现就藏起来
            if engine_ready():
                hide_engine_console()
                set_state(icon, "ready", "已就绪",
                          "http://127.0.0.1:8094（加载 %.0f 秒；左键点图标看日志窗口）"
                          % (time.time() - t0))
                warm_up_async()
                return
            # 启动 12 秒后进程仍不存在：
            #  - 进程压根没出现过 => 真失败（多为显存不足）
            #  - 进程起来过又被关掉 => 用户在加载中点掉了窗口的 X，按"已停止"收尾
            if time.time() - t0 > 12 and not engine_process_running():
                if seen:
                    engine_gone(icon)
                else:
                    fail_and_exit(icon, "启动失败",
                                  "引擎进程已退出；多为显存不足，详见日志")
                return
            time.sleep(1.0)
        fail_and_exit(icon, "启动超时", "240 秒内未就绪，详见日志")
    except Exception:
        log("bootstrap crashed:\n" + traceback.format_exc())
        fail_and_exit(icon, "内部错误", "详见 logs\\tray.log")


def set_state(icon, phase, text, detail=""):
    STATE.update(phase=phase, text=text, detail=detail)
    log("state -> %s (%s) %s" % (phase, text, detail))
    try:
        icon.icon = ICONS.get(phase, ICONS["ready"])
        icon.title = "%s — %s" % (TITLE, text)
    except Exception:
        pass


def fail_and_exit(icon, text, detail, hold=20):
    """启动失败：先留一会儿红图标，然后自动退出。

    为什么要自动退出（2026-10-12 踩到的坑）：托盘宿主是**单实例**的（互斥体
    "NInferTrayHost"）。失败后如果一直挂着，用户关掉占显存的程序、再双击桌面
    快捷方式时，新宿主只会往 tray.log 写一行 "another tray host is already
    running" 就静默退出 —— 用户看到的是"双击没反应"，比红色图标误导得多。
    退出之后，下一次双击就一定能重新拉起引擎。
    """
    set_state(icon, "failed", text, detail)
    notify("%s。托盘图标 %d 秒后自行退出，之后可再次双击桌面图标重试。" % (text, hold))
    # 启动失败也要落一份日志 —— "起不来的那几次"同样需要事后分析
    finish_session_log(abnormal=True, note="启动失败：%s / %s" % (text, detail))
    if STOP.wait(hold):
        return
    log("failed state held %d s -> tray host exits" % hold)
    try:
        icon.stop()
    except Exception:
        pass


def on_quit(icon, item=None):
    STOP.set()
    if PROXY[0]:
        try:
            PROXY[0].stop()
        except Exception:
            pass
    stop_engine()
    # 用户主动退出：这是**正常结束**，和崩溃要区分开
    finish_session_log(abnormal=False, note="用户在托盘菜单选择退出")
    try:
        icon.stop()
    except Exception:
        pass


def on_status(icon, item=None):
    """点击状态项：弹一个气泡提示。"""
    try:
        icon.notify(STATE["detail"] or STATE["text"], TITLE)
    except Exception:
        pass


def pin_icon_identity():
    """把托盘图标的「身份」固定下来。

    pystray 默认用 id(self)（内存地址）拼窗口类名和图标 ID —— 每次启动都不同，
    Windows 11 会把图标当成全新图标、一律塞进「隐藏的图标」面板，而且你在
    「设置 → 个性化 → 任务栏 → 其他系统托盘图标」里打开它也留不住。
    固定类名与 ID 之后，这个开关就能一次生效、长期有效。
    """
    try:
        import ctypes
        import pystray._win32 as _w

        def _register_class(self):
            return _w.win32.RegisterClassEx(_w.win32.WNDCLASSEX(
                cbSize=ctypes.sizeof(_w.win32.WNDCLASSEX),
                style=0,
                lpfnWndProc=_w._dispatcher,
                cbClsExtra=0,
                cbWndExtra=0,
                hInstance=_w.win32.GetModuleHandle(None),
                hIcon=None,
                hCursor=None,
                hbrBackground=_w.win32.COLOR_WINDOW + 1,
                lpszMenuName=None,
                lpszClassName="NInferTrayHostWnd",
                hIconSm=None))

        def _message(self, code, flags, **kwargs):
            _w.win32.Shell_NotifyIcon(code, _w.win32.NOTIFYICONDATAW(
                cbSize=ctypes.sizeof(_w.win32.NOTIFYICONDATAW),
                hWnd=self._hwnd, hID=1, uFlags=flags, **kwargs))

        # 左键单击（含双击的第一下）= 呼出引擎窗口；右键仍然弹菜单。
        # pystray 默认把左键也绑成"弹菜单"，这里替换掉。
        _orig_notify = _w.Icon._on_notify

        def _on_notify(self, wparam, lparam):
            if lparam == _w.win32.WM_LBUTTONUP:
                on_show_console()
                return 0
            return _orig_notify(self, wparam, lparam)

        _w.Icon._register_class = _register_class
        _w.Icon._message = _message
        _w.Icon._on_notify = _on_notify
        return True
    except Exception:
        log("identity pin failed:\n" + traceback.format_exc())
        return False


def main():
    import pystray
    from pystray import Menu, MenuItem as Item

    pin_icon_identity()
    start_warmup_proxy()

    for k, c in COLORS.items():
        ICONS[k] = make_icon(c)          # 兜底：文件读不到时至少有个能看的图标
    ICONS.update(load_tray_icons())

    menu = Menu(
        Item(lambda item: "状态：%s" % STATE["text"], on_status),
        Menu.SEPARATOR,
        Item("显示引擎窗口（日志）", on_show_console),
        Item("隐藏引擎窗口", lambda icon, item: hide_engine_console()),
        Menu.SEPARATOR,
        Item("打开项目文件夹", lambda icon, item: open_path(PROJECT)),
        Item("打开接口页 (127.0.0.1:8094)", lambda icon, item: open_path(URL)),
        Item(lambda item: "预热代理：%s" % (
                 "监听 8093 ✓" if (PROXY[0] and PROXY[0].listening) else "未启动"),
             on_proxy_info),
        Menu.SEPARATOR,
        Item("停止引擎并退出", on_quit),
    )

    icon = pystray.Icon("ninfer-tray", ICONS["starting"],
                        "%s — 启动中…" % TITLE, menu)
    CURRENT_ICON[0] = icon

    def setup(ic):
        # 关键：pystray 在传入自定义 setup 回调时不会自动把图标置为可见
        # （见 pystray 文档：must explicitly set this attribute）。少了这一步，
        # Shell_NotifyIcon(NIM_ADD) 永远不会被调用 —— 引擎跑起来了，托盘里却什么都没有。
        try:
            ic.visible = True
        except Exception:
            log("failed to make icon visible:\n" + traceback.format_exc())
        threading.Thread(target=bootstrap, args=(ic,), daemon=True).start()
        threading.Thread(target=watchdog, args=(ic,), daemon=True).start()

    log("tray host up (pid=%d)" % os.getpid())
    icon.run(setup=setup)


if __name__ == "__main__":
    try:
        # 单实例：已有托盘宿主在跑就不再起第二个
        # 注意用 Local（无前缀）命名空间 —— "Global\" 需要 SeCreateGlobalPrivilege，
        # 普通交互用户没有，CreateMutexW 会直接失败，单实例检测就废了。
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, "NInferTrayHost")
        if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
            log("another tray host is already running; exiting")
            # 以前这里只写日志就退出 —— 用户看到的是"双击没反应"。现在明确告诉他去哪看。
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "%s 的托盘图标已经在运行了。\n\n"
                    "请看屏幕右下角（可能被折叠进「隐藏的图标」小面板里）——"
                    "左键点它可呼出引擎日志窗口，右键是菜单。\n\n"
                    "如果右下角找不到图标，说明上次是异常退出：请在任务管理器里结束"
                    " pythonw.exe，然后重新双击桌面图标。" % APPNAME,
                    APPNAME, 0x40)          # MB_ICONINFORMATION
            except Exception:
                pass
            sys.exit(0)
        main()
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        sys.exit(1)
