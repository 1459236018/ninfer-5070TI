"""从 start-mtp-slim.bat 读取唯一真相源：模型路径、引擎端口、KV 精度。

为什么必须这样做（2026-10-16 实测教训）：
  本机把**引擎让到 8095**，**预热代理占住 8094**（客户端 models.json 仍指 8094）。
  于是任何"引擎起来了吗"的判断如果打 8094，会拿到代理代替没起来的引擎回的 200，
  自动定容就会一路二分到上限并把必炸的池值写回启动器。
  => 探测引擎请一律用 engine_port()，不要硬编码端口，也不要硬编码模型名。

用法::

    import batcfg
    MODEL = batcfg.resolve_model()          # 第一个真实存在的 .ninfer
    PORT  = batcfg.engine_port()            # 启动器 --port，即引擎端口
    ARGS  = batcfg.load_bat()[0]            # 启动器里的完整参数
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_BAT = "start-mtp-slim.bat"


def bat_path(name=DEFAULT_BAT):
    return os.path.join(ROOT, name)


def read_bat_text(name=DEFAULT_BAT):
    p = bat_path(name)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="ascii", errors="replace") as f:
        return f.read()


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


def arg_value(args, key, default=None):
    for i, a in enumerate(args or []):
        if a == key and i + 1 < len(args):
            return args[i + 1]
    return default


def model_candidates(txt=None):
    """启动器里所有 set "MODEL=..." 候选（主用 + fallback），按出现顺序。"""
    txt = read_bat_text() if txt is None else txt
    out = []
    for mm in re.finditer(r'set "MODEL=([^"]+)"', txt):
        out.append(mm.group(1).replace("%ROOT%", ROOT + os.sep).replace("/", os.sep))
    return out


def resolve_model(txt=None):
    """模拟启动器的 MODEL 逻辑：按候选顺序取第一个真实存在的文件。

    启动器里是 `set MODEL=主` + `if not exist ... set MODEL=备用`，
    所以"第一个存在的候选"就是它最终会用的那一个。
    """
    cands = model_candidates(txt)
    for c in cands:
        if os.path.exists(c):
            return c
    env = os.environ.get("MODEL")
    if env and os.path.exists(env):
        return env
    if cands:
        return cands[0]
    if os.path.isdir(ROOT):
        local = sorted(f for f in os.listdir(ROOT) if f.endswith(".ninfer"))
        if local:
            return os.path.join(ROOT, local[0])
    return None


def load_bat(name=DEFAULT_BAT):
    """返回 (args, model, kv_dtype, has_vision)；读不到参数时 args 为 None。

    所有参数里的 %ROOT% / %MODEL% 都会展开成真实路径 —— 启动器里
    `--request-log-jsonl "%ROOT%logs\\requests.jsonl"` 这种写法，如果原样传给引擎，
    引擎会去开一个名叫 `%ROOT%logs\\...` 的相对路径（打不开）。
    """
    txt = read_bat_text(name)
    m = re.search(r'set "ENGINE_ARGS=(.*)"', txt)
    args = split_args(m.group(1)) if m else None
    model = resolve_model(txt)
    if args:
        root_prefix = ROOT + os.sep
        expanded = []
        for a in args:
            a = a.replace("%ROOT%", root_prefix)
            if model:
                a = a.replace("%MODEL%", model)
            expanded.append(a)
        args = expanded
        if args[0] in ("%MODEL%", '"%MODEL%"'):
            if model:
                args[0] = model
            else:
                args = args[1:]
    kv = arg_value(args, "--kv-dtype", "nvfp4")
    return args, model, kv, bool(args and "--vision" in args)


def engine_port(default=8094, name=DEFAULT_BAT):
    """启动器 --port：**引擎**监听端口（本机 8095，前面可能还有代理占 8094）。"""
    args = load_bat(name)[0]
    v = arg_value(args, "--port")
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def engine_url(port=None):
    return "http://127.0.0.1:%d" % (engine_port() if port is None else port)


def proxy_port(default=None):
    """预热代理监听端口（从托盘脚本解析；解析不到返回 default）。

    仅用于提示"客户端打的是代理、不是引擎"，不参与就绪判断。
    """
    p = os.path.join(ROOT, ".workbuddy", "tools", "ninfer-tray.py")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except OSError:
        return default
    m = re.search(r"PROXY_LISTEN\s*=\s*\(\s*\"[^\"]*\"\s*,\s*(\d+)", txt)
    if not m:
        return default
    try:
        return int(m.group(1))
    except ValueError:
        return default


def client_port(default=None):
    """客户端实际连接的端口（~/.workbuddy/models.json），仅供提示。"""
    p = os.path.join(os.path.expanduser("~"), ".workbuddy", "models.json")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return default
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("url", "base_url", "baseUrl") and isinstance(v, str):
                    mm = re.search(r":(\d+)", v)
                    if mm:
                        return int(mm.group(1))
                r = walk(v)
                if r:
                    return r
        elif isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None
    return walk(data) or default


def summary():
    """一行现状，方便任何探针在开头打印。"""
    args = load_bat()[0]
    return ("模型 %s | 引擎端口 %s | 代理端口 %s | 客户端端口 %s | KV %s"
            % (os.path.basename(resolve_model() or "?"), engine_port(),
               proxy_port(), client_port(), arg_value(args, "--kv-dtype", "?")))


if __name__ == "__main__":
    print("ROOT      :", ROOT)
    print("启动器    :", bat_path(), os.path.exists(bat_path()))
    print("现状      :", summary())
    print("引擎参数  :")
    for a in (load_bat()[0] or []):
        print("   ", a)
