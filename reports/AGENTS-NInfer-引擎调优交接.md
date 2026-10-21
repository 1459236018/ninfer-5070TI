# NInfer 引擎调优交接文档（N 卡专用 · 交给 Agent 部署）

> **读者：接手这个引擎包 / 这台机器的下一个 AI Agent。**
> **平台：仅 NVIDIA 显卡（N 卡）** —— 引擎只链 CUDA 运行时，第一步就跑 `nvidia-smi` 读算力；
> AMD / Intel 显卡在第一步就以**退出码 3** 结束，不是调参数能解决的。
> **这份文档是部署指令，不是阅读材料** —— 从 **§0.1 部署流程** 开始照做，用 **§13 七项自检** 收尾。
> 目标：30 分钟内达到同等性能，并且**不再重走已经被排除的路**。
> 状态：2026-10-19 21:10。两条优化已落地、已实测、已验证可回滚。
> 配套：`reports/引擎逆向分析-2026-10-18.html`（686 MB 二进制的静态逆向报告）。

---

> ## ⚠️ 当前生产配置（2026-10-20 09:20 —— **先读这一块**，下文历史段落仅作参考）
>
> | 项 | 当前值 |
> |---|---|
> | 引擎 | **fusion-kvmem**（677 MB，基于 NInfer 0.11.0） |
> | 上下文 / 池 | **196,608 / 196,608**（相等 ⇒ 全稠密，环不介入、零丢页） |
> | KV 精度 | **`rk2v4-e8`**（2-bit，约 14.4 KiB/token） |
> | runtime / free | 3.68 GiB / **1.02 GiB** |
> | 实测 | 启动 4.6 s；2K decode **136.6** / 1.5 万 **127.5**（三次中位）；**17 万 token 162 秒、100% 保留** |
> | 采样 | `--presence-penalty 0 --temperature 0.7 --top-p 0.9 --top-k 20` |
>
> ★ **池必须等于上下文**；`--kv-dtype` 必须显式写。
> ★ **hybrid 前缀缓存禁用**（崩溃判据见 §5.7）。
> ★ **环的相关参数（KV_WINDOW / SCORE_* / PROTECT_* / host-kv-mib / max-shared-prefixes）当前全部未启用** ——
> 池等于上下文时它们没有意义。要开环请先读本文件顶部「如果开环」那段，并注意真实可用只有池的 ~45%。
> ★ 回滚：`.workbuddy\_trash\merge-20261019\infer-serve-120a-stock.exe` 复制回 `engine/`。


### 0.1 交给 Agent 的部署流程（照做）

用户把这个包给你部署。**按顺序做，每步都有判据，不要跳步。**

| # | 动作 | 判据 / 卡住怎么办 |
|---|---|---|
| 1 | 解压到本地目录 | 路径**不要含空格或中文**（bat 用 `%~dp0` 取根，空格仍可能出问题） |
| 2 | `nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv` | **必须 NVIDIA 卡**（AMD/Intel 直接停）。compute_cap 必须是 8.0/8.6/8.9/12.0 |
| 3 | 核对 `engine\` 里有没有对应架构的 exe | **本包只有 12.0 的 `ninfer-serve-120a.exe`**。不是 12.0 → 停下，向用户要 `-86/-89/-80` 的 exe，并读 §2.1 换 `--kv-dtype` |
| 4 | 确认模型 | **包内没有 `.ninfer`**。请用户提供，放包根命名 `swift15_iq3xxs_mtp.ninfer`，或 `set MODEL=<绝对路径>` |
| 5 | 核对 `start-mtp-slim.bat` 参数 | **先看显存和 artifact 体积**：和 §3 那个 16 GB 参考档一致就别改；不一致必须按 §3.0 重算 `--max-context` 与 `--kv-capacity`（两者必须相等） |
| 6 | 启动 | 双击 `start-mtp-slim-silent.vbs`。**注意：沙箱里 spawn `cmd.exe` 会被安全策略拦（SIGTERM）**，脚本化启动可能失败，此时请用户双击 |
| 7 | 看控制台日志 | 应有 `capacity \| KV 176,128 tokens ... \| free > 300 MiB`；**不应**出现 `[ring] content scoring ON` 或 `kvmem_score: ARMED` |
| 8 | 从托盘菜单正常退出一次 | 日志出现 `prefix cache saved: N blocks ... MiB`，且 `logs\prefix-cache.bin` 生成（>0 字节） |
| 9 | 再启动 | 日志出现 `prefix cache restored \| N blocks \| ... \| XXX ms` |
| 10 | 跑 §13 七项自检 | 七项全绿才算部署完成 |

**三条最容易犯的错：**

- ❌ 直接 `taskkill /f` 停引擎 → 缓存不写，第 9 步永远不出现。必须走托盘的优雅停止（§5.2）。
- ❌ 照抄 §3 的数字到别的卡 → 那些是 RTX 5070 Ti + IQ3_XXS 的实测值，换卡/换模型必须重标定。
- ❌ 在包根之外启动 → 模型路径与 `%ROOT%` 相对关系全乱。

---

## 1. 资产清单

```
<包根>/
  AGENTS.md                     ← 本文件
  README.md                     ← 包结构与用法
  config/
    start-mtp-slim.bat          ← 生产启动器（已改好，纯 ASCII）
    ninfer-tray.py              ← 托盘宿主（stop_engine 已改成优雅停止）
  reports/
    引擎逆向分析-2026-10-18.html  ← 686 MB 二进制的静态逆向报告
  bench/
    ab_hostkv.py                ← A/B/C 三组：host-kv 显存 + 环开/关
    ab_ring_ttft.py             ← 环开/关的首轮 prefill 与 TTFT 对比
    ab_ring_reuse.py            ← 环开/关的前缀复用对比（关键证据）
    hybrid_restart2.py          ← hybrid 的追加式复用 + 关闭探测
    restore_test.py             ← 跨重启 restore 全流程
    persist_test.py             ← 强杀后旧缓存是否仍可用
    final_verify.py             ← 最终配置（池 184320 + host 4096）端到端
    cost_probe.py               ← 池与 host-cache 的上限探测
    first_turn_opt.py           ← 首轮基线与 hybrid 初探
    midchange.py                ← 「中间块变化」下两种模式的复用差异
    _send_break.py              ← 独立发 CTRL_BREAK 的 helper（托盘同款）
  re/
    src_tree.txt                ← 从 __FILE__ 还原的 247 个源文件路径
    all_flags.txt               ← 131 个 CLI 标志全集
    str_rdata.txt               ← .rdata 全量 ASCII 字符串（31,104 条，4.9 MB，可 grep）
