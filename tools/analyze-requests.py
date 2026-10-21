"""分析引擎请求日志 —— 找出真正拖慢体验的地方。

为什么需要它：
  logs/requests.jsonl 里每条请求有几十个字段，肉眼看不出"慢在哪一段"。
  这个脚本按维度聚合，直接回答三个问题：
    1. 时间花在 prefill / decode / 排队 哪一段？
    2. 前缀缓存到底有没有生效？（用 result.prefix_cache_hit_tokens）
    3. 哪些请求最贵？值得不值得优化？

★ 字段坑（2026-10-21 踩过）：
  materialization.cached_prefix_tokens **恒为 0**，它不是命中数。
  真正的命中字段是 **result.prefix_cache_hit_tokens**。
  配套字段：result.computed_prefill_tokens（实际重算的 token 数）
            result.prefix_reuse_path（private_turn_closure / shared_stable_prefix / ...）

用法：
  python tools/analyze-requests.py                 # 全量摘要
  python tools/analyze-requests.py --slow 20       # 只列最慢的 20 条
  python tools/analyze-requests.py --since 1016    # 只看 10 月 16 日之后
  python tools/analyze-requests.py --json out.json # 机器可读
"""
import argparse
import json
import os
import statistics
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "logs", "requests.jsonl")


def load(path, since=None):
    """读 jsonl，只保留 request_done 事件，并按 since 过滤。"""
    if not os.path.exists(path):
        sys.exit("找不到日志：%s" % path)
    out = []
    skipped = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                skipped += 1
                continue
            if ev.get("event") != "request_done":
                continue
            ts = ev.get("timestamp_unix_ms")
            if since and ts:
                import time as _t
                stamp = _t.strftime("%Y%m%d", _t.localtime(ts / 1000.0))
                if stamp < str(since):
                    continue
            out.append(ev)
    if skipped:
        print("（跳过 %d 行无法解析的 JSON）" % skipped)
    return out


def num(ev, path, default=0.0):
    """按 "result.prompt_tokens" 这样的路径取值，缺字段返回 default。"""
    cur = ev
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(part)
        if cur is None:
            return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def summarize(evs):
    n = len(evs)
    if not n:
        return None
    prompt = [num(e, "result.prompt_tokens") for e in evs]
    completion = [num(e, "result.completion_tokens") for e in evs]
    hit = [num(e, "result.prefix_cache_hit_tokens") for e in evs]

    prefill = [num(e, "timings_seconds.prefill") for e in evs]
    decode = [num(e, "timings_seconds.decode") for e in evs]
    total = [num(e, "timings_seconds.total") for e in evs]
    ttft = [num(e, "timings_seconds.ttft") for e in evs]

    # 有缓存命中的请求 / 全新请求
    cached = [i for i in range(n) if hit[i] > 0]
    cold = [i for i in range(n) if hit[i] == 0]

    hit_rate = [hit[i] / prompt[i] * 100.0 for i in range(n) if prompt[i] > 0]
    big = [i for i in range(n) if prompt[i] >= 10000]
    big_hit = [hit[i] / prompt[i] * 100.0 for i in big if prompt[i] > 0]

    # decode 速度（只统计真的生成了 token 的）
    dec_tps = []
    for i in range(n):
        if decode[i] > 0.05 and completion[i] > 2:
            dec_tps.append(completion[i] / decode[i])
    # prefill 速度：只算**真正重算**的 token，不能拿 prompt 总量除
    # （缓存命中时 prefill 耗时接近 0，prompt 却很大，会算出几万 tok/s 的假数）
    pre_tps = []
    for i in range(n):
        computed = num(e := evs[i], "result.computed_prefill_tokens")
        if computed <= 0:
            computed = max(0.0, prompt[i] - hit[i])
        if prefill[i] > 0.05 and computed > 500:
            pre_tps.append(computed / prefill[i])

    return {
        "requests": n,
        "prompt_total": int(sum(prompt)),
        "prompt_median": int(pct(prompt, 50)),
        "prompt_p90": int(pct(prompt, 90)),
        "prompt_max": int(max(prompt) if prompt else 0),
        "completion_total": int(sum(completion)),
        "cached_requests": len(cached),
        "cold_requests": len(cold),
        "cached_ratio_pct": round(len(cached) / n * 100.0, 1),
        "hit_rate_median_all": round(pct(hit_rate, 50), 1),
        "hit_rate_median_10k+": round(pct(big_hit, 50), 1) if big_hit else 0.0,
        "big_requests_10k+": len(big),
        "wall_total_s": round(sum(total), 1),
        "prefill_total_s": round(sum(prefill), 1),
        "decode_total_s": round(sum(decode), 1),
        "ttft_median_s": round(pct(ttft, 50), 2),
        "ttft_p90_s": round(pct(ttft, 90), 2),
        "prefill_tps_median": round(pct(pre_tps, 50), 0),
        "decode_tps_median": round(pct(dec_tps, 50), 1),
        "decode_tps_p90": round(pct(dec_tps, 90), 1),
        "reuse_paths": dict(Counter(
            e.get("result", {}).get("prefix_reuse_path") or "(none)" for e in evs)),
    }


