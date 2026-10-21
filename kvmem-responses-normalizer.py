#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kvmem-responses-normalizer.py
=============================
本地小代理：把 Hermes Desktop 发给引擎的 /v1/responses 请求"洗干净"。

背景：ninfer 引擎要求 Responses API 里每个 type=reasoning 的 item 必须带
content 数组（content: [] 即可），否则返回
    HTTP 400: reasoning Item must contain a content array
Hermes Desktop 在对话历史里存的 reasoning item 只带 summary、没有 content，
所以一发请求就被引擎拒绝。本代理在转发前自动给缺失 content 的 reasoning
item 补上 content: []，其余字段原样透传。

用法：
    python kvmem-responses-normalizer.py [上游地址] [监听端口]
    默认上游 http://127.0.0.1:8094 ，监听 http://127.0.0.1:8096
然后把 Hermes Desktop 的后端地址改成 http://127.0.0.1:8096 即可。

流式（stream=true）与普通请求都支持，按 chunked 原样转发。
"""
import json
import sys
import time
import http.server
import urllib.request
import urllib.error
import urllib.parse

UPSTREAM = "http://127.0.0.1:8094"
DEFAULT_LISTEN_PORT = 8096
LOG_PATH = None


def _log(line: str):
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S") + " " + line + "\n")
        except Exception:
            pass

HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding", "content-length",
    "content-encoding", "upgrade", "proxy-authorization", "proxy-connection",
}


def summarize_request(obj) -> str:
    """输出请求结构摘要（不含图片 base64 等大字段）。"""
    parts = []
    if isinstance(obj, dict):
        tools = obj.get("tools")
        if isinstance(tools, list):
            names = []
            for t in tools:
                if isinstance(t, dict):
                    nm = t.get("name")
                    if not nm and isinstance(t.get("function"), dict):
                        nm = t["function"].get("name")
                    fn_type = t.get("type")
                    if not fn_type and isinstance(t.get("function"), dict):
                        fn_type = t["function"].get("type")
                    names.append("%s:%s" % (nm, fn_type))
            parts.append("tools=[" + ",".join(names) + "]")
        inp = obj.get("input")
        if isinstance(inp, list):
            kinds = []
            for it in inp:
                if not isinstance(it, dict):
                    kinds.append("?")
                    continue
                t = it.get("type", "?")
                if t == "message":
                    role = it.get("role", "?")
                    has_img = any(
                        isinstance(c, dict) and c.get("type") == "input_image"
                        for c in it.get("content", [])
                        if isinstance(c, dict)
                    )
                    kinds.append("msg/%s%s" % (role, "+img" if has_img else ""))
                elif t == "reasoning":
                    kinds.append("reasoning")
                else:
                    kinds.append(t)
            parts.append("input=[" + ",".join(kinds) + "]")
    return " ".join(parts)


def normalize_body(raw: bytes) -> bytes:
    """清洗 /v1/responses 的 input 数组。

    引擎规则（已实测）：
      - reasoning item 缺 content 数组  -> 400 reasoning Item must contain a content array
      - content 里混有非 reasoning_text -> 400 reasoning content only supports reasoning_text parts
      - 带 summary 的 reasoning item     -> 400 summary/encrypted content cannot reconstruct
      - content: [] 或 content=[reasoning_text] -> 200

    因此只保留"content 里含 reasoning_text 部分"的 reasoning item，其余直接丢弃。
    本引擎默认 --default-reasoning-effort none，丢弃 reasoning 不影响生成。
    """
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        _log("REQ %s [unparseable body]" % summarize_request(None))
        return raw
    _log("REQ %s" % summarize_request(obj))
    changed = False
    if isinstance(obj, dict):
        inp = obj.get("input")
        if isinstance(inp, list):
            kept = []
            for it in inp:
                if isinstance(it, dict) and it.get("type") == "reasoning":
                    content = it.get("content")
                    if isinstance(content, list) and any(
                        isinstance(p, dict) and p.get("type") == "reasoning_text"
                        for p in content
                    ):
                        kept.append(it)
                    else:
                        changed = True  # 丢弃无用/不兼容的 reasoning item
                else:
                    kept.append(it)
            if changed:
                obj["input"] = kept
    if not changed:
        return raw
    _log("REQ normalized: dropped %d reasoning item(s)" % (len(inp) - len(kept)))
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _relay(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        if self.path.startswith("/v1/") and "responses" in self.path:
            body = normalize_body(body)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        full_url = urllib.parse.urljoin(UPSTREAM, self.path)
        req = urllib.request.Request(full_url, data=body, method=self.command, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=600) as up:
                first = up.read(65536)
                self.send_response(up.status)
                for k, v in up.headers.items():
                    if k.lower() not in HOP_BY_HOP:
                        self.send_header(k, v)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                _log("RESP %s status=%d first_bytes=%d" % (self.path, up.status, len(first)))
                if first:
                    self.wfile.write(("%X\r\n" % len(first)).encode("ascii"))
                    self.wfile.write(first)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                while True:
                    chunk = up.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(("%X\r\n" % len(chunk)).encode("ascii"))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        except urllib.error.HTTPError as e:
            err_body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except Exception as e:  # 上游不可达等
            msg = json.dumps({"error": {"message": "proxy upstream error: %s" % e,
                                        "type": "proxy_error"}}).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def do_POST(self):
        self._relay()

    def do_GET(self):
        self._relay()

    def log_message(self, *args):
        pass


def main():
    global UPSTREAM, LOG_PATH
    upstream = sys.argv[1] if len(sys.argv) > 1 else UPSTREAM
    if not upstream.startswith("http://") and not upstream.startswith("https://"):
        upstream = "http://" + upstream
    UPSTREAM = upstream
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LISTEN_PORT
    if len(sys.argv) > 3:
        LOG_PATH = sys.argv[3]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), ProxyHandler)
    print("kvmem-responses-normalizer listening on http://127.0.0.1:%d -> %s (log=%s)"
          % (port, UPSTREAM, LOG_PATH))
    server.serve_forever()


if __name__ == "__main__":
    main()