```

> `engine/` **在包内**（exe + 12 个 DLL）。**模型不在包内** —— `.ninfer` 按 §2 自备，
> 放进包根即可（默认文件名 `swift15_iq3xxs_mtp.ninfer`，或用 `set MODEL=D:\path\to\x.ninfer` 覆盖）。
>
> ⚠️ 本包里的 `start-mtp-slim.bat` **已去掉双模型 fallback**：它只认包根那一个 artifact，
> 不会再回退到第二个模型。源机器上的生产版本仍保留 fallback，两者仅此一处不同。

### 1.1 桌面快捷方式（包里没有 `.lnk`）

`.lnk` 内部写死目标的**绝对路径 + 卷序列号**，换机器/换目录立即失效 ⇒ 必须现场生成。
两种做法：

**A. 右键（无需依赖，推荐给人类用户）**
右键 `start-mtp-slim-silent.vbs` → 发送到 → 桌面快捷方式 → 属性 → 更改图标 → 选 `ninfer.ico`。

**B. 脚本（`tools/make_shortcut.py`，适合自动化）**

```bat
pip install pylnk3
python tools\make_shortcut.py ^
  --target  "<ROOT>\start-mtp-slim-silent.vbs" ^
  --workdir "<ROOT>" ^
  --icon    "<ROOT>\ninfer.ico" ^
  --name    "AI引擎"
