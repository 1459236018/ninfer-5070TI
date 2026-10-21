"""按用户显卡自动定容：探测 -> 公式估算 -> 二分实测 -> 写配置。

关键设计：**参数一律从 start-mtp-slim.bat 里读**（那是唯一真相源）。
自己拼参数集很容易漏项 —— 例如漏掉 `--vision` 会让测出的上限虚高约 600 MiB，
写回启动器后就起不来了。

用法:
  python autoconfig.py --report            只看显存账本与公式估算，不启动引擎
  python autoconfig.py --probe             二分实测池上限（约 1 分钟，会反复启停引擎）
  python autoconfig.py --probe --apply     实测后把结果写进 start-mtp-slim.bat

退出码: 0 成功; 2 找不到启动器; 3 引擎始终起不来。
"""
import argparse, json, os, re, subprocess, sys, time, urllib.request
import batcfg  # 同目录助手：端口/模型一律以启动器为准

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = batcfg.ROOT
EXE = os.path.join(ROOT, "engine", "ninfer-serve-120a.exe")
BAT = batcfg.bat_path()
# 2026-10-16 修复：本机把引擎让到 8095、预热代理占住 8094（客户端打 8094）。
# 以前这里写死 8094 —— 代理会替"根本没起来的引擎"回 200，于是二分一路顶到
# MAX_POOL，--apply 把必炸的池值写回启动器。就绪判断必须打【引擎端口】。
ENGINE_PORT = batcfg.engine_port()
BASE_URL = "http://127.0.0.1:%d" % ENGINE_PORT
STEP = 8192
MIN_POOL, MAX_POOL = 8192, 262144
SAFETY_MIB = 300

FIXED_GIB = 1.05          # KV runtime 固定开销（nvfp4 实测拟合）
PER_TOKEN_KIB = 18.8      # 每 token 的 KV 开销
VISION_MIB = 615          # --vision 净占显存（实测：开/关的上限差 32768 token）

DEFAULT_ARGS = [          # 启动器缺失时的兜底（应尽快修好启动器）
    "--host", "127.0.0.1", "--port", str(ENGINE_PORT), "--model-id", "qwen3.8-27b",
    "--kv-dtype", "nvfp4", "--gdn-state-fp16",
    # cg 192 与生产档保持一致：cg32 时引擎自报 CUDA graph 实际用了 42~207 MiB，
    # 即那笔账是假的（占用从"余量"里偷偷扣）。
    "--cuda-graph-allowance-mib", "192", "--host-kv-mib", "4096",
    "--prefill-chunk", "512", "--max-concurrency", "1",
    "--default-max-tokens", "32768",
    "--max-shared-prefixes", "7", "--derive-session-keys",
]


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="gbk", errors="replace", timeout=60, **kw)


def split_args(s):
    """按空格切分，但保留引号内的内容（bat 参数里有带空格的字符串）。"""
    out, cur, q = [], "", None
    for ch in s:
        if q:
            if ch == q:
                q = None
            else:
                cur += ch
        elif ch in "\"'":
            q = ch
        elif ch.isspace():
            if cur:
                out.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def load_bat():
    """从启动器读出真实参数。返回 (args, model_path, kv_dtype, has_vision)。

    实现与 batcfg.load_bat() 等价（fallback 链同样按"第一个存在的候选"解析）；
    新代码请直接用 batcfg，这里保留是为了不动既有调用点。
    """
    return batcfg.load_bat()


def gpu_info():
    r = sh(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
            "--format=csv,noheader,nounits"])
    lines = (r.stdout or "").strip().splitlines()
    if not lines:
        print("!! 读不到 nvidia-smi，这台机器可能没有 NVIDIA 显卡"); sys.exit(1)
    name, total, used, free = [x.strip() for x in lines[0].split(",")]
    return {"name": name, "total": int(total), "used": int(used), "free": int(free)}


