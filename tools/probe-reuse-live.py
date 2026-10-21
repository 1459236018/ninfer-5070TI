"""连到【正在运行】的引擎，复现「同一会话连续两轮」的前缀复用。

不动引擎进程（不重启、不改参数），只发请求，看 usage.prompt_tokens_details.cached_tokens。
用法: python probe-reuse-live.py [循环轮数]
"""
import json, os, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import batcfg  # 同目录助手：端口以启动器为准（引擎 8095，8094 是预热代理）

BASE = batcfg.engine_url()


def ask(msgs, mt=24):
    body = json.dumps({"model": "qwen3.8-27b", "messages": msgs,
                       "max_tokens": mt, "temperature": 0,
                       "reasoning_effort": "none"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t = time.time()
    with urllib.request.urlopen(req, timeout=900) as r:
        d = json.loads(r.read())
    dt = time.time() - t
    u = d.get("usage") or {}
    det = u.get("prompt_tokens_details") or {}
    return {"prompt": u.get("prompt_tokens"), "cached": det.get("cached_tokens", 0),
            "out": u.get("completion_tokens"), "wall_s": round(dt, 2)}


FILLER = "会议记录第 {n} 条：本周完成接口联调，遗留三个待办项，下周排期复核，所有变更需走变更单。\n"
N = 350
hist = "".join(FILLER.format(n=i) for i in range(1, N + 1))

msgs = [{"role": "user", "content": hist + "\n\n读完后请只回复 OK。"}]
print("历史约 %d 字符（≈%d token）" % (len(hist), len(hist) // 3))
print("轮 1:", ask(msgs))
sys.stdout.flush()
msgs.append({"role": "assistant", "content": "OK"})
msgs.append({"role": "user", "content": "刚才那份记录一共写了多少条？只输出数字。"})
print("轮 2:", ask(msgs))
sys.stdout.flush()

rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
for i in range(rounds):
    msgs.append({"role": "assistant", "content": "350"})
    msgs.append({"role": "user", "content": "第 %d 次追问：把最后一条记录的编号再报一次，只输出数字。" % (i + 3)})
    print("轮 %d:" % (i + 3), ask(msgs))
    sys.stdout.flush()