```

- 默认输出 `%USERPROFILE%\Desktop`；`--outdir` 改位置；`--minimized` 最小化启动
- **必须装 `pylnk3`**：它生成 target IDList。缺失时脚本仍会写出 `.lnk`，但**没有 IDList**
  （实测 310 字节 vs 完整 575 字节，`flags 0xd6` vs `0xd7`），资源管理器解析不完整。脚本会打印
  `idlist  : SKIPPED -- pylnk3 not installed`
- 该脚本**不依赖 COM / WScript.Shell**，直接写 MS-SHLLINK 二进制格式；对 Git Bash 传进来的
  `D:/...` 路径会自动 `os.path.normpath`

**target 必须是 `.vbs` 而不是 `.bat`**：`start-mtp-slim-silent.vbs` 负责隐藏控制台 → 起托盘 → 托盘再拉 bat；
指 `.bat` 会留下控制台窗口，**关掉它等于杀掉引擎**。

**图标**：`ninfer.ico`（手工拼的 9 帧，16~256px）。引擎 exe **无图标资源**，`--icon` 指 exe 只会得到空白。

### 1.2 托盘依赖（前缀缓存的前提）

**没有托盘就没有优雅停止，也就永远写不出前缀缓存** —— 所以部署前必须先确认这条链：

```
start-mtp-slim-silent.vbs → pythonw.exe + tools\ninfer-tray.py → 启动 bat → 管理引擎
```

`infer-tray.py` 需要 **Python 3.9+** 以及 **`pystray`、`Pillow`** 两个包：

```bat
pip install pystray pillow
```

vbs 的查找顺序（包里的这版）：

1. 包内 `python\pythonw.exe`
2. PATH 里的 `pythonw.exe`
3. **都没有 → 退化成 `cmd /c start-mtp-slim.bat /silent`**（引擎能跑，但无托盘、无缓存）

⇒ 判据：启动后任务栏右下角**必须有蓝色方块图标**。没有就说明退化到路径 3 了，
此时 §5 的跨重启加速完全失效 —— 不要误判成"引擎不支持"。

> 源机器上那个 vbs 还带一个「选择模型」弹窗（它有两个 artifact 可选），本包这版**已去掉**，
> 只认 `start-mtp-slim.bat`；同时把写死的 `C:\Users\<name>\...pythonw.exe` 换成了可移植的两级查找。

---

## 2. 环境基线（接手先核对）

```bat
nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv
dir swift15_iq3xxs_mtp.ninfer
engine\ninfer-serve-120a.exe --help | more
```

必须一致：

- **compute capability 必须是 8.0 / 8.6 / 8.9 / 12.0 之一** —— 引擎自身只支持这四档：
  `Qwen3.5 family runtime requires compute capability 8.0, 8.6, 8.9 or 12.0`。
  **本包只自带 12.0（sm_120a）的 exe**；30/40 系要先补对应架构的 exe，再按 **§2.1** 换 `--kv-dtype`。
- 模型必须是 `.ninfer` 格式，且引擎认它的 artifact identity，否则报
  `artifact identity 'X' is not supported by target 'Y'`。
- **换模型之后，§3 的所有数字都必须重新标定**（权重每省 1 GiB，池可多开约 5.7 万 token）。

### 2.1 非 50 系显卡怎么配（跨架构迁移）

跨架构要过**两道关**，缺一不可：先换 exe，再换 KV 精度。

**第一关：换 exe（架构绑定，改参数解决不了）**

| compute capability | 典型卡 | 需要的 exe | 本包自带 |
|---|---|---|---|
| 8.0 | A100 | `ninfer-serve-80.exe` | ❌ |
| 8.6 | RTX 30 系（Ampere） | `ninfer-serve-86.exe` | ❌ |
| 8.9 | RTX 40 系（Ada） | `ninfer-serve-89.exe` | ❌ |
| 12.0 | RTX 50 系（Blackwell） | `ninfer-serve-120a.exe` | ✅ |

放进 `engine\` 即可，`pick-engine.bat` 会按 `nvidia-smi --query-gpu=compute_cap` 自动选。
找不到对应文件 → **退出码 4**；架构不在发布范围 → **退出码 5**。
> 不同架构的二进制**彼此不含对方的 PTX**，选错不会"变慢"，而是直接死掉或卡住。

**第二关：换 `--kv-dtype`**

共 9 个取值：`bf16(默认) · int8 · fp8 · rk8v4 · rk4v4 · rk4v4-e8 · rk2v4-e8 · nvfp4 · k8v4`

| 架构 | 卡 | 起步档（推荐顺序） | 为什么 |
|---|---|---|---|
| sm_120 | RTX 50 | **`nvfp4`** → `fp8` → `rk4v4` | 有 FP4 张量核心路径 |
| sm_89 | RTX 40 | **`fp8`** → `rk4v4` → `k8v4` | Ada 有 FP8、**无 FP4** |
| sm_86 | RTX 30 | **`rk4v4`** → `k8v4` → `rk2v4-e8` | Ampere 无 FP8/FP4，只能软件量化 |
| sm_80 | A100 | 同 sm_86 | 同上 |

实测性能（**本机 sm_120**，池 32,768，同 prompt —— 只有这一列是实测）：

| dtype | decode t/s | prefill t/s | KV runtime | 评价 |
|---|---|---|---|---|
| `nvfp4` | 140.3 | 1,625 | 1.54 GiB | 50 系首选 |
| `fp8` | **149.2** | 1,631 | 2.01 GiB | 最快，但 KV 大 30% |
| `rk4v4` | 97.3 | 1,840 | 1.65 GiB | prefill 最快，decode 最慢 |
| `rk8v4` | 96.1 | 1,870 | 1.91 GiB | 不推荐 |
| `rk2v4-e8` | 90.4 | 1,842 | 1.51 GiB | 最省，最慢 |
| `bf16` | — | — | 3.07 GiB | **16 GB 卡放不下**，会拒绝启动 |

> ⚠️ **sm_89 / sm_86 两行是按硬件能力推断的，未在真机验证**（本机只有一张 5070 Ti）。
> 上机后若该档报错，沿推荐顺序往下退一档。判据：能启动 + 日志 `capacity | KV ... | free XXX MiB` 正常。

**额外一坑：权重量化也有架构约束**

`--kv-dtype` 管的是 KV，**artifact 的权重量化是另一回事**。二进制里有一条针对权重的警告：

```
nvfp4 linear_swiglu: the A16 route is registered only through T=16, and on sm_80/86/89
A16 is the only policy available for NVFP4 weights (no NVFP4 tensor-core path), so this
artifact cannot serve a prefill chunk wider than 16 columns on this architecture.
Use a groupwise-int artifact here; an sm_120a build runs NVFP4 through AllowA4 at any width.
```

⇒ **权重量化是 NVFP4 的 artifact，在 40/30 系上 prefill 会被卡到 16 列宽**（几乎不可用），
要换 groupwise-int 的 artifact（Q4/Q5/Q8 系）。本项目用的 IQ3_XXS 正属 groupwise-int，不受此限。

**换卡后必须重做两件事**

1. **重标池上限**（权重体积决定池）：`tools/autoconfig.py --probe` 或 `tools/probe-poolsize.py <候选值>`
2. **重标算子路由**：`--device-profile calibrate`（约 45 s）或留 `auto` 让它现场标定

---

## 3. 参考配置（16 GB 卡 · 约 9.7 GiB 权重）

> **⚠️ 这一节是标定时的参考档，不是通用参数。**
> 显存或 artifact 体积不同 → 必须按 **§3.0** 重算池，否则直接起不来。

### 3.0 按显存选 artifact 与池

**硬性下限：显存 ≥ 8 GB**（低于这个连 8~14B 的 IQ3 都塞不进 KV）。
8 GB 是"能跑"的线，12 GB 起比较舒服，16 GB 是下面这套参数标定时的档位。

| 显存 | 建议权重体积 | 大致规模 | `--vision` | 预期池 |
|---|---|---|---|---|
| **8 GB** | ≤ 4.5 GiB | 10~14B，IQ3_XXS 级 | 建议关 | 20k~25k |
| 12 GB | ≤ 7 GiB | 14~20B，IQ3_XXS 级 | 可关 | 60k~70k |
| **16 GB** | ≤ 11 GiB | 24~32B，IQ3_XXS 级 | overlay | 150k~190k |
| 24 GB | ≤ 18 GiB | 32B @ IQ4 | 开 | 300k+ |
| 32 GB | ≤ 26 GiB | 32B @ Q6/bf16 | 开 | 400k+ |

**换算公式**（估算，必须实测确认）：

```
池 token ≈ (显存总量 − 桌面1.2G − 权重 − vision(0或0.6G) − 0.25G − 安全0.3G − 1.05G) ÷ 18.8KiB
```
其中 KV runtime ≈ `1.05 GiB + 18.8 KiB × tokens`（nvfp4）。**权重每省 1 GiB ≈ 多开 5.7 万 token**。

标定工具：`python tools\autoconfig.py --probe`（真实启动引擎二分搜索，1~2 分钟）
或 `python tools\probe-poolsize.py <候选值>`。
**算完必须把 `--max-context` 和 `--kv-capacity` 设成同一个数**（硬三角，见 §7）。

### 3.1 完整参数

`start-mtp-slim.bat` 的全部有效设置（16 GB 参考档）：

```bat
set "NINFER_KV_REUSE_HOSTBACKED=0"
set "CUBLAS_WORKSPACE_CONFIG=:16:8"

