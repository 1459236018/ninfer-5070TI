"""实测：上下文长度 / max-context / kv-capacity 分别对显存的影响。

实验 A：同一池（81920），先短请求后长请求 —— 验证"对话变长"是否额外吃显存。
实验 B：同池（81920），只把 --max-context 从 131072 降到 81920 —— 验证 max-context 本身是否吃显存。
"""
import subprocess, sys, time, os, json, urllib.request

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
EXE = os.path.join(ROOT, "engine", "ninfer-serve-120a.exe")
import batcfg  # noqa: E402  同目录助手：模型与端口一律以启动器为准

MODEL = batcfg.resolve_model()
ENGINE_PORT = batcfg.engine_port()
BASE = "http://127.0.0.1:%d" % ENGINE_PORT
def _find_workdir(root):
    """诊断输出目录：优先 .workbuddy/bench（本机布局），否则回退 logs/。都会确保存在。"""
    for _rel in ((".workbuddy", "bench"), ("logs",)):
        _p = os.path.join(root, *_rel)
        if os.path.isdir(_p):
            return _p
    _p = os.path.join(root, "logs")
    os.makedirs(_p, exist_ok=True)
    return _p


LOG = os.path.join(_find_workdir(ROOT), "probe-vram.log")
DTYPE = os.environ.get("KV_DTYPE", "rk4v4")


def vram(tag):
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True)
    used, free = [int(x.strip()) for x in r.stdout.strip().split(",")]
    print("    [%-12s] used=%5d MiB  free=%5d MiB" % (tag, used, free))
    sys.stdout.flush()
    return used, free


def kill_all():
    subprocess.run(["taskkill", "/F", "/IM", "ninfer-serve-120a.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)


def boot(max_ctx, kv_cap):
    args = [EXE, MODEL, "--host", "127.0.0.1", "--port", str(ENGINE_PORT), "--model-id", "qwen3.8-27b",
            "--max-context", str(max_ctx), "--kv-capacity", str(kv_cap), "--kv-dtype", DTYPE,
            "--cuda-graph-allowance-mib", "128", "--host-kv-mib", "4096", "--prefill-chunk", "512",
            "--spec", "mtp", "--draft-tokens", "4",
            "--vision", "--vision-residency", "overlay", "--media-cache-mib", "0",
            "--default-max-tokens", "32768", "--max-concurrency", "1",
            "--max-shared-prefixes", "7", "--derive-session-keys",
            "--presence-penalty", "0", "--temperature", "0.7", "--top-p", "0.9", "--top-k", "20"]
    env = dict(os.environ)
    env["PATH"] = os.path.join(ROOT, "engine") + os.pathsep + env.get("PATH", "")
    env["NINFER_KV_WINDOW"] = "16384"
    env["NINFER_KV_RETRIEVE"] = "8192"
    env["NINFER_KV_RING"] = "1"
    env["NINFER_HOST_PAGEABLE"] = "1"
    env["NINFER_KV_REUSE_HOSTBACKED"] = "0"
    env["CUBLAS_WORKSPACE_CONFIG"] = ":16:8"
    f = open(LOG, "wb")
    p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    t0 = time.time()
    ok = False
    while time.time() - t0 < 180:
        if p.poll() is not None:
            break
        try:
            if urllib.request.urlopen(BASE + "/v1/models", timeout=1).status == 200:
                ok = True
                break
        except Exception:
            pass
        time.sleep(1)
    print("    ready=%s  %.1fs" % (ok, time.time() - t0))
    return p, f, ok


def ask(tag, text, mt=16):
    body = json.dumps({"model": "qwen3.8-27b",
                       "messages": [{"role": "user", "content": text}],
                       "max_tokens": mt, "temperature": 0,
                       "reasoning_effort": "none"}).encode()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    r = json.loads(urllib.request.urlopen(req, timeout=900).read())
    u = r.get("usage", {})
    det = u.get("prompt_tokens_details", {}) or {}
    print("    [%-12s] prompt=%6s cached=%6s out=%4s  %.1fs"
          % (tag, u.get("prompt_tokens"), det.get("cached_tokens", 0),
             u.get("completion_tokens"), time.time() - t))
    sys.stdout.flush()
    return u.get("prompt_tokens") or 0


def stop(p, f):
    f.flush()
    f.close()
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(15)
        except Exception:
            p.kill()
    kill_all()


if __name__ == "__main__":
    kill_all()
    u0 = vram("idle-before")

    print("== 实验 A：池 81920，短 -> 长 prompt ==")
    p, f, ok = boot(131072, 81920)
    if not ok:
        print("  启动失败，见", LOG)
        stop(p, f)
        sys.exit(1)
    time.sleep(6)
    a_boot = vram("A-boot")

    n1 = ask("short", "用一句话说明什么是 KV cache。")
    time.sleep(3)
    a_short = vram("A-after-short")

    filler = "The quick brown fox jumps over the lazy dog. "
    long_text = "以下是一段背景资料，读完后只回复 OK。\n" + filler * 2400 + "\n只回复 OK。"
    print("    长 prompt 字符数 =", len(long_text))
    n2 = ask("long", long_text)
    time.sleep(4)
    a_long = vram("A-after-long")
    stop(p, f)

    print("== 实验 B：同池 81920，仅把 --max-context 降到 81920 ==")
    p, f, ok = boot(81920, 81920)
    if not ok:
        print("  启动失败，见", LOG)
        stop(p, f)
        sys.exit(1)
    time.sleep(6)
    b_boot = vram("B-boot")
    stop(p, f)

    print()
    print("=" * 68)
    print("结论数据")
    print("  实验 A: boot=%d  short(prompt %s tok)=%d  long(prompt %s tok)=%d"
          % (a_boot[0], n1, a_short[0], n2, a_long[0]))
    print("        会话从 %s tok 涨到 %s tok，整卡显存变化 = %+d MiB"
          % (n1, n2, a_long[0] - a_short[0]))
    print("  实验 B: max-context 81920 的 boot=%d， 与 A(max-context 131072) 的 boot=%d 相差 %+d MiB"
          % (b_boot[0], a_boot[0], b_boot[0] - a_boot[0]))
    print("DONE")