def print_summary(s):
    if not s:
        print("没有 request_done 记录。")
        return
    wall = s["wall_total_s"] or 1.0
    print("=" * 62)
    print("请求总数        : %d（有缓存 %d / 全量重算 %d = 缓存率 %s%%）"
          % (s["requests"], s["cached_requests"], s["cold_requests"], s["cached_ratio_pct"]))
    print("prompt token    : 合计 %s / 中位 %s / P90 %s / 最大 %s"
          % (f'{s["prompt_total"]:,}', f'{s["prompt_median"]:,}',
             f'{s["prompt_p90"]:,}', f'{s["prompt_max"]:,}'))
    print("输出 token      : %s" % f'{s["completion_total"]:,}')
    print("-" * 62)
    print("墙钟总耗时      : %.1f s" % s["wall_total_s"])
    print("  prefill       : %8.1f s  (%4.1f%%)"
          % (s["prefill_total_s"], s["prefill_total_s"] / wall * 100))
    print("  decode        : %8.1f s  (%4.1f%%)"
          % (s["decode_total_s"], s["decode_total_s"] / wall * 100))
    other = s["wall_total_s"] - s["prefill_total_s"] - s["decode_total_s"]
    print("  其他/排队     : %8.1f s  (%4.1f%%)" % (other, other / wall * 100))
    print("-" * 62)
    print("TTFT 中位 %.2f s / P90 %.2f s" % (s["ttft_median_s"], s["ttft_p90_s"]))
    print("prefill 速度中位: %s tok/s" % f'{s["prefill_tps_median"]:,.0f}')
    print("decode  速度中位: %.1f tok/s（P90 %.1f）"
          % (s["decode_tps_median"], s["decode_tps_p90"]))
    print("-" * 62)
    print("缓存命中率      : 全体中位 %s%% / 长请求(≥10K)中位 %s%%（共 %d 条）"
          % (s["hit_rate_median_all"], s["hit_rate_median_10k+"], s["big_requests_10k+"]))
    print("复用路径        :")
    for k, v in sorted(s["reuse_paths"].items(), key=lambda x: -x[1]):
        print("    %-28s %d" % (k, v))
    print("=" * 62)


def print_slow(evs, limit):
    ranked = sorted(evs, key=lambda e: -num(e, "timings_seconds.total"))[:limit]
    print("\n最慢的 %d 条请求：" % len(ranked))
    print("%-3s %-19s %8s %8s %8s %8s %7s %6s"
          % ("#", "时间", "prompt", "命中", "prefill", "decode", "tok/s", "输出"))
    print("-" * 78)
    import time as _t
    for i, e in enumerate(ranked, 1):
        ts = e.get("timestamp_unix_ms")
        when = _t.strftime("%m-%d %H:%M:%S", _t.localtime(ts / 1000.0)) if ts else "?"
        p = int(num(e, "result.prompt_tokens"))
        h = int(num(e, "result.prefix_cache_hit_tokens"))
        pf = num(e, "timings_seconds.prefill")
        de = num(e, "timings_seconds.decode")
        c = num(e, "result.completion_tokens")
        tps = (c / de) if de > 0.05 else 0.0
        hr = ("%.0f%%" % (h / p * 100)) if p else "-"
        print("%-3d %-19s %8s %8s %8.2f %8.2f %7.1f %6d"
              % (i, when, f"{p:,}", hr, pf, de, tps, int(c)))


def main():
    ap = argparse.ArgumentParser(description="分析引擎请求日志")
    ap.add_argument("--log", default=LOG, help="请求日志路径")
    ap.add_argument("--slow", type=int, default=0, help="另外列出最慢的 N 条")
    ap.add_argument("--since", default=None,
                    help="只统计该日期之后，如 20261016")
    ap.add_argument("--json", default=None, help="把摘要写成 JSON")
    a = ap.parse_args()

    evs = load(a.log, a.since)
    s = summarize(evs)
    print_summary(s)
    if a.slow:
        print_slow(evs, a.slow)
    if a.json and s:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
        print("\n摘要已写入 %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
