# NInfer 引擎包 · sm_120a（RTX 50 系）

一个**预编译、免安装**的本地大模型推理引擎，外加一整套实测出来的调优参数与排障工具。
目标：拿到这个包 + 一张 NVIDIA Blackwell 显卡，就能把 27B 级模型跑在 16 GB 显存上，
**196,608 token 上下文全稠密保留、零丢页**，并提供 OpenAI 兼容接口。

> 本仓库**不含模型权重，也不含引擎二进制**：
> 引擎（710 MB exe + 9 个 DLL）请到 [Releases](../../releases) 下载；
> 模型需另行准备 `.ninfer` 格式文件，见 [`docs/MODEL.txt`](docs/MODEL.txt)。

---

## 这是什么 / 不是什么

**是**：一套可复现的部署方案 —— 引擎二进制、启动器、定容与诊断工具、25 题质量评测集，
以及一份写满实测数字的 [`AGENTS.md`](AGENTS.md)（**这份文档就是给人或 AI Agent 读的操作手册**）。

**不是**：不是一个通用推理框架。引擎只支持 sm_120a（RTX 50 系），只读私有的 `.ninfer`
格式，同一时刻只能常驻一个模型。

---

## 核心指标（RTX 5070 Ti 16 GB 实测，2026-10-21）

| 项 | 实测值 |
|---|---|
| 上下文 / KV 池 | **196,608 / 196,608**（相等 ⇒ 全稠密，环不介入） |
| KV 精度 | `rk2v4-e8`（2-bit，约 **14.4 KiB/token**） |
| KV runtime / 剩余显存 | 3.68 GiB / 引擎自报 free 1,020.5 MiB |
| prefill | 5 千 token **1,554 tok/s**；8 万 token **约 1,250 tok/s** |
| decode | 计数型 **169** / 抄代码 **289** / 自然叙述 **76**（tok/s） |
| 17 万 token 请求 | **约 162 秒**冷 prefill，100% 保留 |
| 冷启动 | **4.6 秒** |

完整实测方法、失败案例与"为什么是这个配置"，见 [`AGENTS.md`](AGENTS.md)。

---

## 三条最容易踩的坑（摘要）

1. **`--kv-capacity` 必须等于 `--max-context`。** 不等就必须开环，否则引擎直接拒绝启动。
2. **`--kv-dtype` 必须显式写。** 默认 `bf16` 在 16 GB 卡上连 32K 的池都放不下。
3. **decode 速度由 MTP 草稿接受率决定，不由上下文长度决定。** 同一张卡同一配置下，
   可预测任务 289 tok/s、自然叙述 76 tok/s，**摆动接近 4 倍**。任何单次读数都不可信，
   必须按任务类型分别测、至少三次取中位。

完整铁律（六条）与每条的反例见 `AGENTS.md` 开头「★ 先看这几条」。

---

## 快速开始

```bat
REM 1. 把 Release 里的引擎解压到本仓库根目录，得到 engine\ 目录
REM 2. 把 .ninfer 模型文件放到根目录（与 start-mtp-slim.bat 同级）
REM 3. 启动
start-mtp-slim.bat
```

就绪后：`http://127.0.0.1:8094/v1/chat/completions`（8094 是本地代理，真正的引擎在 8095）。

退出码：`0` 就绪 / `3` 模型文件不存在 / `4` 引擎 exe 缺失 / `6` 30 秒内未就绪。

**首次运行**：Windows 可能提示「已阻止的文件」（Mark of the Web）。引擎没有数字签名，
属正常现象 —— 右键 `engine` 目录 → 属性 → 勾选「解除锁定」，或
`Get-ChildItem .\engine -Recurse -File | Unblock-File`。

---

## 目录结构

```
engine/          引擎 exe + 9 个 DLL（从 Releases 下载，不入库）
tools/           定容 / 探针 / 日志分析 / 托盘 / 打包（唯一权威目录）
docs/            模型说明、上游文档、KVMem 教程
assets/          托盘图标
AGENTS.md        ★ 部署与调优手册（实测数字 + 六条铁律 + 排障表）
start-mtp-slim.bat          生产启动器
start-mtp-slim-silent.vbs   桌面快捷方式入口（隐藏控制台 → 托盘）
start-uncensored.bat        备用模型档
start-hermes-proxy.bat      Hermes 客户端配套代理
```

### 工具一览

