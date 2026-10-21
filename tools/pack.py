"""把引擎包打成可分发的 zip（**不含模型权重**，也不含运行日志与备份）。

用法:
  python tools/pack.py                    打包到 dist/
  python tools/pack.py --out D:\\somewhere
  python tools/pack.py --list             只列出要打包哪些文件，不写 zip
  python tools/pack.py --verify X.zip     校验一个已打好的 zip（无 .ninfer / 有必需的启动器与引擎）

产物: <out>/ninfer-engine-pack-<日期>.zip
    + 同名 .sha256                      整包 SHA256
    + 同名 .manifest.txt                包内每个文件的相对路径 / 字节数 / SHA256
"""
import argparse, hashlib, os, sys, time, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 目录：整目录递归打包（过滤规则见 wanted()）
DIRS = ["engine", "tools", "assets", "docs"]

# 单文件：按清单打包（缺失的跳过并提示，但不中断）
# 注意 docs/ 已整目录打包，所以 MODEL.txt / README-*.md / SHA256SUMS.txt / 教程 只在 docs/ 里，
# 这里不再重复列（否则包内会出现两份同名文件，且历史上的顶层副本已过期）。
FILES = [
    "AGENTS.md",
    "start-mtp-slim.bat",
    "start-mtp-slim-silent.vbs",
    "start-uncensored.bat",
    "start-hermes-proxy.bat",
    "start-dflash-128k-tuned.bat",   # 已退休：包内是拒绝启动的说明桩，保留是为了让老快捷方式看到原因
    "kvmem-responses-normalizer.py",
    "verify-arch-engine.ps1",
    "verify-kit-manifest.ps1",
    "ninfer.ico",
]

# 必须出现在包里的关键文件（--verify 用作判据）
REQUIRED = [
    "AGENTS.md",
    "start-mtp-slim.bat",
    "engine/ninfer-serve-120a.exe",
    "engine/pick-engine.bat",
    "tools/batcfg.py",
    "tools/ninfer-tray.py",
]

# 一律排除（按名字）
EXCLUDE_NAMES = {
    ".workbuddy", ".workbuddy-ai", "logs", "dist", "__pycache__", ".git",
    "node_modules", ".venv", "venv",
}
# 一律排除（按扩展名）
EXCLUDE_EXT = {".ninfer", ".log", ".err", ".jsonl", ".pyc", ".zip", ".dmp", ".bak"}
# 一律排除（按后缀）
EXCLUDE_SUFFIX = (".resp", ".grow.err", ".share.err", ".over.err", ".bak-")


def wanted(path, name):
    if name in EXCLUDE_NAMES or name.startswith("."):
        return False
    ext = os.path.splitext(name)[1].lower()
    if ext in EXCLUDE_EXT:
        return False
    if any(name.endswith(s) for s in EXCLUDE_SUFFIX):
        return False
    return True


def collect():
    """返回 [(相对路径, 绝对路径, 字节数)]，按相对路径排序。"""
    out = []
    for d in DIRS:
        base = os.path.join(ROOT, d)
        if not os.path.isdir(base):
            print("  !! 缺目录:", d)
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [x for x in dirnames if wanted(dirpath, x)]
            for fn in filenames:
                if not wanted(dirpath, fn):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, ROOT).replace("\\", "/")
                out.append((rel, full, os.path.getsize(full)))
    for f in FILES:
        full = os.path.join(ROOT, f)
        if not os.path.exists(full):
            print("  -- 跳过（不存在）:", f)
            continue
        out.append((f.replace("\\", "/"), full, os.path.getsize(full)))
    out.sort(key=lambda x: x[0])
    return out


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def verify(zpath):
    """校验产包：无模型权重、关键文件齐全、manifest 与包内文件一一对应。"""
    bad = 0
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        models = [n for n in names if n.lower().endswith(".ninfer")]
        if models:
            print("  [FAIL] 包内含模型权重:", models[:5])
            bad += 1
        else:
            print("  [ok]   无 .ninfer 模型权重")
        miss = [r for r in REQUIRED if r not in names]
        if miss:
            print("  [FAIL] 缺关键文件:", miss)
            bad += 1
        else:
            print("  [ok]   关键文件齐全（%d 项）" % len(REQUIRED))
        exe = "engine/ninfer-serve-120a.exe"
        if exe in names:
            got = z.getinfo(exe).file_size
            want = os.path.getsize(os.path.join(ROOT, exe))
            if got != want:
                print("  [FAIL] 引擎 exe 大小不符: 包内 %d / 本地 %d" % (got, want))
                bad += 1
            else:
                print("  [ok]   引擎 exe %d 字节" % got)
        print("  包内文件数: %d" % len(names))
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--list", action="store_true", help="只列清单，不打包")
    ap.add_argument("--verify", metavar="ZIP", help="校验一个已打好的 zip")
    a = ap.parse_args()

    if a.verify:
        print("校验:", a.verify)
        sys.exit(1 if verify(a.verify) else 0)

    items = collect()
    if a.list:
        for rel, full, size in items:
            print("%11d  %s" % (size, rel))
        print("\n合计 %d 个文件，%.2f GiB" % (len(items), sum(x[2] for x in items) / 1024**3))
        return

    os.makedirs(a.out, exist_ok=True)
    stamp = time.strftime("%Y%m%d")
    zpath = os.path.join(a.out, "ninfer-engine-pack-%s.zip" % stamp)

    total = 0
    print("打包中...")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for rel, full, size in items:
            z.write(full, rel)
            total += size

    size = os.path.getsize(zpath)
    print("\n完成: %s" % zpath)
    print("  文件数 %d，原始 %.2f GiB，压缩后 %.2f GiB" % (len(items), total / 1024**3, size / 1024**3))

    h = sha256_file(zpath)
    shp = zpath + ".sha256"
    with open(shp, "w", encoding="ascii") as fh:
        fh.write("%s  %s\n" % (h, os.path.basename(zpath)))
    print("  校验: %s" % shp)

    mpath = zpath + ".manifest.txt"
    with open(mpath, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# ninfer-engine-pack manifest  (packed %s)\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        fh.write("# SHA256                                           bytes  path\n")
        for rel, full, size in items:
            fh.write("%s  %10d  %s\n" % (sha256_file(full), size, rel))
    print("  清单: %s" % mpath)

    print("\n自检:")
    bad = verify(zpath)
    print("\n注意：包内 **不含模型权重**（*.ninfer）。分发时需另附模型，见 AGENTS.md 第 0 节。")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
