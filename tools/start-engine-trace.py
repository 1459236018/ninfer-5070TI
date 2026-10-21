"""诊断用：启动引擎并开启请求记录（--request-log-jsonl），用于对比连续两轮的真实 prompt。

引擎以独立进程启动（不依赖本脚本存活），stdout 进 logs/engine-trace.log，
每个请求的完整记录进 logs/requests.jsonl。
"""
import subprocess, os, sys, time, json, re, urllib.request

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
LOGS = os.path.join(ROOT, "logs")
EXE = os.path.join(ROOT, "engine", "ninfer-serve-120a.exe")
import batcfg  # noqa: E402  同目录助手：模型与端口一律以启动器为准

MODEL = batcfg.resolve_model() or os.path.join(ROOT, "model.ninfer")
ENGINE_PORT = batcfg.engine_port()
BASE = "http://127.0.0.1:%d" % ENGINE_PORT
OUT = os.path.join(LOGS, "engine-trace.log")
REQ = os.path.join(LOGS, "requests.jsonl")

def _load_bat_args():
    """从生产启动器读参数——那是唯一真相源。

    这里曾经写死过 --max-context 131072 / --kv-capacity 81920，
    结果启动器早已调到 106496 而本脚本还在报 81920，诊断时直接误导人。
    现在一律跟随启动器，只额外追加请求记录参数。
    """
    bat = os.path.join(ROOT, "start-mtp-slim.bat")
    if not os.path.exists(bat):
        return None, None
    txt = open(bat, encoding="ascii", errors="replace").read()
    m = re.search(r'set "ENGINE_ARGS=(.*)"', txt)
    if not m:
        return None, None
    args, cur, q = [], "", None
    for ch in m.group(1):
        if q:
            if ch == q:
                q = None
            else:
                cur += ch
        elif ch in "\"'":
            q = ch
        elif ch.isspace():
            if cur:
                args.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        args.append(cur)
    mm = re.search(r'set "MODEL=([^"]+)"', txt)
    model = mm.group(1).replace("%ROOT%", ROOT + os.sep).replace("/", os.sep) if mm else None
    # 先把占位符展开（启动器里模型写的是 "%MODEL%"），再判断第一项是不是模型，
    # 否则 "%MODEL%" 会被当成一个陌生参数传给引擎（引擎会打印用法并退出）。
    if model:
        args = [a.replace("%MODEL%", model).replace("%ROOT%", ROOT + os.sep) for a in args]
    # 启动器参数表的第一项是模型路径；本脚本以 [EXE, MODEL] + ARGS 启动，要去掉它避免重复。
    if args and args[0].lower().endswith(".ninfer"):
        args = args[1:]
    return args, model


_BAT_ARGS, _BAT_MODEL = _load_bat_args()
ARGS = list(_BAT_ARGS) if _BAT_ARGS else [
    "--host", "127.0.0.1", "--port", str(ENGINE_PORT), "--model-id", "qwen3.8-27b",
    "--kv-dtype", "nvfp4", "--gdn-state-fp16",
    "--cuda-graph-allowance-mib", "128", "--host-kv-mib", "4096",
    "--max-concurrency", "1", "--max-shared-prefixes", "7", "--derive-session-keys",
    "--vision", "--vision-residency", "overlay",
]
if "--request-log-jsonl" not in ARGS:
    ARGS += ["--request-log-jsonl", REQ, "--request-log-max-mib", "256"]
if _BAT_MODEL:
    MODEL = _BAT_MODEL

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

if __name__ == "__main__":
    subprocess.run(["taskkill", "/F", "/IM", "ninfer-serve-120a.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    os.makedirs(LOGS, exist_ok=True)
    if os.path.exists(REQ):
        os.remove(REQ)

    env = dict(os.environ)
    env["PATH"] = os.path.join(ROOT, "engine") + os.pathsep + env.get("PATH", "")
    env["NINFER_KV_WINDOW"] = "16384"
    env["NINFER_KV_RETRIEVE"] = "8192"
    env["NINFER_KV_RING"] = "1"
    env["NINFER_HOST_PAGEABLE"] = "1"
    env["NINFER_KV_REUSE_HOSTBACKED"] = "0"
    env["CUBLAS_WORKSPACE_CONFIG"] = ":16:8"

    f = open(OUT, "ab")
    p = subprocess.Popen([EXE, MODEL] + ARGS, stdout=f, stderr=subprocess.STDOUT,
                         env=env, cwd=ROOT,
                         creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    print("引擎已启动 pid=%d" % p.pid)
    t0 = time.time()
    while time.time() - t0 < 180:
        if p.poll() is not None:
            print("启动失败，见", OUT)
            sys.exit(1)
        try:
            if urllib.request.urlopen(BASE + "/v1/models", timeout=1).status == 200:
                print("就绪 %.1fs，请求记录 → %s" % (time.time() - t0, REQ))
                sys.exit(0)
        except Exception:
            pass
        time.sleep(1)
    print("180s 未就绪，见", OUT)
    sys.exit(1)