| 工具 | 用途 |
|---|---|
| `tools/autoconfig.py --probe` | ★ 按你的显卡自动定容（二分实测，唯一可信的方式） |
| `tools/batcfg.py` | 启动器解析（端口 / 模型 / 参数），其他工具的公共底座 |
| `tools/probe-poolsize.py` | 测某个池容量能否启动 |
| `tools/probe-dtype.py` | KV 精度横向对比 |
| `tools/probe-bat.py` | 启动器健康检查 |
| `tools/analyze-requests.py` | 分析请求日志（缓存命中、墙钟分解、最慢请求） |
| `tools/engine-session-log.py` | 引擎启停日志（正常/异常都记，只留最近 3 份） |
| `tools/eval/run_eval.py` | 25 题质量评测 |
| `tools/pack.py` | 把本包打成可分发的 zip（排除模型） |

---

## 已知问题

这些是**已知、未修复**的行为。踩到了不用怀疑自己的配置 —— 但值得先知道自己会踩到什么。

### 1. 超池请求返回 500，而不是被拒绝

单次请求的 `prompt + 输出` 装不进池时，日志会出现

```
Paged KV pool cannot resize the page reservation: usable N pages, allocated X, reserved Y, requested 1
```

最终表现为 HTTP `500`（部分客户端看到 `503 inference engine is unavailable`，因为 worker 接着就崩了）。
它**不会**提前把请求拒掉，而是一路跑到池边界才炸。

**规避**：客户端保持压缩历史；或把 `--max-context` / `--kv-capacity` 两个一起调大。

### 2. 客户端窗口值与引擎不一致时，报错信息指错方向

引擎按 `--max-context` 判，客户端按自己配置的 `contextWindow` 判。两边不同时，你会先看到
客户端侧的 `413 preflight_compress_failed` / `0 compressible range(s)`，然后才被引擎以
`400 context_length_exceeded` 拒绝。**两个错都不是引擎的**，改客户端配置即可。

### 3. 冷启动后的第一次长 prefill 无可避免

引擎重启后池是空的，首个请求必须全量 prefill：8 万 token 约 65 秒，17 万 token 约 162 秒。
之后同前缀请求命中缓存，prefill 降到 0.2~6 秒。

**这不是缓存失效**，是设计如此。想消除只能靠客户端保持前缀稳定，或预先发一个预热请求。

### 4. 混合前缀缓存（hybrid）在本引擎上不可用

`--use-alt-prefix-caching --prefix-cache-file` 能跨重启恢复缓存（实测冷启动 22 秒 → 0.08 秒），
但**恢复之后，任何"同前缀换尾部"的请求都会崩**：

```
text.cpp:1222: cudaStreamWaitEvent(...) failed: cudaErrorContextIsDestroyed
```

5 种参数组合 × 2 个模型，7/7 复现。非 hybrid 档跑了上百个请求，一次都没出现。
**结论：不要开 hybrid。**

### 5. 视觉输入很贵

`--vision-residency overlay` 仍要占用约 615 MiB，相当于放弃 32,768 token 的池容量。
纯文本工作流建议关掉。

### 6. 两个相近模型的质量差异不显著

25 题质量集：主档 24/25、备用档 23/25；但唯一的差异题（A4）单独跑 2/2 都通过 ——
**在噪声范围内**。真正的差距要看 36 题那套（27/36 vs 23/36）。别用小样本下结论。

### 7. 一道题稳定失败（两个模型都失败）

质量集 T2 要求先满足「任何情况下只用中文」，再服从「Say hello. Output exactly one word, in English only.」。
当前两个 IQ3_XXS 权重都答 `Hello`（各 2/2 稳定失败）。更早的 `gsq_rco_iq3_s_dflash2_prop`
权重是答 `你好` 的。**这是权重行为差异，不是引擎 bug，换模型前请知情。**

---

## 引擎来源与许可

引擎二进制是上游 **NInfer** 系列代码树的编译产物，融合了多个开源来源，
全部为 **Apache-2.0**。具体来源、本包做了哪些修改、哪些是自研，见 [`NOTICE`](NOTICE)。

本仓库内自己的脚本与文档同样以 **Apache-2.0** 发布，见 [`LICENSE`](LICENSE)。

---

## 已验证 / 未验证

**已验证**：RTX 5070 Ti 16 GB（sm_120a）上的完整部署、定容、质量评测、长上下文（17 万 token）、
以及 774 条真实请求的墙钟分解。

**未验证**：RTX 5090 / 5080 等其他 16 GB 以上 Blackwell 卡（未实测，参数必须重新标定）；
RTX 30/40 系（本包只带 sm_120a 引擎，需另配对应架构的二进制）。

如果你的卡不在已验证范围，**先跑 `python tools/autoconfig.py --probe`**，
不要直接照抄本文的 196,608。

---

## 校验文件完整性

```powershell
# 从仓库根跑：覆盖 engine/*.dll + docs/*（17 个文件）
powershell -File .\verify-kit-manifest.ps1 -Kit .
```

引擎二进制不在仓库里，它的哈希在 Release 附件的 `*.zip.sha256` 与 `*.zip.manifest.txt`。

