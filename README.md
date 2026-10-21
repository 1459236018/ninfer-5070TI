# NInfer 引擎包 · sm_120a（RTX 50 系）

一个**预编译、免安装**的本地大模型推理引擎，外加一整套实测出来的调优参数与排障工具。
目标：拿到这个包 + 一张 NVIDIA Blackwell 显卡，就能把 27B 级模型跑在 16 GB 显存上，
**196,608 token 上下文全稠密保留、零丢页**，并提供 OpenAI 兼容接口。

> 本仓库**不含模型权重，也不含引擎二进制**：
> 引擎（710 MB exe + 9 个 DLL）请到 [Releases](../../releases) 下载；
> 模型需另行准备 `.ninfer` 格式文件，见 [`docs/MODEL.txt`](docs/MODEL.txt)。

> [!IMPORTANT]
> **非官方构建，按现状提供，不提供支持。**
>
> - 本仓库的引擎二进制是上游多个 Apache-2.0 项目的融合编译产物，**不是任何上游项目的官方发布**，
>   也**不提供源码**。上游/官方渠道请以其各自仓库为准，见 [`NOTICE`](NOTICE)。
> - 所有实测数字都来自**一块 RTX 5070 Ti 16 GB**。其他显卡（5090、5080、30/40 系）**未经验证**，
>   参数必须用 `tools/autoconfig.py --probe` 重新标定，直接照抄本文数值可能起不来。
> - 引擎**没有数字签名**。Windows 的 SmartScreen / Mark-of-the-Web 会提示风险，
>   这是未签名程序的常规行为，但你需要自己判断是否运行。
> - **已知存在未修复的问题**，见下方[「已知问题」](#已知问题)。其中 hybrid 前缀缓存在本引擎上**必崩**，
>   超池请求会返回 500 而不是被提前拒绝。
> - 拿去商用、分发、二次封装前，请自行确认上游许可与合规性。本仓库只保留署名，不作担保。

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

> 注意区分两种「装不下」：
> **超上下文**（`prompt` 大于 `--max-context`）→ `400 context_length_exceeded`，见下一节；
> **超池**（`prompt + 输出` 装不进 `--kv-capacity`）→ 本节说的 500。
> 本包默认 `--max-context` 与 `--kv-capacity` 相等（全稠密），**这种情形不会发生**；
> 只有把池调得比上下文小才会踩到。

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

分两块：**本包里有的文件**在仓库里校验，**引擎二进制**在分发包里校验。

### 1. 仓库文件（仓库根跑）

```powershell
# 覆盖 engine/*.dll + docs/*（17 个文件）
powershell -File .\verify-kit-manifest.ps1 -Kit .
```

正常输出（工作区是 Windows 换行时）：

```
ok=17  mismatch=0  missing=0  unparsed=0  crlf_only=0
```

正拖成 `git clone` 下来的仓库、且 git 把文本文件转成了 LF 时，输出会变成下面这样，**这同样是正常结果**：

```
CRLF-ONLY docs/Huihui-abliterated.conversion.json
CRLF-ONLY docs/README-pack.md
ok=15  mismatch=0  missing=0  unparsed=0  crlf_only=2
```

两种情况**退出码都是 0**。

> **为什么要分两段：** `SHA256SUMS.txt` 里每个哈希都是**磁盘原样字节**的（解压 zip 后必须是这个结果）；
> 而 `.gitattributes` 给文本文件标了 `text=auto`，git 内部存的是 LF，同一个文本文件因此有两个合法哈希。
> `docs/SHA256SUMS.lf.txt` 只列**文本文件**的 LF 版哈希，作为第二段比对；DLL 这类二进制永远只按原样校验，
> 绝不参与换行符换算（否则等于在比较一个不存在的文件，还可能掩盖单字节篡改）。
> 第二段只在第一段失败时才用，命中记为 `CRLF-ONLY`，**不会把被改过内容的文件放过去**。

### 2. 分发包（下载后核对）

Release `v2026.10.21.2` 的三个附件，**发布时的 SHA256**（可直接与 GitHub 上的文件比对）：

| 文件 | 字节数 | SHA256 |
|---|---:|---|
| `ninfer-engine-pack-20261021.zip` | 888,769,017 | `2C3DD29BE0C3FAFA32F75BB3708CB44D69C054AA73A4CE7099D8910BFF415DC1` |
| `ninfer-engine-pack-20261021.zip.manifest.txt` | 5,358 | `72F434D3D050778A378118DB4C07FC68F49D8CA5930F521D2F2BA33651DA31E0` |
| `ninfer-engine-pack-20261021.zip.sha256` | 99 | `9DE925410E4BC5DCB6F3D05615F4AAEA94DBD92A1C6F870F75D1F30C47BA02D8` |

一条命令核对下载下来的 zip：

```powershell
(Get-FileHash .\ninfer-engine-pack-20261021.zip -Algorithm SHA256).Hash
# 应当输出 2C3DD29BE0C3FAFA32F75BB3708CB44D69C054AA73A4CE7099D8910BFF415DC1
```

> **GitHub 附件页显示的 `847.59 MB` 是按 1024 进制算的**，与上表 888,769,017 字节是同一个东西，不是对不上。
>
> 已撤下的 `v2026.10.21.1` 里的 zip 是 888,767,512 字节、`7BE66869…`。那一版同样是合法构建，
> 但包里的 `start-mtp-slim-silent.vbs` 是旧的坏版本（托盘脚本名少一个字母，见 `AGENTS.md` 第 0 节）。
> **请以下表这一版为准。**

### 3. 分发包内部（逐文件）

zip 解开后，`.manifest.txt` 列出**包内每个文件**的 SHA256 与字节数（52 条）。核对单个文件：

```powershell
Select-String -Path .\ninfer-engine-pack-20261021.zip.manifest.txt -Pattern 'AGENTS.md'
# B76C761E93B0194015EA88C9A6AA2DB8DB5C1A09BDDE532D5AB6A3E6095A4A4E       34755  AGENTS.md

(Get-FileHash .\AGENTS.md -Algorithm SHA256).Hash
# 与上一行那个哈希不同 => 文件被改过
```

> **`.manifest.txt` 是 zip 自己的逐文件清单**（按解压后的字节算），和包内的 `docs/SHA256SUMS.txt` 是两回事：
> 后者只覆盖 `engine/*.dll` + `docs/*` 共 17 个文件，且同样按磁盘原样字节算。
> 解压后跑一次 `powershell -File .\verify-kit-manifest.ps1 -Kit .` 就同时验了这两层里属于它的那层。

**为什么写进 README**：附件可以被替换，哈希写死在仓库里就换不掉了 ——
谁改了任何一个字节，这里的三行就对不上。

