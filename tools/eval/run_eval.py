"""KV 量化精度回归评测器。

用法:
  python run_eval.py --dtype nvfp4                 # 启动引擎跑一轮，结果写 eval-<dtype>.json
  python run_eval.py --dtype rk4v4
  python run_eval.py --compare nvfp4 rk4v4         # 读两份结果做 A/B 对比
  python run_eval.py --dtype nvfp4 --endpoint      # 不启动引擎，连已运行的引擎端口（默认取启动器）

设计要点:
  - 请求级一律 temperature=0 + reasoning_effort=none，把变量压到只剩 KV 精度本身。
  - 每条用例的 filler 带唯一盐（用例 id），避免跨条命中共享前缀缓存，保证条目彼此独立。
  - 全部自动判分，无人工环节；单条超时或异常记为 error，不中断整轮。
"""
import argparse, json, os, re, subprocess, sys, time, unicodedata, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import batcfg  # noqa: E402  上级目录助手：模型/端口/生产参数一律以启动器为准

ROOT = batcfg.ROOT
BENCH = os.path.join(ROOT, ".workbuddy", "bench")
EVALDIR = os.path.join(BENCH, "eval")
EXE = os.path.join(ROOT, "engine", "ninfer-serve-120a.exe")
MODEL = batcfg.resolve_model()
CASES = os.path.join(EVALDIR, "cases.json")
PORT = batcfg.engine_port()
BASE = "http://127.0.0.1:%d" % PORT

FILLER = "运维手册第 {n} 节：集群巡检按日执行，先检查磁盘水位，再检查服务健康探针，最后归档日志，全部操作需双人复核。"

def _prod_args():
    """生产档参数直接跟随启动器（唯一真相源），只裁掉三样：

    ① 模型路径（本脚本自己以 [EXE, MODEL] 启动）；
    ② --kv-dtype（评测按 --dtype 追加，重复会打架）；
    ③ 请求日志参数（评测不需要，且会往 logs/ 写）。

    2026-10-16 之前这里是写死的 `--max-context 131072 --kv-capacity 81920`：
    启动器早已是 184320/184320，评测却还在用另一个档，结论不可比。
    """
    args = batcfg.load_bat()[0]
    if not args:
        return None
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if i == 0 and str(a).lower().endswith(".ninfer"):
            i += 1
            continue
        if a in ("--kv-dtype", "--request-log-jsonl", "--request-log-max-mib"):
            i += 2
            continue
        out.append(a)
        i += 1
    return out


PROD_ARGS = _prod_args() or [
    "--host", "127.0.0.1", "--port", str(PORT), "--model-id", "qwen3.8-27b",
    "--max-context", "184320", "--kv-capacity", "184320",
    "--cuda-graph-allowance-mib", "192", "--host-kv-mib", "4096", "--prefill-chunk", "512",
    "--spec", "mtp", "--draft-tokens", "4",
    "--vision", "--vision-residency", "overlay", "--media-cache-mib", "0",
    "--default-max-tokens", "32768", "--default-reasoning-effort", "high",
    "--default-thinking-budget", "2048",
    "--thinking-budget-message", "Thinking budget reached. Stop deliberating and write your final answer now.",
    "--max-concurrency", "1", "--max-shared-prefixes", "7", "--derive-session-keys",
    "--presence-penalty", "0", "--temperature", "0.7", "--top-p", "0.9", "--top-k", "20",
]


# ---------- 判分 ----------

def norm(s):
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("，", ",").replace("：", ":").replace("；", ";")
    return re.sub(r"\s+", " ", s).strip().lower()


def judge(case, answer):
    ans = answer or ""
    if case["match"] == "regex":
        pat = case.get("match_pattern") or case["expect"]
        if re.search(pat, ans, re.I | re.S):
            return True
        stripped = ans.strip().strip("。.！!？?：:；; \n\r\t*`\"'")
        return bool(re.search(pat, stripped, re.I | re.S))
    exp = norm(case["expect"])
    a = norm(ans)
    if case["match"] == "exact":
        return a == exp
    return exp in a


# ---------- prompt 构造 ----------

