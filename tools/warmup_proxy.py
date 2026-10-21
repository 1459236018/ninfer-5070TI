"""warmup_proxy.py -- 让"引擎重启后的第一次对话"不再等那 28 秒。

问题（2026-10-16 实测）
    引擎的 KV 检查点目录与共享前缀目录都在进程内存里。引擎一重启，两者全空，
    而 WorkBuddy 每个请求都顶着 ~36k token 的固定前导（system + tools + 技能 + 记忆），
    于是"重启后的第一句"必须全量 prefill：36,025 / 1,455 tok/s ≈ 28 s，客户端看到 42 s。
    同一进程内共享前缀完全正常（第二个请求 0.33 s / 命中 99.9%），所以只有第一句慢。

办法
    本模块在客户端与引擎之间做一层本地转发（默认 8093 -> 8094），顺手把每个
    POST /v1/chat/completions 的**稳定前导**（messages 中第一条 user 之前的全部内容
    加上 tools）摘出来存盘；引擎就绪后用这份样本回放一次预热请求，
    把共享前缀目录重建起来。之后用户真正发问时就能命中，第一句从 42 s 掉到 1~2 s。

    代价说明：预热本身仍要花那 ~25 s 的 GPU 时间，只是被挪到"引擎刚就绪、用户还在
    切窗口打字"的窗口里。若用户比预热更快发问，引擎会先跑完预热再答（总时长与不预热
    相当，不会更差）。

用法
    from warmup_proxy import WarmupProxy
    proxy = WarmupProxy(PROJECT, log=log)
    proxy.start()            # 起转发线程；8093 被占或失败只记日志，不影响 8094 直连
    proxy.warm_up()          # 引擎就绪后调用一次（可在后台线程里调用）

    客户端把模型地址指到 http://127.0.0.1:8093/v1 即可；指回 8094 就绕过本代理。
"""
import json
import os
import re
import threading
import time
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = ("127.0.0.1", 8094)
LISTEN = ("127.0.0.1", 8093)

# 预热总开关。2026-10-16 17:05 用户"两个模型都取消预热" ⇒ False（彻底关掉）。
# 想恢复：改成 True，并按需往 WARM_ARTIFACTS 里填 artifact 名。
WARM_ENABLED = False
# 预热白名单（仅在 WARM_ENABLED=True 时生效）：只给这些 artifact 预热；
# 空 set() = 全部预热。
WARM_ARTIFACTS = {"huihui-qwen3.8-27b-abliterated-swift15-gsq-rco-iq3xxs"}
# 逐跳首部，转发时必须丢掉（RFC 7230 §6.1）
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
}
# 样本里塞进 user 消息的那句占位文本：只要前缀一致就会被复用，内容无关紧要
DUMMY_USER = "ok"

# ToolSearch.description 里那份随连接器状态变动的 deferred tools 清单。
# 它在 system prompt 的 <connector-status> 段里已经完整出现过一遍，属于重复信息。
_DEFERRED_RE = re.compile(
    r"The following deferred tools are available via ToolSearch\..*?"
    r"</available_deferred_tools>\s*", re.S)


def strip_dynamic_tool_list(body):
    """删掉 ToolSearch.description 里那份动态的 deferred tools 清单。

    2026-10-16 实测根因：这份清单随连接器/MCP 状态变化，而 ToolSearch 排在 tools
    数组末尾（tools 共约 23k token，ToolSearch 独占 ~2.5k）。它一变，prompt 前缀就在
    18k~22k token 处断开，每次对话要多算 18k+ token ≈ 13~15 s（实测 ttft 18.8s / 20.5s）。
    删掉后既省体积（~9,000 字符），前缀也稳定下来；信息不丢 —— system 的
    <connector-status> 段里本来就是完整的。
    """
    try:
        d = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    changed = False
    for t in d.get("tools") or []:
        fn = t.get("function") or {}
        if fn.get("name") == "ToolSearch":
            desc = fn.get("description") or ""
            new = _DEFERRED_RE.sub("", desc)
            if new != desc:
                fn["description"] = new
                changed = True
    if not changed:
        return body
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