"%ENGINE%" "%MODEL%" ^
  --host 127.0.0.1 --port 8095 --model-id qwen3.8-27b ^
  --max-context 176128 --kv-capacity 176128 ^
  --kv-dtype nvfp4 --gdn-state-fp16 ^
  --cuda-graph-allowance-mib 192 ^
  --host-cache-mib 4096 ^
  --prefill-chunk 512 ^
  --spec mtp --draft-tokens 4 ^
  --vision --vision-residency overlay --media-cache-mib 0 ^
  --default-max-tokens 32768 --default-reasoning-effort high --default-thinking-budget 2048 ^
  --thinking-budget-message "Thinking budget reached. Stop deliberating and write your final answer now." ^
  --max-concurrency 1 ^
  --cache-taps-per-request 4 ^
  --use-alt-prefix-caching --prefix-cache-file "<ROOT>\logs\prefix-cache.bin" ^
  --presence-penalty 0 --thinking-presence-penalty 1.0 --temperature 0.7 --top-p 0.9 --top-k 20 ^
  --request-log-jsonl "<ROOT>\logs\requests.jsonl" --request-log-max-mib 256
```

**为什么池是 163840（而不是更大或更小）**：现在是**环模式** —— `--max-context 262144` 远大于
`--kv-capacity 163840`，池只装 16 万，超出的页自动降到 host（`--host-kv-mib 8192`）并按相关度检索回来。
这个组合的账是：

- **16 万以内**：请求完全装进池，走全稠密路径，decode **119.9**（与原厂 120.8 持平）
- **超过 16 万**：自动降级成检索，慢（20 万 token ≈ 7 分钟）但**能给出正确答案**，而不是崩
- 池不宜再大：176128 时 free 只有 516 MiB，实测在"缓存 restore + 大请求"组合下崩过两次；
  193024 会直接撞 TDR 红线（事件日志 `nvlddmkm Id 153`）
- 池也不宜再小：低于 131072 会让 14~16 万这一档的常见请求也开始检索，白白变慢

**不要在环模式下把 `--kv-capacity` 设成等于 `--max-context`** —— 那样环关闭，超池请求会直接失败。

**每个参数为什么在这：**

| 参数 | 理由（都是实测，不是默认值） |
|---|---|
| `--kv-dtype nvfp4` | 默认 bf16 **在 16 GB 卡上放不下**，会直接拒绝启动。nvfp4 decode 140 vs rk4v4 97，KV 也最小。**仅 50 系（sm_120）**；40 系用 `fp8`、30 系用 `rk4v4`（§2.1） |
| `--gdn-state-fp16` | 省 143 MiB |
| `--cuda-graph-allowance-mib 192` | 默认按 profile 自动算会虚报；32 时引擎持续告警超额。192 拿到诚实账本，代价 ~56 MiB |
| `--host-cache-mib 4096` | **hybrid 专用**的统一内存上限（替代 `--host-kv-mib`）。只花系统 RAM，不影响显存与池 |
| `--prefill-chunk 512` | 1024 更慢（1,197 vs 1,455）且 runtime 涨；2048 起不来；256 慢 8% |
| `--vision-residency overlay` | 默认 resident 多占 ~600 MiB；overlay 把视觉塔放主机内存，且不牺牲 `--vision-max-merged` |
| `--media-cache-mib 0` | 不留常驻媒体缓存 |
| `--default-thinking-budget 2048` | **必须明显小于客户端 max_tokens**。否则思考把整个输出预算吃光，正文 0 字符 + `finish_reason=length` |
| `--max-concurrency 1` | 引擎的 content scorer 全局只有一份 mean-K 索引；且单卡单序列最快 |
| `--use-alt-prefix-caching` + `--prefix-cache-file` | 跨重启复用（§5）。**两者必须成对** |
| `--request-log-jsonl` | 诊断用；**打不开是 FATAL**（会直接起不来），确保 `logs\` 存在 |

环境变量只剩两个：

- `NINFER_KV_REUSE_HOSTBACKED=0` —— 设 1 会在超池时 adopt 失败 → `Paged KV reservation invariant was violated` → HTTP 500。
- `CUBLAS_WORKSPACE_CONFIG=:16:8` —— 确定性 workspace。

> 已删除：`NINFER_KV_WINDOW`（环，§4）、`NINFER_KV_RING`（**这个变量根本不存在**，二进制零命中，是历史误传）、
> `NINFER_KV_RETRIEVE`、`NINFER_HOST_PAGEABLE`（都只服务环/超池场景）。

---

## 4. 优化一：关掉 KVMem 环

### 4.1 假设
`NINFER_KV_WINDOW` 把 Device KV 池当环形缓冲用：超出窗口的旧页降级到 Host tier。
但当前 **池(184,320) == max-context(184,320)**，逻辑上下文永远装得下 ⇒ 环**永远没有 demote 的活干**，只剩簿记开销。

### 4.2 实测（`bench/ab_hostkv.py`、`ab_ring_ttft.py`、`ab_ring_reuse.py`，各 3~4 次取值一致）

| 指标 | 有环 16384 | 无环 |
|---|---|---|
| 引擎显存 | 14,548~14,586 MiB | **14,357~14,372（-186~-224）** |
| 冷 prefill 21.5k | 1.40~1.44k tok/s | **1.52~1.53k** |
| 冷 prefill 10.7k | 1.59k | **1.63~1.66k** |
| 相同 prompt 命中 | 99.9% / 98~108 ms | 99.9% / **70~78 ms** |
| 前缀+尾巴 | 0% | 0%（两组一致） |
| 启动 | 5.57 s | 4.65 s |

### 4.3 为什么前缀复用不受影响（关键证据）
`ab_ring_reuse.py` 连续四次请求，两组行为**完全一致**：

```
t1 冷启动          cache 0
t2 完全相同        cache 10,728 (99.9%, turn closure)
t3 前缀+尾巴       cache 0
t4 再次相同        cache 10,728 (99.9%)
```

前缀复用靠的是 **checkpoint 目录**（日志行 `context cache | private 2 | shared 7`），与环是两套机制。

### 4.4 关掉后的空间
池可以从 184,320 提到 **196,608**（实测 OK，引擎自报 free 342 MiB）；204,800 也能起但 free 仅 107 MiB，不建议。当前**没有**提到 196,608（保持 184,320，把 186 MiB 当安全余量）。

### 4.5 回滚
在环境变量区加回 `set "NINFER_KV_WINDOW=16384"`。

---

## 5. 优化二：hybrid 前缀缓存 + 优雅关闭（★ 核心）

### 5.1 三条 KV 路径，二选一

| 路径 | 参数 | 何时写盘 | 本档是否可用 |
|---|---|---|---|
| 关闭时快照 | `--use-alt-prefix-caching` + `--prefix-cache-file` | **clean shutdown** | ✅ 当前采用 |
| 运行时磁盘层 | `--disk-kv-path` + `--disk-kv-restore` | 页被驱逐时 | ❌ 池 184k 从不驱逐 ⇒ 永不写 |
| 外部预热 | 代理回放 | 运行中 | 有效但用户已否决，勿再提 |

**hybrid 与 disk tier 硬互斥**（实测原文）：
```
--disk-kv-path configures the default prefix cache and cannot be combined with --use-alt-prefix-caching
```

### 5.2 关键发现：优雅退出一直都在，只是信号没送到

**踩过的坑（务必记住）：** 一开始以为引擎没有 clean shutdown，因为 `Ctrl+C` / `Ctrl+Break` /
`taskkill /PID`（不带 /F）全都杀不掉它，等了 120 s 也不退。**结论错了**。

反汇编证据（`SetConsoleCtrlHandler` 的 IAT 槽 `0x1d1e1f0`，唯一 call site `0x888c`，注册的 handler 在 **VA `0x78b0`**）：

```
0x78b0  sub rsp, 28h
0x78b4  sub ecx, 1              ; ctrl_type - 1
0x78b7  je  +0x32               ; ctrl_type == 1  (CTRL_BREAK)  -> 0x78eb
0x78b9  cmp ecx, 1
0x78bc  je  +0x07               ; ctrl_type == 2  (CTRL_CLOSE)  -> 0x78c5
0x78be  xor eax, eax            ; ctrl_type == 0  (CTRL_C)      -> return FALSE
        ...