def estimate(resident_gib, gpu, has_vision):
    extra = VISION_MIB if has_vision else 0
    avail = gpu["total"] - gpu["used"] - int(resident_gib * 1024) - 250 - SAFETY_MIB - extra
    pool = int((avail / 1024.0 - FIXED_GIB) * 1024 * 1024 / PER_TOKEN_KIB)
    return avail, max(MIN_POOL, min(MAX_POOL, pool // STEP * STEP))


def kill_engine():
    sh(["taskkill", "/F", "/IM", "ninfer-serve-120a.exe"])
    time.sleep(3)


def _read_text(path):
    try:
        return open(path, "rb").read().decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_count():
    """当前有几个引擎进程（tasklist 在本机可用；wmic 已被 Win11 移除）。"""
    r = sh(["tasklist", "/FI", "IMAGENAME eq ninfer-serve-120a.exe", "/FO", "CSV", "/NH"])
    return (r.stdout or "").lower().count("ninfer-serve-120a.exe")


def engine_ready(proc=None, log=None, timeout=1):
    """真·就绪 = 本次启动的进程还活着 + 引擎端口回了非空模型清单 + 本次日志写了 engine ready。

    为什么不能只看端口（2026-10-16 实测踩到）：只打端口时，任何在本轮启动之前
    还活着的引擎（杀不干净、或上一轮刚起来）都会替"根本没起来的新进程"回答 200，
    于是二分搜索一路顶到 MAX_POOL，把 262144 报成"可启动"——而引擎自己对这个池
    的判决是 `requires 6062970112 bytes, but only 5315231744 available`（FATAL）。
    日志文件每轮都以 "wb" 重新截断，所以日志里出现 engine ready 必然来自本轮。
    """
    if proc is not None and proc.poll() is not None:
        return False
    try:
        with urllib.request.urlopen(BASE_URL + "/v1/models", timeout=timeout) as r:
            if r.status != 200:
                return False
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return False
    ids = [m.get("id") for m in (data.get("data") or []) if isinstance(m, dict)]
    if not any(ids):
        return False
    if log is None:
        return True          # 仅"现役引擎在服务吗"这类查询用得到
    return "engine ready" in _read_text(log)


RESIDENT_CACHE = os.path.join(HERE, "_autoconfig-resident.json")


def cached_resident(model):
    try:
        with open(RESIDENT_CACHE, encoding="utf-8") as f:
            rec = json.load(f).get(os.path.basename(model))
        return float(rec["gib"]) if rec else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def remember_resident(model, gib):
    if not model:
        return
    data = {}
    try:
        with open(RESIDENT_CACHE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        pass
    data[os.path.basename(model)] = {"gib": round(gib, 2), "ts": int(time.time())}
    try:
        with open(RESIDENT_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def resident_estimate(weight, model):
    """常驻权重的估计值 + 它的来源（实测优先）。

    旧公式 weight*0.807 是按 13.99 GiB 的 IQ3_S artifact 拟合的；
    换成 10.33 GiB 的 IQ3_XXS 后引擎自报常驻 9.71 GiB（比值 0.94），
    旧公式会把"可用于 KV"少算约 3 GiB，从而把池上限估得过分保守。
    现在：① 优先用上次实测缓存；② 否则用 0.94；③ 一定把来源打出来。
    """
    c = cached_resident(model)
    if c:
        return c, "上次实测"
    return round(weight * 0.94, 1), "公式 0.94（本机 10.33 GiB 文件实测 9.71 GiB）"


def _remember_from_log(txt, args):
    mm = re.search(r"weights ready \| ([\d.]+) GiB", txt)
    if not mm:
        return
    model = args[0] if args and str(args[0]).lower().endswith(".ninfer") else ""
    remember_resident(model, float(mm.group(1)))


def _strip_request_log(args):
    """探针起的引擎不要写生产请求日志。

    两个理由：① 探针每次启停都给 logs/requests.jsonl 追加噪声，把真实现场冲淡；
    ② 日志目录不可写时（只读挂载/沙箱）打不开日志文件会让启动判定失真。
    """
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in ("--request-log-jsonl", "--request-log-max-mib"):
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def try_boot(pool, base_args, log):
    args = _strip_request_log(base_args)
    for i, a in enumerate(args):
        if a == "--max-context" and i + 1 < len(args):
            args[i + 1] = str(pool)
        elif a == "--kv-capacity" and i + 1 < len(args):
            args[i + 1] = str(pool)
    if "--max-context" not in args:
        args += ["--max-context", str(pool)]
    if "--kv-capacity" not in args:
        args += ["--kv-capacity", str(pool)]

    env = dict(os.environ)
    env["PATH"] = os.path.join(ROOT, "engine") + os.pathsep + env.get("PATH", "")
    env.update({"NINFER_KV_WINDOW": "16384", "NINFER_KV_RETRIEVE": "8192",
                "NINFER_KV_RING": "1", "NINFER_HOST_PAGEABLE": "1",
                "NINFER_KV_REUSE_HOSTBACKED": "0", "CUBLAS_WORKSPACE_CONFIG": ":16:8"})
    kill_engine()
    stale = _proc_count()
    if stale:
        return False, "旧引擎杀不干净（还有 %d 个进程），这轮结果不可信" % stale
    with open(log, "wb") as f:
        p = subprocess.Popen([EXE] + args, stdout=f, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
        t0 = time.time()
        while time.time() - t0 < 90:
            if p.poll() is not None:
                break
            try:
                if engine_ready(p, log):
                    f.flush()
                    txt = _read_text(log)
                    m = re.search(r"capacity \| KV[^\n]*", txt)
                    _remember_from_log(txt, args)
                    kill_engine()
                    return True, (m.group(0).strip() if m else None)
            except Exception:
                pass
            time.sleep(0.5)
    try:
        txt = _read_text(log)
        err = next((l.strip()[:150] for l in txt.splitlines() if "FATAL" in l or "ERROR" in l), None)
    except Exception:
        err = None
    kill_engine()
    return False, err


def probe(base_args, start, log):
    print("  试探估算值 %d ..." % start)
    ok, cap = try_boot(start, base_args, log)
    print("    %-8d %s %s" % (start, "OK  " if ok else "FAIL", (cap or "")[:100]))
    best = start if ok else 0
    lo, hi = (start, MAX_POOL) if ok else (MIN_POOL, start - STEP)
    while lo <= hi:
        mid = (lo + hi) // 2 // STEP * STEP
        if mid == best:
            break
        ok, cap = try_boot(mid, base_args, log)
        print("    %-8d %s %s" % (mid, "OK  " if ok else "FAIL", (cap or "")[:100]))
        if ok:
            best = mid
            lo = mid + STEP
        else:
            hi = mid - STEP
    return best


def apply_to_bat(pool):
    if not os.path.exists(BAT):
        print("!! 找不到", BAT); return False
    if pool < MIN_POOL or pool > MAX_POOL or pool % STEP:
        print("!! 拒绝写入明显非法的池值 %r（必须是 %d 的整数倍，且落在 %d..%d 内）"
              % (pool, STEP, MIN_POOL, MAX_POOL))
        return False
    raw = open(BAT, "rb").read()
    if any(b > 127 for b in raw):
        print("!! 启动器含非 ASCII 字节，拒绝改写（会导致 cmd 解析崩）"); return False
    lines = raw.decode("ascii").splitlines(keepends=True)
    changed = 0
    for i, ln in enumerate(lines):
        if ln.startswith('set "ENGINE_ARGS='):
            new = re.sub(r"--max-context \d+", "--max-context %d" % pool, ln)
            new = re.sub(r"--kv-capacity \d+", "--kv-capacity %d" % pool, new)
            if new != ln:
                lines[i] = new
                changed += 1
    if not changed:
        print("!! 没找到可替换的 ENGINE_ARGS 行"); return False
    with open(BAT, "wb") as f:
        f.write("".join(lines).encode("ascii"))
    return True


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(EXE):
        print("!! 引擎不存在:", EXE); sys.exit(3)
    base, model, kv, has_vision = load_bat()
    if base is None:
        print("!! 读不到启动器参数，改用内置兜底参数（结果可能偏乐观）")
        base = DEFAULT_ARGS
    if not model or not os.path.exists(model):
        cand = [f for f in os.listdir(ROOT) if f.endswith(".ninfer")]
        if not cand:
            print("!! 找不到 .ninfer 模型文件"); sys.exit(3)
        model = os.path.join(ROOT, cand[0])
        if base and base[0].endswith(".ninfer"):
            base[0] = model

    weight = os.path.getsize(model) / 1024 ** 3
    resident, resident_src = resident_estimate(weight, model)
    g = gpu_info()
    if engine_ready():
        print("!! 注意：引擎端口 %d 上已经有一个引擎在跑。" % ENGINE_PORT)
        print("   下面的'已用显存'里含着它的权重和 KV，账本与公式估算都会失真")
        print("   （会算出'可用于 KV = 负数'这种荒谬值）。定容前请先停引擎。")

    print("=" * 74)
    print("显卡      : %s" % g["name"])
    print("显存      : 总 %d MiB / 已用 %d MiB / 空闲 %d MiB" % (g["total"], g["used"], g["free"]))
    print("模型      : %s（磁盘 %.2f GiB -> 常驻约 %.1f GiB，%s）"
          % (os.path.basename(model), weight, resident, resident_src))
    print("引擎端口  : %d    （代理端口 %s / 客户端端口 %s）"
          % (ENGINE_PORT, batcfg.proxy_port(), batcfg.client_port()))
    print("KV 精度   : %s    vision: %s" % (kv, "开" if has_vision else "关"))
    print("参数来源  : %s" % (BAT if os.path.exists(BAT) else "内置兜底"))
    avail, est = estimate(resident, g, has_vision)
    print("-" * 74)
    print("可用于 KV : %d MiB（扣掉 桌面/权重/引擎250/安全%d%s）"
          % (avail, SAFETY_MIB, ("/vision %d" % VISION_MIB) if has_vision else ""))
    print("公式估算  : 池约 %d token" % est)
    print("=" * 74)

    if not a.probe:
        print("\n加 --probe 做实测二分（公式只是起点，必须实测）")
        sys.exit(0)

    log = os.environ.get("NINFER_AUTOCONFIG_LOG") or os.path.join(HERE, "_autoconfig.log")
    if engine_ready():
        print("\n!! 引擎端口 %d 上已经有一个引擎在跑（可能是你正在用的生产档）。" % ENGINE_PORT)
        print("   --probe 会 taskkill 掉它来逐个试参数；跑完需要重新启动生产档（桌面图标）。")
    print("\n实测二分中（每次真实启动一次引擎，约 8-12 秒）...")
    best = probe(base, est, log)
    if not best:
        print("\n!! 连最小池都起不来。检查：")
        print("   - 桌面占用是否过高 -> 让用户关掉浏览器 / 壁纸引擎后重试")
        print("   - 模型与引擎是否匹配（artifact identity 不受支持）")
        sys.exit(3)

    print("\n" + "=" * 74)
    print("实测最大可启动池 : %d token" % best)
    # 贴着上限跑很危险：桌面显存一波动就起不来。回退一档留余量。
    rec = max(MIN_POOL, best - STEP)
    print("建议使用         : %d token（回退一档，避免桌面波动导致启动失败）" % rec)
    print("=" * 74)
    if a.apply:
        print("已写入启动器" if apply_to_bat(rec) else "写入失败，请手工修改两个数值")
    else:
        print("加 --apply 可自动写回启动器")