def flatten_inline_text(body):
    """把"只含 text 元素的多模态 content"折叠成普通字符串再转发。

    2026-10-16 根因（实测）：
      WorkBuddy 的第一条 user 是 8,933 字符的 [{"type":"text","text":...}]，
      引擎对这种**数组格式**的 content 会走 media 预处理路径，于是共享前缀复用失效 ——
      · 完全相同的 prompt（warm1 vs warm2）→ cached 36,025（100%）
      · 前缀相同但问句不同 → cached 0，仍是 29~33 s 全量
      （而纯字符串 content 的前缀复用完全正常，前 10 项对照测试全部命中）
    折叠成字符串后前缀复用恢复正常。含图片的请求原样保留，不做改动。
    """
    try:
        d = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    msgs = d.get("messages")
    if not isinstance(msgs, list):
        return body
    changed = False
    for m in msgs:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, list) and c and all(
                isinstance(e, dict) and e.get("type") == "text" for e in c):
            m["content"] = "".join(e.get("text") or "" for e in c)
            changed = True
    if not changed:
        return body
    return json.dumps(d, ensure_ascii=False).encode("utf-8")


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NInferWarmupProxy/1.0"

    def log_message(self, *args):          # 静音 access log
        pass

    # ------------------------------------------------------------ 读请求体
    def _read_body(self):
        n = self.headers.get("Content-Length")
        if n:
            try:
                return self.rfile.read(int(n))
            except Exception:
                return b""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    if not out:
                        break
                    continue
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                out.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(out)
        return b""

    # ------------------------------------------------------------ 转发
    def _handle(self):
        proxy = self.server.proxy
        body = self._read_body() if self.command in ("POST", "PUT", "PATCH") else b""
        if self.command == "POST" and "/v1/chat/completions" in self.path:
            proxy.note_raw(body)                 # 原始请求（诊断用）
            body = strip_dynamic_tool_list(body)  # ← 删掉会变的 deferred tools 清单
            folded = flatten_inline_text(body)    # 数组格式会让前缀复用失效
            proxy.log("proxy: 加工 %s（%d -> %d 字节）"
                      % ("已改写" if len(folded) != len(body) else "无需改写",
                         len(body), len(folded)))
            body = folded
            proxy.note_request(body)
        # 2026-10-16 诊断：每一次 POST 都留一行，用来确认"请求到底有没有经过代理"。
        # 之前的现场是：客户端配置指向 8094，但 14:30 那次慢请求在代理侧毫无记录。
        if self.command == "POST":
            proxy.note_seen(self.path, body)

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP}
        if body:
            headers["Content-Length"] = str(len(body))

        try:
            conn = http.client.HTTPConnection(proxy.upstream[0], proxy.upstream[1],
                                              timeout=proxy.timeout)
            conn.request(self.command, self.path, body=body or None, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            proxy.log("proxy: 上游不可达 %r" % (exc,))
            try:
                self.send_error(502, "engine unreachable")
            except Exception:
                pass
            return

        try:
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                lk = k.lower()
                if lk in HOP_BY_HOP or lk == "content-length":
                    continue
                self.send_header(k, v)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            # 逐块搬运：read1() 只在手里有数据时返回，所以 SSE 的实时性不被破坏
            while True:
                data = resp.read1(32768)
                if not data:
                    break
                self.wfile.write(b"%X\r\n" % len(data))
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass                      # 客户端提前断开等，属正常
        finally:
            try:
                conn.close()
            except Exception:
                pass

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle


class WarmupProxy(object):
    def __init__(self, project, listen=LISTEN, upstream=UPSTREAM, log=print,
                 timeout=1800):
        self.listen = listen
        self.upstream = upstream
        self.log = log
        self.timeout = timeout
        self.state_dir = os.path.join(project, ".workbuddy", "state")
        # 每次真实请求的"骨架"追加到这里，用于排查"为什么预热的前缀对不上"
        self.diag_path = os.path.join(self.state_dir, "request-diag.jsonl")
        # 最近一次真实请求的完整 body，便于逐字节复现排查
        self.last_path = os.path.join(self.state_dir, "last-request.json")
        # 折叠**之前**的原始 body（诊断用：确认折叠到底有没有生效、content 里有没有非 text 元素）
        self.raw_path = os.path.join(self.state_dir, "last-request-raw.json")
        # 当前引擎加载的 artifact 名（预热样本按它分开存，见 _sample_path）
        self._artifact = [""]
        self.stats = {"requests_seen": 0, "sample_bytes": 0, "warmed": 0,
                      "warm_seconds": None}
        self._httpd = None
        self._thread = None

    # ------------------------------------------------------------ 按模型分样本
    def _current_artifact(self):
        """从引擎请求日志读当前实例加载的 artifact 名。

        2026-10-16：引擎的 KV 前缀缓存**按 artifact 隔离**，换模型后完全不通用 ——
        无审查档（huihui-qwen3.8-27b-abliterated-...）与默认档
        （swift-1.5-qwen3.8-27b-...）是两个不同 artifact。所以预热样本必须按模型
        分开存，否则切档后那 25 s 预热纯属白跑（实测：样本是 A 档采的，给 B 档
        预热，引擎直接不认）。
        """
        try:
            p = os.path.join(self.state_dir, "..", "..", "logs", "requests.jsonl")
            last = ""
            with open(p, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"server_start"' in line:
                        last = line
            if last:
                a = (json.loads(last).get("artifact") or {})
                name = a.get("name") or ""
                if name:
                    self._artifact[0] = name
        except Exception:
            pass
        return self._artifact[0]

    def _sample_path(self):
        """按 artifact 分开的样本路径；读不到 artifact 时退回默认文件名。"""
        art = self._current_artifact()
        base = "warmup-sample.json"
        if art:
            base = "warmup-sample-%s.json" % hashlib.md5(
                art.encode("utf-8")).hexdigest()[:8]
        return os.path.join(self.state_dir, base)

    # ------------------------------------------------------------ 生命周期
    def start(self):
        try:
            httpd = _Server(self.listen, _Handler)
        except Exception as exc:
            self.log("proxy: 监听 %s:%d 失败（%r）；8094 直连不受影响"
                     % (self.listen[0], self.listen[1], exc))
            return False
        httpd.proxy = self
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        kwargs={"poll_interval": 0.5},
                                        name="warmup-proxy", daemon=True)
        self._thread.start()
        self.log("proxy: %s:%d -> %s:%d 已就绪"
                 % (self.listen[0], self.listen[1],
                    self.upstream[0], self.upstream[1]))
        return True

    def stop(self):
        try:
            if self._httpd:
                self._httpd.shutdown()
                self._httpd.server_close()
        except Exception:
            pass

    @property
    def listening(self):
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------ 样本
    def load_sample(self):
        try:
            with open(self._sample_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save_sample(self, sample):
        try:
            os.makedirs(os.path.dirname(self._sample_path()), exist_ok=True)
            tmp = self._sample_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sample, f, ensure_ascii=False)
            os.replace(tmp, self._sample_path())
            self.stats["sample_bytes"] = os.path.getsize(self._sample_path())
        except Exception as exc:
            self.log("proxy: 写样本失败 %r" % (exc,))

    def note_seen(self, path, body):
        """每次 POST 都记一行（路径 + 字节数），用于确认流量是否真的经过本代理。"""
        try:
            with open(self.diag_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "kind": "proxy_seen", "path": path,
                                    "bytes": len(body)}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def note_raw(self, body):
        """存下折叠之前的原始请求体（诊断用）。"""
        try:
            with open(self.raw_path, "wb") as f:
                f.write(body)
        except Exception:
            pass

    def note_request(self, body):
        """从真实请求里摘出稳定前导（第一条 user 之前的一切 + tools）。"""
        if not body:
            return
        try:
            d = json.loads(body.decode("utf-8", "replace"))
        except Exception:
            return
        msgs = d.get("messages")
        if not isinstance(msgs, list) or not msgs:
            return

        # ---- 保留完整 body（供事后逐字节复现）
        try:
            with open(self.last_path, "wb") as f:
                f.write(body)
        except Exception:
            pass

        # ---- 诊断骨架（每次请求都追加一行；用于比对"为什么预热的前缀对不上"）
        # 2026-10-16 实测：引擎侧复用 100% 没问题，但真实请求 36,025 token 与样本
        # 复刻的 33,477 对不上，且分叉点很靠前。这里把 system 长度/首尾、工具清单、
        # 消息结构都记下来，才能定位差异。
        try:
            sys0 = msgs[0].get("content") if isinstance(msgs[0].get("content"), str) else ""
            fu = next((i for i, m in enumerate(msgs)
                       if isinstance(m, dict) and (m.get("role") or "").lower() == "user"), -1)
            tools = d.get("tools") or []
            skel = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": d.get("model"),
                "max_tokens": d.get("max_tokens"),
                "stream": d.get("stream"),
                "top_keys": sorted(d.keys()),
                "n_messages": len(msgs),
                "first_user_idx": fu,
                "roles": [[m.get("role"),
                           len(m.get("content") or "") if isinstance(m.get("content"), str) else -1]
                          for m in msgs[:8] if isinstance(m, dict)],
                "system_chars": len(sys0),
                "system_head": sys0[:160],
                "system_tail": sys0[-160:],
                "n_tools": len(tools),
                "tool_names": [(t.get("function") or {}).get("name") for t in tools],
                "tools_chars": len(json.dumps(tools, ensure_ascii=False)),
            }
            with open(self.diag_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(skel, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # 2026-10-16 关键发现：WorkBuddy 在引擎启动 / 新会话时会先发一个"生成会话标题"的请求
        # （system 只有 2056 字符，以 "Generate a concise, sentence-case title" 开头）。
        # 实测它会挤掉刚预热好的检查点，导致用户第一句又退回全量 prefill。
        # 对照证据：
        #   预热 → 用户请求（无标题请求介入）      → cached 33,466（92.9%）✓
        #   预热 → 标题请求 → 用户请求             → cached 0 ✗
        # 对策：识别到标题请求就在它完成之后补一次预热。
        # ★ 而且标题请求**绝不能**当作预热材料 —— 它的 system 与真正的 agent 前导完全不同，
        #   一旦被存进样本（实测 0.3 KiB），后续所有预热都会用错材料。
        try:
            head = msgs[0].get("content")
            head = head if isinstance(head, str) else ""
            if "Generate a concise, sentence-case title" in head[:400]:
                self.log("proxy: 检测到会话标题请求 → 稍后补一次预热（不作为预热材料）")
                threading.Thread(target=self._warm_later, name="warm-later",
                                 daemon=True).start()
                return
        except Exception:
            pass

        self.stats["requests_seen"] += 1
        # 2026-10-16（关键修正）：预热材料 = 真实请求的**完整 messages**。
        # 原先只保留"第一条 user 之前"的内容（system + tools），看着足够，实测完全对不上：
        # WorkBuddy 的第一条 user 是一个 8,933 字符的多模态数组（<system-reminder> 里带
        # OS / Shell / IDE 主题 / Workspace Folder），引擎会把它走 media 预处理路径，
        # 于是真实 prompt 的结构与"system + tools + 一句 ok"截然不同 —— 前缀对不齐，
        # 预热等于白做（实测：重启后第一句仍是 31~33 s 全量 prefill）。
        # 现在原样重放整份 messages（只把 max_tokens 压到 1 免得真生成），
        # 预热出来的 prompt 与真实请求逐 token 一致，那段 ~36k 前导才能真正被复用。
        sample = {
            "model": d.get("model"),
            "messages": msgs,
            "max_tokens": 1,
            "temperature": 0,
            "stream": False,
        }
        if d.get("tools"):
            sample["tools"] = d["tools"]
        self.save_sample(sample)
        self.log("proxy: 记录预热材料 %d 条消息%s，样本 %.1f KiB"
                 % (len(msgs),
                    " + tools" if d.get("tools") else "",
                    len(json.dumps(sample, ensure_ascii=False)) / 1024.0))

    # ------------------------------------------------------------ 预热
    def _warm_later(self, delay=3.0):
        """延迟一小会儿再预热（用于"标题请求把检查点挤掉"之后的补做）。"""
        time.sleep(delay)
        self.warm_up()

    def warm_up(self, retries=3, retry_wait=5.0):
        """引擎就绪后回放样本，把共享前缀目录重建起来。

        2026-10-16（第二次修正）：**必须连发两次**。引擎的"跨会话共享前缀"需要同一个
        前缀被至少两个请求请求过才会正式发布（"见过两次才认"），只发一次等于只登记不发布；
        而 WorkBuddy 在引擎启动 30 多秒后会先发一个"生成会话标题"的请求（system 仅 2056
        字符），它会把预热刚建立的私有检查点挤掉。结果：第一句用户请求自己成了"第二个请求"
        —— 触发发布，但自己享受不到，仍然是 31~33 s 全量 prefill。
        实测证据：预热后用户请求 prompt 36,025 / cached 0 / 33 s；同一份 body 事后重放
        则 cached 36,020 —— 说明前缀已发布，只是当时没赶上。
        """
        if not WARM_ENABLED:
            self.log("warmup: 已全局关闭（WARM_ENABLED=False），跳过")
            return False
        art = self._current_artifact()
        if WARM_ARTIFACTS and art and art not in WARM_ARTIFACTS:
            self.log("warmup: 跳过（artifact=%s 不在预热白名单，不占引擎时间）" % art)
            return False
        sample = self.load_sample()
        if not sample:
            self.log("warmup: 还没有样本（客户端还没经代理发过请求），跳过")
            return False
        body = json.dumps(sample, ensure_ascii=False).encode("utf-8")
        # 样本文件可能还是"未折叠"的旧版本（例如手工重建的），这里统一走一遍折叠，
        # 保证预热喂给引擎的 prompt 与转发时改写的请求**完全一致**。
        # 2026-10-16 踩坑：预热样本 148.4 KiB（未折叠）而转发后是 141.3 KiB，
        # 两者前缀对不上，第一句照样 31 s。
        body = flatten_inline_text(body)
        done = 0
        for i in (1, 2):
            t0 = time.time()
            try:
                conn = http.client.HTTPConnection(self.upstream[0], self.upstream[1],
                                                  timeout=self.timeout)
                conn.request("POST", "/v1/chat/completions", body=body,
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
                resp.read()
                dt = time.time() - t0
                if resp.status != 200:
                    raise RuntimeError("HTTP %d" % resp.status)
                conn.close()
                done += 1
                self.stats["warmed"] += 1
                self.stats["warm_seconds"] = dt
                self.log("warmup: 第 %d 次完成，%.1f s（样本 %.1f KiB）"
                         % (i, dt, len(body) / 1024.0))
            except Exception as exc:
                self.log("warmup: 第 %d 次失败 %r" % (i, exc))
                time.sleep(retry_wait)
                continue
            if i == 1:
                time.sleep(2.0)     # 让引擎登记完，再发第二次触发发布
        if done:
            self.log("warmup: 预热完成（共 %d 次），第一句可以秒回了" % done)
            return True
        return False


if __name__ == "__main__":
    import sys
    proj = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = WarmupProxy(proj)
    p.start()
    if len(sys.argv) > 1 and sys.argv[1] == "warm":
        p.warm_up()
    print("代理已启动（Ctrl+C 退出）。样本：%s" % p._sample_path())
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        p.stop()