0x78c5  mov rcx,[global]; test; call 0x70220 ; ExitProcess      ; 关窗口
0x78eb  mov rcx,[global]; test; call 0x70220 ; return TRUE      ; ★ 优雅退出
```

- **CTRL_BREAK(1)** → 调 `0x70220`（停止流程）→ 返回 TRUE。
- **CTRL_C(0)** → 返回 FALSE，**不处理**。所以 Ctrl+C 永远无效，不要試。
- **真正的原因**：`GenerateConsoleCtrlEvent` **只对调用方所在的控制台生效**。引擎跑在
  `start /min` 创建的独立控制台里，所以从外部直接发信号全部落空。

**正确调用序列**（`bench/_send_break.py`，托盘 `stop_engine()` 同款）：

```python
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.FreeConsole()                       # 先离开自己的控制台
if k32.AttachConsole(pid):              # 附着到引擎的控制台
    time.sleep(0.4)
    k32.GenerateConsoleCtrlEvent(1, 0)  # 1 = CTRL_BREAK_EVENT, 0 = 该控制台全部进程
```

⚠️ **发送方自己也会收到这个事件并被终止** ⇒ 必须由一个**独立短命进程**去发，
不能在主控进程里直接发（实测主控会跟着死，后续代码不执行）。

### 5.3 实测效果

```
优雅关闭：0.5 s → "prefix cache saved: 284 blocks, 4 snapshots, 642.0 MiB in 0.1 s"
重启    ："prefix cache restored | 284 blocks | 4 snapshots | 642.0 MiB | 273 ms"
首轮 18,241 token → 0.16 s（cache 18,232 = 100%）
同前缀换尾巴     → 0.16 s（99.9%，legacy 这里是 0%）
```

对照：同样的 18k prompt 在 legacy（无缓存）下首轮 **11.84 s**。**约 47 倍**。

### 5.4 代价（已压到最小，逐项实测）

| | legacy（原） | hybrid（现） |
|---|---|---|
| 池 | 184,320 | **184,320（不变）** |
| host RAM | 4 GiB | **4 GiB（`--host-cache-mib 4096`）** |
| 引擎显存 | 14,362~14,558 | 14,414 |
| 首轮（重启后） | 7.6–11.8 s | **0.16 s** |
| 磁盘 | — | 0.6–1.2 GiB（`logs\prefix-cache.bin`） |

> **早前的误判**：以为必须牺牲 10% 池（165,120）。那是 `--kv-capacity auto` 的保守值，不是上限。
> 显式写 184,320 完全没问题，与 legacy 一模一样。

### 5.5 互斥矩阵（撞上就是拒绝启动）

| 组合 | 结果 |
|---|---|
| `--use-alt-prefix-caching` + `--derive-session-keys` | ❌ `configures the default prefix cache and cannot be combined with --use-alt-prefix-caching` |
| `--use-alt-prefix-caching` + `--max-shared-prefixes` | ❌ 同上 |
| `--use-alt-prefix-caching` + `--disk-kv-path` | ❌ 同上 |
| `--use-alt-prefix-caching` + `--host-kv-mib` | ❌ `--host-kv-mib configures the default prefix cache and cannot be combined` |
| hybrid + `--host-cache-mib` | ✅ 这是正确搭配 |

### 5.6 缓存文件的持久性（重要，别误判）

- **文件在硬盘常驻**：启动只读不删，只有优雅关闭才覆盖写。
- **强杀不会破坏它** —— `bench/persist_test.py` 三轮实测：

```
R1 优雅关闭 → 456.7 MiB
R2 restore 456.7 MiB（首轮 0.18 s）→ 强杀 → 文件仍 456.7 MiB
R3 再启动 → 仍 restore 456.7 MiB，首轮 0.17 s
```

⇒ **强杀的后果是「这次的新内容不进缓存」，不是「退回全量」。**
⇒ 引擎**没有**周期性 / 崩溃安全保存（全字符串确认，只有 clean shutdown 一条路）。

### 5.7 ⚠️ 已知崩溃：hybrid 缓存攒大了会让引擎退出（2026-10-19 抓到日志）

**症状**：引擎启动正常（`prefix cache restored | N blocks | ... GiB`），但**第一个请求就退出**，
Application 日志里没有任何崩溃记录（不是访问违例）。

**日志铁证**（必须靠 `NINFER_CAPTURE_LOG=1` 才会留下，否则随控制台窗口一起消失）：

```
prefix cache restored | 1,512 blocks | 15 snapshots | 2.88 GiB | 1.4s
req#1 started | ... | tools 24
.../src/models/qwen3_5/execution/text.cpp:1222:
  CUDA_CHECK(cudaStreamWaitEvent(ctx_.stream, layer_ready_[layer], 0))
  failed: cudaErrorInvalidResourceHandle
