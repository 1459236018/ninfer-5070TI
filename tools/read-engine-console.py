"""读取正在运行的引擎控制台缓冲区（不重启、不干扰），拿 req#N done 行。

引擎把逐请求日志写在它自己的控制台里（bat 用 start /min 起的窗口），
托盘把窗口 SW_HIDE 了但缓冲区还在，可以用 AttachConsole + ReadConsoleOutputCharacterW 读出来。
"""
import ctypes, sys, re
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
OPEN_EXISTING = 3


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


def read_console(pid, lines=None):
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        return None, "AttachConsole 失败 err=%d" % ctypes.get_last_error()
    try:
        h = k32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE,
                            FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                            OPEN_EXISTING, 0, None)
        if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
            return None, "CreateFileW(CONOUT$) 失败 err=%d" % ctypes.get_last_error()
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not k32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
            return None, "GetConsoleScreenBufferInfo 失败"
        w = info.dwSize.X
        total = info.dwSize.Y
        cursor = info.dwCursorPosition.Y
        out = []
        CHUNK = 500
        for start in range(0, total, CHUNK):
            n = min(CHUNK, total - start)
            buf = ctypes.create_unicode_buffer(w * n + 1)
            read = wintypes.DWORD(0)
            ok = k32.ReadConsoleOutputCharacterW(h, buf, w * n, COORD(0, start), ctypes.byref(read))
            if not ok:
                break
            text = buf[:read.value]
            out.extend(text[i:i + w].rstrip() for i in range(0, len(text), w))
        k32.CloseHandle(h)
        return out, "buf %dx%d, cursor.Y=%d, 读到 %d 行" % (w, total, cursor, len(out))
    finally:
        k32.FreeConsole()


if __name__ == "__main__":
    pid = int(sys.argv[1])
    rows, msg = read_console(pid)
    print("状态:", msg)
    if rows is None:
        sys.exit(1)
    raw = "".join(rows)
    items = re.split(r"(?=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}\s+INFO)", raw)
    keep = [s.strip() for s in items
            if re.search(r"req#\d+ (done|started)|capacity \||context cache \||throughput \|", s)]
    print("=== 请求/capacity 相关（已拼接折行）===")
    for s in keep:
        s = re.sub(r"\s+", " ", s)
        print("  ", s[:400])
