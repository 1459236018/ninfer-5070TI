"""实测各 --kv-dtype 的显存占用与速度（同池、同配置，只换 KV 精度）。

引擎 help 列的取值：bf16 (default), int8, fp8, rk8v4, rk4v4, rk4v4-e8, rk2v4-e8, nvfp4, k8v4
"""
import subprocess, sys, time, os, json, re, urllib.request

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
def _find_workdir(root):
    """诊断输出目录：优先 .workbuddy/bench（本机布局），否则回退 logs/。都会确保存在。"""
    for _rel in ((".workbuddy", "bench"), ("logs",)):
        _p = os.path.join(root, *_rel)
        if os.path.isdir(_p):
            return _p
    _p = os.path.join(root, "logs")
    os.makedirs(_p, exist_ok=True)
    return _p


BENCH = _find_workdir(ROOT)
EXE = os.path.join(ROOT, "engine", "ninfer-serve-120a.exe")
import batcfg  # noqa: E402  同目录助手：模型与端口一律以启动器为准

MODEL = batcfg.resolve_model()
ENGINE_PORT = batcfg.engine_port()
BASE = "http://127.0.0.1:%d" % ENGINE_PORT

POOL = int(os.environ.get("POOL", "32768"))
MC = int(os.environ.get("MC", str(POOL)))
DTYPES = [d for d in os.environ.get(
    "DTYPES", "rk4v4,bf16,fp8,nvfp4,rk8v4,rk2v4-e8").split(",") if d]


def vram():
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free",
                        "--format=csv,noheader,nounits"], capture_output=True, text=True)
    u, f = [int(x.strip()) for x in r.stdout.strip().split(",")]
    return u, f


def kill_all():
    subprocess.run(["taskkill", "/F", "/IM", "ninfer-serve-120a.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)


def post(path, payload, timeout=600):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def run(dtype):
    log = os.path.join(BENCH, "dtype-%s.log" % dtype)
    args = [EXE, MODEL, "--host", "127.0.0.1", "--port", str(ENGINE_PORT), "--model-id", "qwen3.8-27b",
            "--max-context", str(MC), "--kv-capacity", str(POOL), "--kv-dtype", dtype,
            "--cuda-graph-allowance-mib", "128", "--host-kv-mib", "4096", "--prefill-chunk", "512",
            "--spec", "mtp", "--draft-tokens", "4",
            "--vision", "--vision-residency", "overlay", "--media-cache-mib", "0",
            "--default-max-tokens", "32768", "--max-concurrency", "1",
            "--presence-penalty", "0", "--temperature", "0.7", "--top-p", "0.9", "--top-k", "20"]
    env = dict(os.environ)
    env["PATH"] = os.path.join(ROOT, "engine") + os.pathsep + env.get("PATH", "")
    env["NINFER_KV_WINDOW"] = "16384"
    env["NINFER_KV_RETRIEVE"] = "8192"
    env["NINFER_KV_RING"] = "1"
    env["NINFER_HOST_PAGEABLE"] = "1"
    env["NINFER_KV_REUSE_HOSTBACKED"] = "0"
    env["CUBLAS_WORKSPACE_CONFIG"] = ":16:8"

    f = open(log, "wb")
    p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    t0 = time.time()
    ok = False
    while time.time() - t0 < 150:
        if p.poll() is not None:
            break
        try:
            if urllib.request.urlopen(BASE + "/v1/models", timeout=1).status == 200:
                ok = True
                break
        except Exception:
            pass
        time.sleep(1)
    res = {"dtype": dtype, "ok": ok, "load_s": round(time.time() - t0, 1)}

    if ok:
        time.sleep(4)
        u, fr = vram()
        res["used"] = u
        res["free"] = fr
        try:
            t = time.time()
            r = post("/v1/chat/completions", {
                "model": "qwen3.8-27b",
                "messages": [{"role": "user", "content": "从 1 数到 200，用逗号分隔，不要解释。"}],
                "max_tokens": 128, "temperature": 0, "reasoning_effort": "none"})
            dt = time.time() - t
            out = (r.get("usage") or {}).get("completion_tokens", 0)
            res["decode_tok_s"] = round(out / dt, 1) if dt > 0 else None
        except Exception as e:
            res["decode_err"] = str(e)[:80]

        try:
            filler = "The quick brown fox jumps over the lazy dog. "
            long_text = "读完后只回复 OK。\n" + filler * 2400 + "\n只回复 OK。"
            t = time.time()
            r = post("/v1/chat/completions", {
                "model": "qwen3.8-27b",
                "messages": [{"role": "user", "content": long_text}],
                "max_tokens": 4, "temperature": 0, "reasoning_effort": "none"})
            dt = time.time() - t
            ptok = (r.get("usage") or {}).get("prompt_tokens", 0)
            res["prompt_tokens"] = ptok
            res["prefill_tok_s"] = round(ptok / dt, 0) if dt > 0 else None
        except Exception as e:
            res["prefill_err"] = str(e)[:80]

    f.flush()
    try:
        txt = open(log, "rb").read().decode("utf-8", "replace")
    except Exception:
        txt = ""
    m = re.search(r"capacity \| KV ([\d,]+) tokens, (\S+)[^\n]*?runtime ([\d.]+) GiB \| free ([\d.]+) MiB", txt)
    if m:
        res["kv_runtime_gib"] = float(m.group(3))
        res["log_free_mib"] = float(m.group(4))
    errs = [l for l in txt.splitlines() if l.startswith("ninfer-serve:")][:1]
    if errs:
        res["error"] = errs[0][:120]
    f.close()
    kill_all()
    return res


if __name__ == "__main__":
    kill_all()
    vram()
    out = []
    for d in DTYPES:
        print("--- %s ---" % d)
        sys.stdout.flush()
        r = run(d)
        out.append(r)
        print("   ", json.dumps(r, ensure_ascii=False))
        sys.stdout.flush()

    print()
    print("=" * 100)
    print("%-10s %-6s %-9s %-9s %-11s %-11s %s" % ("dtype", "ok", "KV GiB", "used MiB", "decode t/s", "prefill t/s", "note"))
    for r in out:
        print("%-10s %-6s %-9s %-9s %-11s %-11s %s" % (
            r["dtype"], r["ok"],
            r.get("kv_runtime_gib", "-"),
            r.get("used", "-"),
            r.get("decode_tok_s", "-"),
            r.get("prefill_tok_s", "-"),
            r.get("error", "")))
    print("DONE")