```

**对照实验（同一套参数、同一个 43K token 请求）**：

| 缓存状态 | 结果 |
|---|---|
| 2.88 GiB / 15 snapshots | **崩** |
| 空缓存 | **OK 34.9 s** |

**根因**：hybrid 快照只增不删，攒到 GB 级后 restore 会让某层的 CUDA event 句柄失效。
**与显存、与请求大小、与代理都无关** —— 不要往池容量上找。

**判据**：日志出现 `CUDA_CHECK ... failed` ⇒ 直接删 `logs\prefix-cache.bin` 重启。

**预防（三者选一或叠加）**：
1. `--cache-taps-per-request 2`（本包默认，原来是 4）
2. 定期删除 `logs\prefix-cache.bin`（它本来只是"加速剂"，删了不影响功能）
3. 关掉 `--use-alt-prefix-caching` 回 legacy 缓存 —— 牺牲跨重启加速换稳定

> 这个坑在**原厂引擎和社区分支上都存在**（同一套源码血统），换引擎不能规避。

### 5.8 容量换算与调参

- 缓存 ≈ prompt 的 KV（nvfp4 ≈ 18.8 KiB/token）+ 快照（每份 74.8 MiB，通常 3–4 份）
- 18k prompt ≈ 642 MiB；36k prompt ≈ 1.1 GiB
- `--host-cache-mib` **只花系统内存**，不影响显存与池（实测 2/4/8 GiB 显存 14,412~14,423，池一律 184,320）
- 4 GiB 可同时装 2–3 个不同前缀；8 GiB 更多（启动慢 ~2 s）

---

## 6. 引擎内部结构（逆向结论，详见 HTML 报告）

### 6.1 二进制
- PE32+ x86-64，ImageBase `0x140000000`，686,013,440 B，构建 2026-10-08 10:32:39 UTC
- **无 Authenticode 签名**（安全目录为空）、**无自校验代码** ⇒ 技术上可 patch，但见 §12 为何不值得
- 8 个节：`.text` 30.5 MB / `.rdata` 7.8 MB / `.nv_fatb` **641 MB** / 其余零碎
- `.nv_fatb` 里 **384 个 fatbin 条目、354 个 cubin** —— 同一套 GEMM 按「8 种量化 × 7 种调度 × 形状常量」手工展开
- 无导出表、无 PDB、无 RTTI ⇒ **无符号**，函数级只能靠字符串 xref 反推

### 6.2 源码树（247 个 `__FILE__` 还原，见 `re/src_tree.txt`）
```
src/calibration/device_calibration.cu   逐卡算子路由标定
src/core/                               paged_kv_cache / cyclic_kv_cache / decode_graph /
                                        linear_attention_state / evictable_weight_pool
src/models/qwen3_5/                     program(prefill|decode|graphs|structured_round)
                                        prefix(hybrid_cache) / storage(context|disk_tier)
                                        speculative(mtp) / execution(text|vision|draft)