def build_ctx(case):
    n = max(4, int(case["ctx_tokens"]) // 45)
    salt = case["id"]
    parts = [FILLER.format(n=i).replace("运维手册", "运维手册-" + salt) for i in range(1, n + 1)]
    idx = {"start": max(1, int(n * 0.08)),
           "middle": n // 2,
           "end": max(1, int(n * 0.92))}.get(case.get("depth", "middle"), n // 2)
    parts.insert(idx, case["needle"])
    if case.get("inject_text"):
        j = min(len(parts) - 1, idx + max(1, int(n * 0.30)))
        parts.insert(j, case["inject_text"])
    return "\n".join(parts)


def build_prompt(case):
    if "needle" in case:
        return build_ctx(case) + "\n\n问题：" + case["question"]
    return case["question"]


# ---------- 引擎与请求 ----------

def kill_engine():
    subprocess.run(["taskkill", "/F", "/IM", "ninfer-serve-120a.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)


def launch(dtype, model, tag=""):
    log = os.path.join(EVALDIR, "engine-%s%s.log" % (dtype, tag))
    args = [EXE, model] + PROD_ARGS + ["--kv-dtype", dtype]
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
    while time.time() - t0 < 180:
        if p.poll() is not None:
            print("引擎启动失败，见", log)
            sys.exit(1)
        try:
            if urllib.request.urlopen(BASE + "/v1/models", timeout=1).status == 200:
                print("引擎就绪 %.1fs（dtype=%s，模型=%s）"
                      % (time.time() - t0, dtype, os.path.basename(model)))
                return p, f
        except Exception:
            pass
        time.sleep(1)
    print("引擎 180s 未就绪，见", log)
    sys.exit(1)


def ask(messages, max_tokens=256):
    payload = {"model": "qwen3.8-27b", "messages": messages,
               "max_tokens": max_tokens, "temperature": 0, "reasoning_effort": "none"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"], time.time() - t, d.get("usage", {}) or {}


def run_case(case):
    if "turns" in case:
        msgs, ans, dt, usage = [], "", 0.0, {}
        for t in case["turns"]:
            msgs.append({"role": "user", "content": t})
            ans, dt, usage = ask(msgs)
            msgs.append({"role": "assistant", "content": ans})
        return ans, dt, usage
    ans, dt, usage = ask([{"role": "user", "content": build_prompt(case)}])
    return ans, dt, usage


# ---------- 主流程 ----------

def run(dtype, endpoint_only=False, model=None, tag=""):
    model = model or MODEL
    print("评测模型: %s" % os.path.basename(model))
    with open(CASES, encoding="utf-8") as fh:
        spec = json.load(fh)
    cases = spec["cases"]
    proc = None
    engfh = None
    if not endpoint_only:
        kill_engine()
        proc, engfh = launch(dtype, model, tag)

    results = []
    t_start = time.time()
    for c in cases:
        rec = {"id": c["id"], "cat": c["cat"], "expect": c["expect"], "match": c["match"]}
        try:
            ans, dt, usage = run_case(c)
            ok = judge(c, ans)
            rec.update({"ok": ok, "answer": ans.strip()[:200], "wall_s": round(dt, 2),
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens")})
        except Exception as e:
            rec.update({"ok": False, "answer": "", "error": str(e)[:160]})
        results.append(rec)
        flag = "PASS" if rec.get("ok") else "FAIL"
        print("  %-4s %-12s %s  %s" % (c["id"], c["cat"], flag, (rec.get("answer") or rec.get("error", ""))[:70]))
        sys.stdout.flush()

    passed = sum(1 for r in results if r.get("ok"))
    out = {"dtype": dtype, "tag": tag, "model": os.path.basename(model),
           "passed": passed, "total": len(results),
           "pass_rate": round(passed / len(results), 4),
           "elapsed_s": round(time.time() - t_start, 1), "results": results}
    outpath = os.path.join(EVALDIR, "eval-%s%s.json" % (dtype, tag))
    with open(outpath, "w", encoding="utf-8") as outfh:
        json.dump(out, outfh, ensure_ascii=False, indent=2)

    print("\n%s: %d/%d 通过 (%.1f%%)，用时 %.1fs → %s"
          % (os.path.basename(model), passed, len(results),
             100.0 * passed / len(results), out["elapsed_s"], os.path.basename(outpath)))
    if engfh:
        try:
            engfh.flush()
            engfh.close()
        except Exception:
            pass
    if proc:
        proc.terminate()
        try:
            proc.wait(15)
        except Exception:
            proc.kill()
        kill_engine()
    return out


def compare(dtypes):
    data = {}
    for d in dtypes:
        p = os.path.join(EVALDIR, "eval-%s.json" % d)
        if not os.path.exists(p):
            print("缺少结果文件:", p)
            return
        with open(p, encoding="utf-8") as fh:
            data[d] = json.load(fh)

    mx = len(max(data.values(), key=lambda x: len(x["results"]))["results"])
    print("\n" + "=" * 84)
    print("%-5s %-13s %s" % ("id", "category", "  ".join("%-8s" % d for d in dtypes)))
    print("-" * 84)
    regress = []
    for i in range(mx):
        row = []
        for d in dtypes:
            rs = data[d]["results"]
            row.append("PASS" if (i < len(rs) and rs[i].get("ok")) else "FAIL")
        if len(set(row)) > 1:
            regress.append((data[dtypes[0]]["results"][i]["id"], row))
        print("%-5s %-13s %s" % (data[dtypes[0]]["results"][i]["id"],
                                 data[dtypes[0]]["results"][i]["cat"],
                                 "  ".join("%-8s" % r for r in row)))
    print("-" * 84)
    for d in dtypes:
        print("%-18s %d/%d (%.1f%%)  用时 %ss" % (d, data[d]["passed"], data[d]["total"],
                                                 100 * data[d]["pass_rate"], data[d]["elapsed_s"]))
    print("\n不一致条目: %d 条" % len(regress))
    for cid, row in regress:
        print("  %-5s %s" % (cid, "  ".join("%-8s" % r for r in row)))
    base, alt = dtypes[0], dtypes[-1]
    delta = data[alt]["passed"] - data[base]["passed"]
    print("\n结论: %s 相对 %s %+d 条（%s）" % (
        alt, base, delta,
        "需人工复核" if abs(delta) >= 2 else "差异在噪声范围内（<2 条）"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dtype")
    ap.add_argument("--endpoint", action="store_true", help="不启动引擎，连已运行的端口")
    ap.add_argument("--model", help="要评测的 .ninfer：文件名或完整路径（默认取启动器里的那个）")
    ap.add_argument("--tag", default="", help="结果文件名后缀，例如 -huihui → eval-nvfp4-huihui.json")
    ap.add_argument("--compare", nargs=2, metavar=("A", "B"))
    a = ap.parse_args()
    chosen = MODEL
    if a.model:
        chosen = a.model if os.path.isabs(a.model) else os.path.join(ROOT, a.model)
        if not os.path.exists(chosen):
            print("找不到模型:", chosen)
            avail = sorted(f for f in os.listdir(ROOT) if f.endswith(".ninfer"))
            print("根目录现有：", ", ".join(avail))
            sys.exit(2)
    if a.compare:
        compare(a.compare)
    elif a.dtype:
        run(a.dtype, endpoint_only=a.endpoint, model=chosen, tag=a.tag)
    else:
        ap.print_help()
