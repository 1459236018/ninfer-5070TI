"""测「池能开多大」：逐个变体启动引擎，只报能否就绪 + capacity 行，不做请求。

变体由 KV_CAP / MC / EXTRA 环境变量控制。
"""
import subprocess, os, sys, time, json, re, urllib.request
import batcfg  # 同目录助手：模型与端口一律以启动器为准

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
# 2026-10-16：模型与端口不再硬编码。引擎让到 8095、预热代理占住 8094，
# 于是"打 8094 看有没有 200"会被代理冒充成"引擎已就绪"。
MODEL = batcfg.resolve_model()
ENGINE_PORT = batcfg.engine_port()
BASE = "http://127.0.0.1:%d" % ENGINE_PORT

BASE_ARGS = [
    "--host", "127.0.0.1", "--port", str(ENGINE_PORT), "--model-id", "qwen3.8-27b",
    "--kv-dtype", "nvfp4", "--cuda-graph-allowance-mib", "128",
    "--host-kv-mib", "4096", "--prefill-chunk", "512",
    "--spec", "mtp", "--draft-tokens", "4",
    "--vision", "--vision-residency", "overlay", "--media-cache-mib", "0",
    "--default-max-tokens", "32768", "--default-reasoning-effort", "high",
    "--default-thinking-budget", "2048",
    "--thinking-budget-message", "Thinking budget reached. Stop deliberating and write your final answer now.",
    "--max-concurrency", "1", "--max-shared-prefixes", "7", "--derive-session-keys",
    "--presence-penalty", "0", "--temperature", "0.7", "--top-p", "0.9", "--top-k", "20",
]


def kill():
    subprocess.run(["taskkill", "/F", "/IM", "ninfer-serve-120a.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)


def try_boot(tag, kv_cap, extra=None):
    log = os.path.join(BENCH, "poolsize-%s.log" % tag)
    args = [EXE, MODEL, "--max-context", str(kv_cap), "--kv-capacity", str(kv_cap)] + BASE_ARGS + (extra or [])
    env = dict(os.environ)
    env["PATH"] = os.path.join(ROOT, "engine") + os.pathsep + env.get("PATH", "")
    env.update({"NINFER_KV_WINDOW": "16384", "NINFER_KV_RETRIEVE": "8192",
                "NINFER_KV_RING": "1", "NINFER_HOST_PAGEABLE": "1",
                "NINFER_KV_REUSE_HOSTBACKED": "0", "CUBLAS_WORKSPACE_CONFIG": ":16:8"})
    kill()
    f = open(log, "wb")
    p = subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
    t0 = time.time()
    ok = False
    while time.time() - t0 < 100:
        if p.poll() is not None:
            break
        try:
            if urllib.request.urlopen(BASE + "/v1/models", timeout=1).status == 200:
                ok = True
                break
        except Exception:
            pass
        time.sleep(1)
    time.sleep(1)
    f.flush(); f.close()
    txt = open(log, "rb").read().decode("utf-8", "replace")
    cap = re.search(r"capacity \| KV[^\n]*", txt)
    err = [l for l in txt.splitlines() if re.search(r"ERROR|FATAL|outside the usable|requires", l)][:1]
    kill()
    return {"tag": tag, "pool": kv_cap, "ok": ok, "ready_s": round(time.time() - t0, 1),
            "capacity": cap.group(0).strip() if cap else None,
            "err": err[0][:170] if err else None, "extra": " ".join(extra or [])}


if __name__ == "__main__":
    combos = []
    for spec in sys.argv[1:]:
        # 格式: pool[:extra参数,逗号分隔]
        pool, _, ex = spec.partition(":")
        try:
            combos.append((spec, int(pool), [x for x in ex.split(",") if x]))
        except ValueError:
            print("!! 池容量必须是整数，收到: %r" % pool, file=sys.stderr)
            sys.exit(2)
    if not combos:
        # 曾经这里什么都不做就 exit 0 —— 静默成功，比报错更坑。
        print("用法: python probe-poolsize.py <池容量>[:额外参数,逗号分隔] ...")
        print()
        print("  例: python probe-poolsize.py 98304")
        print("      python probe-poolsize.py 98304 114688:--gdn-state-fp16")
        print()
        print("每个取值都会真实启动一次引擎来验证是否可用（约 10 秒/次）。")
        sys.exit(2)
    rows = []
    for tag, pool, extra in combos:
        print("--- %s (pool=%d %s)" % (tag, pool, extra)); sys.stdout.flush()
        r = try_boot(tag.replace(":", "_"), pool, extra)
        rows.append(r)
        print("    ok=%s ready=%.1fs  %s" % (r["ok"], r["ready_s"], r["capacity"] or r["err"]))
        sys.stdout.flush()

    failed = [r for r in rows if not r["ok"]]
    if failed:
        print("\n%d/%d 个取值启动失败" % (len(failed), len(rows)), file=sys.stderr)
        sys.exit(1)