src/ops/  ≈180 个 kernel 源文件          按 量化格式 / 调度 / 形状 三层展开
src/runtime/engine/model_instance.cpp
third_party/                            ggml-quants（K-quant 反量化）、xgrammar（结构化输出）
```

### 6.3 定位
**只服务 Qwen3.5 / 3.6 家族**（要求 ≥1 个 full-attention 层；带 sparse_moe 与 gated_delta_net）。
形状特化到 `n248320_k5120`（词表 248,320 × hidden 5,120）。artifact 格式 **v3**（v2 需官方升级脚本）。

### 6.4 API
- OpenAI Chat Completions `/v1/chat/completions`、`/v1/models`
- OpenAI Responses `/v1/responses`（含 `/{id}`、`/input_items`、`/input_tokens`、`/compact`、`/cancel`）
- **Anthropic Messages** `/v1/messages`、`/v1/messages/count_tokens`
- 运维 `/health`、`/metrics`、`/v1/load`
- `/metrics` 8 个计数器：`requests_total`、`requests_admitted`、`prefix_cache_hit_tokens_total`、
  `reused_prompt_tokens_total`、`draft_tokens_total`、`draft_accepted_tokens_total`、
  `context_cache_exhausted_requests_total`、`uptime_seconds`

### 6.5 内置硬件档（`--device-profile auto` 用）
`3090-sm86(82SM)`、`4080S-sm89(80)`、`4090-sm89(128)`、`5090-sm120(170)`、`RTX PRO 6000 Blackwell ×3(sm120,188)`。
**5070 Ti 不在表内** ⇒ 每次走现场标定并写用户缓存（`--device-profile-path` 可固定）。

### 6.6 三层 KV 与打分器
Device（paged，**64 token/页**）→ Host（`--host-kv-mib`/`--host-cache-mib`）→ Disk（`--disk-kv-path`）。
`NINFER_TERNARY_KVMEM_SCORE` 用 **query 尾部 64 token** 给 KV 页打分决定捞回哪些（实测 6/6 vs 词法 IDF 4/6）。
作者原话：**QUERY_TAIL 永远 64，不许加宽**。全局只有一份 mean-K 索引 ⇒ 必须 `--max-concurrency 1`。

> ⚠️ **适用边界**：SINK / RETRIEVE_SHARE / NEIGHBOR_PAGES / content scorer **只在「prompt 超池」时触发**。
> 本档池 184,320、prompt 峰值 ~68k ⇒ **这些旋钮全是空转**，别去调。

---

## 7. 红线（违反必崩或静默答错）

1. **`--kv-capacity` 不得大于 `--max-context`** —— 否则拒绝启动：
   `FATAL, kv_capacity is outside the usable range for max_context and max_concurrency`
2. **`--max-context` 就是客户端看到的 `context_window`** —— 设大了客户端会一路聊到撞墙才崩。
   ⇒ 两者必须相等，要加就一起加。
3. **单请求需要 prompt + 输出一起装进池** —— 顶到上限时：
   `Paged KV pool cannot resize the page reservation: usable N pages ...`
4. **`NINFER_KV_REUSE_HOSTBACKED=1` 会崩** —— 超池 adopt 失败 → `Paged KV reservation invariant was violated` → HTTP 500。
5. **`--kv-dtype` 必须显式写** —— 默认 bf16 在 16 GB 卡上连 32,768 都放不下（3.07 GiB 纯 KV）。
   非 50 系的起步档见 **§2.1**。
6. **超池 + 任何前缀共享 = 崩**（`Paged KV single-page materialization exceeds reservation`）。
   出事先把共享关掉，再考虑加池。
7. **`--default-thinking-budget` 必须明显小于客户端 max_tokens** —— 否则正文 0 字符。
8. **`--request-log-jsonl` 打不开是 FATAL**，不是警告 —— 确保目录存在、文件不锁。
9. **超池会静默答错**：prompt 中段丢失、HTTP 200、答案看起来合理但错。引擎自己的告警原文：
   `the MIDDLE of such a prompt has been measured to go missing ... the root cause is not localized`。
10. **`.bat` 必须纯 ASCII** —— 一个非 ASCII 字节就会让 cmd 解析整行失败。

---

## 8. 已证伪清单（别再试，全部有实测记录）

**引擎参数：**
`--fast-prefill-kernel`、`--prefill-chunk` 1024/2048/256、`--prefill-cublas`、
`--disk-kv-path`+`--disk-kv-restore`（本档不驱逐 ⇒ 永不写盘；且与 hybrid 互斥）、
`REUSE_HOSTBACKED=1`、`--draft-tokens 16`、`--mlp-a8-decode`、`--kv-lease-growth`、
`--lm-head-q4` / `--lm-head-q6` / `--embedding-q4` / `--embedding-q6`
（artifact 拒绝：`requires text/output_head (token_embedding) to be stored as row-split Q8_G32`）、
`--wddm-evictable-budget`（需 `NINFER_D3D12_RESIDENCY=ON` 构建）、
`--device-profile calibrate`（45 s 启动换 ±1.7% 噪声）、
`--lm-head-draft`（更慢，+0.29 GiB）、`--mtp-attention-window 512`（噪声）、
`--adaptive-mtp`（与固定 draft 4 同为 180 tok/s）、
`--auto-long-anchors`+rolling+`--thorough-admission-search`+`--release-diverged-checkpoints`、
`--host-kv-mib` 降容（4096→512 只差 5 MiB 显存）、NPU。

**方向性结论：**
- prefill 已到算子极限（641 MB fatbin 按形状手工展开，无余量）
- 外部预热代理有效（0.11 s / 100%）但用户判定复杂度不值 ⇒ **不要再提**
- 冷启动那十几秒**只能靠 §5 的方案搬家**，不能靠"算得更快"

---

## 9. 复现实验指南

### 9.1 标准测量流程（所有脚本共用）
```python
p = subprocess.Popen([EXE] + ARGS, cwd=ROOT, env=env,
                     stdout=logfile, stderr=subprocess.STDOUT,
                     creationflags=0x00000010)      # CREATE_NEW_CONSOLE（要发信号时必须）
# 轮询 http://127.0.0.1:8095/v1/models 直到 200
# 读 nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits
# 关键判据在引擎控制台日志里：capacity | / context cache | / prefix cache / req#N done
```

### 9.2 判据速查

| 想确认 | 看哪行 |
|---|---|
| 池是否真的开这么大 | `capacity \| KV 176,128 tokens ... runtime 4.30 GiB \| free XXX MiB` |
| 环是否真关掉 | 不应出现 `[ring] content scoring ON` 或 `kvmem_score: ARMED` |
| 前缀缓存是否恢复 | `prefix cache restored \| N blocks \| ... \| XXX ms` |
| 首轮是否命中 | `req#1 done ... cache 18,232 (100.0%, shared prefix) \| TTFT 70 ms` |
| 是否优雅退出 | 退出码 0，且日志有 `prefix cache saved: N blocks ...` |

### 9.3 发信号的标准姿势
见 §5.2。**必须用独立进程**（`bench/_send_break.py`）。

---

## 10. 回滚

| 改动 | 回滚方式 |
|---|---|
| 关环 | 环境变量加回 `set "NINFER_KV_WINDOW=16384"` |
| hybrid 缓存 | 恢复 `--host-kv-mib 4096`，删 `--use-alt-prefix-caching` / `--prefix-cache-file` / `--host-cache-mib`，加回 `--max-shared-prefixes 7 --derive-session-keys` |
| 托盘优雅停止 | 把 `stop_engine()` 改回 `taskkill /f /im ninfer-serve-120a.exe` |
| 全部 | 包内 `config/` 是改好的版本；原始备份在源机器的 `.workbuddy/_trash/{ring-off,hybrid}-20261019/` |

回退后性能会回到：首轮 7.6–11.8 s、显存 +186 MiB、prefill 1.40–1.44k tok/s。

---

## 11. 环境坑（Windows / 沙箱 / 编码）

- **沙箱不允许 `CREATE_BREAKAWAY_FROM_JOB`** ⇒ 命令行起的常驻进程会在调用结束时被回收。
  想留一个常驻引擎，只能让用户双击桌面快捷方式（explorer 启动不受限）。
- **bash 里 spawn `cmd.exe` / `wscript.exe` 会被安全策略拦**（SIGTERM）⇒ 用 Python 直调 exe；
  也意味着**无法脚本化执行 .bat**，改完 bat 必须让用户双击验证。
- `tasklist` 输出是 **GBK**；Git Bash 里 `tasklist //fi` 会漏报 ⇒ 用 Python 跑 `/fo csv /nh` 再按 GBK 解码。
- 显存读数**只用 `nvidia-smi`**（WMI 高报约 1.8 倍）。共享显存**对本引擎完全不可用**。
- 删除参数要区分**带值参数**与**无值开关**：删 `--derive-session-keys` 时若用「删 key+value」的写法，
  会把下一个参数的值变成裸值 → `unknown argument: 0`（本次实测踩过）。
- `Global\` 内核对象建不了 → 用 `Local`。`.vbs` 是 UTF-16，改完要补 BOM。
- 引擎**不写日志文件**，日志只在它的控制台窗口里；脚本里用 `stdout=` 重定向到文件即可完整捕获。

---

## 12. 未解与边界

| 问题 | 现状 |
|---|---|
| 能不能 patch 二进制换收益 | 技术可行（无签名/无自校验），但**没有正收益目标** —— 瓶颈是硬件 FLOPs、显存容量、artifact 量化格式，都不在"软件开关"上 |
| 运行中实时写盘（磁盘层） | hybrid 与 disk tier 互斥；legacy 的 disk tier 在池 184k 下永不触发 |
| 周期性/崩溃安全保存 | 引擎没有这个选项（全字符串确认） |
| 冷启动的第一次 prefill | 无法消除，只能靠 §5 的快照"搬家" |
| 函数级伪代码 | 686 MB 无符号二进制，未做全量反编译；只做了定点反汇编（handler、IAT 交叉引用） |
| 多卡 / 流水并行 | 引擎有 `--devices` / `--stage-layers`，但部分算子明确报 `is not yet supported with a multi-device --devices split`；未实测 |

---

## 13. 交接自检清单

- [ ] `nvidia-smi` compute_cap 是 8.0 / 8.6 / 8.9 / 12.0 之一，且 `engine\` 里有对应架构的 exe
- [ ] **显存 ≥ 8 GB**，且按 §3.0 选对了 artifact 体积（否则池开不出来）
- [ ] **托盘图标（蓝色方块）出现** —— 没有就说明 pythonw/pystray 缺失，退化到无缓存模式（见 §1.2）
- [ ] 模型文件存在，启动日志出现 `artifact` 与 `weights ready | 9.71 GiB`
- [ ] 启动后日志有 `capacity | KV 176,128 tokens ... free > 300 MiB`
- [ ] 日志**没有** `[ring] content scoring ON` / `kvmem_score: ARMED`
- [ ] 正常退出一次，`logs\prefix-cache.bin` 出现（>0 字节）
- [ ] 再次启动，日志出现 `prefix cache restored`
- [ ] 首轮 TTFT < 1 s（且 `cache` 百分比高）

七项全绿 ⇒ 配置正确，性能已达标。
