# NInfer 引擎包 — 给 AI Agent 的部署与调优指南

> **这份文件是写给 AI Agent 读的。** 目标：拿到本包 + 用户的一张 NVIDIA 显卡，
> 你自己算出可用参数、把引擎跑起来，并在出问题时按判据定位。
>
> 人类用户请直接双击 `start-mtp-slim.bat` 或桌面快捷方式，不需要读这份文件。

> **文档版本：2026-10-21。** 本包由本机（RTX 5070 Ti 16 GB）实测得出，
> 所有数字都标注了测量日期与条件。**换卡、换模型、换引擎版本后这些数字全部失效，必须重测。**

---

## ★ 先看这几条（2026-10-21 实测，RTX 5070 Ti 16 GB）

当前生产档跑的是 **fusion-kvmem 引擎 + `rk2v4-e8` 全稠密**：池等于上下文，环不介入，零丢页。

| 项 | 实测值 | 测量条件 |
|---|---|---|
| 引擎 | `ninfer-serve-120a.exe` = **fusion-kvmem**（710 MB，基于 NInfer `0.11.0` 线） | 2026-10-20 07:36 构建 |
| 上下文 / 池 | **196,608 / 196,608**（相等 ⇒ 全稠密，环不介入） | `--help` 无 `NINFER_KV_WINDOW` |
| KV 精度 | **`rk2v4-e8`**（2-bit，每 token 约 14.4 KiB） | 引擎 `capacity` 行 |
| 页 / runtime / free | 3,072 / 3,072 页；runtime **3.68 GiB**；free **1,020.5 MiB** | 2026-10-21 10:31 启动 |
| 模型常驻 | 权重文件 10.33 GiB，引擎自报常驻约 **9.71 GiB** | 两个模型档一致 |
| prefill | 5 千 token **1,554 tok/s**；8 万 token **约 1,250 tok/s** | 三次中位，带随机前导避开缓存 |
| decode | 计数型 **169**；抄代码 **289**；自然叙述 **76**（tok/s） | 见下面第 5 条铁律 |
| 17 万 token | **约 162 秒**冷 prefill，100% 保留、无 `exceeds` 警告 | 全量 prefill，缓存 0% |
| 冷启动 | **4.6 s**（标定路由）／**49.6 s**（强制重新标定路由） | 路由表命中与否 |

**六条通用铁律（违反任意一条都会得出错误结论或让引擎起不来）：**

1. **池必须等于上下文**（`--kv-capacity == --max-context`）。不等就必须开环，否则引擎拒绝启动。
2. **`--kv-dtype` 必须显式写** —— 默认 `bf16` 在 16 GB 卡上连 32K 的池都放不下。
3. **free 显存必须留在 600 MiB 以上** —— 低于这个数，系统 DWM 需要显存时可能触发
   TDR（`nvlddmkm` Id 153）→ 桌面黑屏且无法恢复。这是本机踩过的真实事故。
4. **hybrid 前缀缓存禁用** —— `--use-alt-prefix-caching` 恢复后，"前缀相同、尾部不同"
   的请求会在 `cudaStreamWaitEvent` 处报 `cudaErrorContextIsDestroyed`，连接直接重置。
   已用 6 种参数组合各测一遍，**全部崩、签名逐字一致**，与模型无关。
5. **decode 必须按任务类型分别测，且至少三次取中位** —— decode 速度由 MTP 草稿接受率决定，
   不由上下文长度决定。同一张卡同一配置：可预测任务接受率 96% ⇒ 289 tok/s，
   自然叙述接受率 36% ⇒ 76 tok/s，**摆动接近 4 倍**。
   用单次样本或单一任务做配置对比会得出**相反**结论（本机误判过两次）。
6. **测 prefill 必须每次换一段随机前导** —— 否则第二次起全部命中前缀缓存，
   会测出"1 万 token 只要 0.1 秒"的假读数。

**为什么用 2-bit KV（而不是 nvfp4 + 环）**：nvfp4 下环模式的 `evidence` 桶固定只占池的 45%
（分配比例硬编码，`EVIDENCE_SHARE` / `_AUTO` / `_RESERVE` 四个变量实测全部无效），
17 万 token 实际只能保留 47%，且 prefill 掉到 542。换 `rk2v4-e8` 后池扩到 196,608，
整个请求装得下，**直接不需要检索**：真实可用 12.7 万 → 19.6 万，17 万 token 从 318 秒降到 162 秒。
质量抽查（常识 / 推理 / 3 万 token 中段回忆）无损失。

> **附：如果想开环（把上下文标到 26 万）** —— 池小于上下文时才需要，注意事项：
> **环由 `NINFER_KV_WINDOW` 这一个变量单独打开**（`NINFER_KV_RING` 在二进制里
> **零命中**，从来不是真变量，别照着旧文档设它）。
> `NINFER_KV_WINDOW=<池>` + `NINFER_HOST_PAGEABLE=1` 打开环；`--max-shared-prefixes 0` 必带；
> `--host-kv-mib` 与 `--host-cache-mib` **互斥**（同时写启动即拒）；
> `NINFER_TERNARY_KVMEM_SCORE_BUDGET=131072` 必须设（默认只给 512 页，丢 84%）；
> `NINFER_KV_PROTECT=1` + `PROTECT_FRAC=5`（**整数百分比**，写 0.05 会启动失败）；
> `NINFER_KV_NEIGHBOR_PAGES` 实测无效。
> 但记住：环模式下真实可用只有池的约 45%，26 万标称 ≈ 12 万可用 —— 不如全稠密的 19.6 万。

---

## 0. 这个包是什么

**包内不含模型权重**（`*.ninfer` 一律排除）。引擎本身是**预编译的独立程序**：

- 纯 CUDA，**只支持 sm_120a**（Blackwell：RTX 50 系）
- 模型是私有的 `.ninfer` 单文件格式（**本包不含模型**，需另配）
- 提供 OpenAI 兼容接口：`http://127.0.0.1:8094/v1/chat/completions`
  （8094 是本地代理，真正的引擎在 8095，见 `tools/warmup_proxy.py`）
- **同一时刻只能跑一个引擎实例**（一个模型常驻）

### 关于"不可改代码"（2026-10-21 补充，对想深挖的人有用）

引擎二进制里**确实带 PTX**（`.nv_fatb` 段，404 个模块，zstd 压缩，解压后 3.49 GiB，
17,665 个 kernel 入口）。`pip install nvidia-cuda-nvcc==13.4.92` 拿到 ptxas 13.x 后
**可以编译、可以塞回去**（已验证：把某模块的 cubin 去掉，驱动会用 PTX 现场 JIT，引擎照常 4 秒就绪）。

**但实测不建议改**：decode 路径 99.5% 的静态指令集中在同一个注意力算子族，
里面 `mma.sync` 只占 1.3%，大头是 fragment 打包与地址计算 —— 已是手调流水。
要提速就得重写 tiling 并重新标定 404 个模块的路由表，等于重写推理引擎。
**127 个命令行参数、138 个 `NINFER_*` 环境变量、50 键路由表全部扫过，提速都在噪声内（±1%）。**

**真正的 2 倍差距在任务形状，不在 kernel**（见铁律第 5 条）。

```
engine/                    ninfer-serve-120a.exe + 9 个 DLL（不要动）
start-mtp-slim.bat         ★ 生产启动器（长会话 / 省显存档）
start-uncensored.bat       备用模型档启动器（Huihui abliterated）
start-hermes-proxy.bat     Hermes 客户端配套（旁路代理）
start-mtp-slim-silent.vbs  桌面快捷方式入口（隐藏控制台 → 托盘）
start-dflash-128k-tuned.bat  已退休（2026-10-16）：模型缺失 + 违反硬三角，
                             现为拒绝启动的说明桩（exit /b 5）
tools/autoconfig.py        ★ 按用户显卡自动定容（本文件第 4 节会用到）
tools/batcfg.py            ★ 启动器解析（端口 / 模型 / 参数，唯一真相源，其他探针都靠它）
tools/probe-poolsize.py    池上限标定探针
tools/probe-bat.py         启动器健康检查
tools/probe-dtype.py       KV 精度横向对比（显存 + 速度）
tools/probe-vram.py        验证「上下文长度是否吃显存」
tools/probe-reuse-live.py  连续对话的前缀复用检测（不动运行中的引擎）
tools/read-engine-console.py   读运行中引擎的日志（含折行拼接）
tools/start-engine-trace.py    带请求日志启动引擎（诊断用）
tools/engine-session-log.py    ★ 引擎启停日志（正常/异常都记，只留最近 3 份）
tools/analyze-requests.py      ★ 分析 requests.jsonl（缓存命中 / 墙钟分解 / 最慢请求）
tools/ninfer-tray.py       托盘宿主（Windows 托盘图标 / 启停日志接线）
tools/warmup_proxy.py      8094 本地代理（请求清洗；预热默认关闭）
tools/eval/                25 条质量评测集（cases.json + run_eval.py，支持 --model/--tag）
tools/pack.py              把本包打成可分发的 zip（排除 .ninfer）
docs/                      引擎作者与社区文档
README-pack.md             原始包说明（英文，含引擎身份表）
README-128k.txt            历史存档：已废弃的 dflash2 / 128k 档位，**里面的数字一律不要照抄**
教程-KVMem与短测-给agent与使用者.md   引擎作者写的红线教程
```

> **`tools/` 是唯一权威目录。** 旧版本曾把托盘脚本放在 `.workbuddy/tools/`，
> 导致改了 `tools/` 却不生效。**改任何脚本前，先确认 VBS / bat 实际调用的是哪个路径。**

### 关于模型（重要）

**模型不在包内，且格式必须是 NInfer 私有的 `.ninfer`** ——
GGUF / safetensors / PyTorch / ONNX 一律读不了（那是别的引擎的格式）。

选模型只要满足三条：格式是 `.ninfer`、**引擎认它的 artifact identity**、
**权重装得进用户的显卡**。
**不必用某个特定模型** —— 候选清单、各自的 SHA256 与体积见 **`MODEL.txt`**。

- 权重体积直接决定池上限：**权重每省 1 GiB，池就能多开约 5.7 万 token**
- 引擎不认某个 artifact 时，日志会写
  `artifact identity 'X' is not supported by target 'Y'`
- **换模型之后，第 3 节的所有数字都必须重新标定**（必须跑 `tools/autoconfig.py --probe`）
- `MODEL.txt` 第二节记的是本机当前在用的那一个 —— 那只是**一个可用示例，不是强制要求**

---

### 支持的显卡架构（先确认这张卡能不能用）

引擎**按架构分发**。启动器会调用 `engine\pick-engine.bat`，逻辑是：

1. 跑 `nvidia-smi --query-gpu=compute_cap` 读显卡的 compute capability
2. 按结果选 exe：

   | compute capability | 对应显卡 | 需要的 exe |
   |---|---|---|
   | 8.6 | RTX 30 系（Ampere） | `ninfer-serve-86.exe` |
   | 8.9 | RTX 40 系（Ada） | `ninfer-serve-89.exe` |
   | 12.0 | RTX 50 系（Blackwell） | `ninfer-serve-120a.exe` |

   **本包只带了 12.0 那一个 exe。** 要用 30 / 40 系，需把对应架构的 exe 放进 `engine\`，
   否则 `pick-engine.bat` 会以退出码 4 拒绝。

3. 不在上表中的架构 → **退出码 5**

#### ★ AMD 显卡完全不能用

三层原因，任意一层都足以致命：

1. **第一步就跑 `nvidia-smi`** —— AMD 卡上不存在这个命令，脚本直接报
   `could not read the GPU compute capability` 并以**退出码 3** 结束，
   连架构判断都到不了
2. 引擎的可执行文件**只链 CUDA 库**（`cudart64_13` / `cublas64_13` / `cublasLt64_13`），
   AMD 没有对应的运行时
3. `.ninfer` 是 NInfer 私有的单文件格式，**只有这个引擎能读** ——
   换到 AMD 等于换掉整套技术栈，不是调参数能解决的

（另：不同架构的二进制**彼此不含对方的 PTX**，选错不会自动退化，而是直接死掉或卡住 ——
这是 `pick-engine.bat` 注释里写明的。）

#### `pick-engine.bat` 退出码（排障用）

| 码 | 含义 |
|---|---|
| 0 | 已选中引擎 |
| 3 | 读不到显卡（没装 NVIDIA 驱动 / 不是 N 卡） |
| 4 | 认得显卡，但对应架构的 exe 不在 `engine\` 里 |
| 5 | 架构不在发布范围（只有 8.6 / 8.9 / 12.0） |

> 支持用环境变量 `NINFER_AGENT_CC` 覆盖探测结果（便于在没有对应显卡的机器上测试选择逻辑）。
> 脚本里还引用了 `docs\02-*.md`（troubleshooting）与 `docs\11-*.md`（per-card differences），
> **这两份文档不在本包内**，若手上有完整 kit 值得一并参考。

---

## 1. 三十秒跑起来

```bat
cd /d <包目录>
start-mtp-slim.bat
```

脚本会：检查模型 → 检查引擎 → 启动引擎（独立控制台窗口，最小化）→
轮询 `GET /v1/models` 最多 30 秒 → 打印 `[ok] engine ready`。

**退出码**（排障第一步永远是这个）：

| 码 | 含义 |
|---|---|
| 0 | 就绪 |
| 3 | **模型文件不存在**（`REFUSE: model not found`）—— 最常见 |
| 4 | `engine\ninfer-serve-120a.exe` 缺失 |
| 6 | 30 秒内未就绪（通常是显存不足，或某个参数非法） |

健康检查（不打扰用户、不开窗口）：

```bash
python tools/probe-bat.py
```

---

## 2. 硬约束：改任何参数前必须知道这三条

实测（RTX 5070 Ti 16 GB）得到的**三角约束**，违反任意一条都会失败：

| # | 约束 | 违反的后果 |
|---|---|---|
| **(a)** | `--kv-capacity` **不得大于** `--max-context` | **引擎拒绝启动**：<br>`FATAL, kv_capacity is outside the usable range for max_context and max_concurrency` |
| **(b)** | `--max-context` 就是 `/v1/models` 报给客户端的 `context_window` | 客户端以为还能聊，一路加长会话，直到撞破池边界 |
| **(c)** | 一个请求需要 **prompt + 输出** 都装进池 | 池满：`Paged KV pool cannot resize the page reservation: usable N pages, allocated X, reserved Y, requested 1` |

**(a) + (b) 逼得两者必须相等**；此时 (c) 只会在「prompt 顶到上限、没给答案留空间」时炸。

> **所以：`--max-context` 和 `--kv-capacity` 一定要设成同一个数。**
> 想知道能不能开更大 —— 只能**两个一起加**，不能只动一个。

其他必须保留的参数（每一个都有实测理由，删了会出问题）：

| 参数 | 为什么不能删 |
|---|---|
| `--kv-dtype <T>` | **引擎默认是 `bf16`，在 16 GB 卡上连 32,768 的池都放不下，会直接拒绝启动** |
| `--gdn-state-fp16` | 省 143 MiB 显存（state 用 FP16）。实测有效 |
| `NINFER_KV_REUSE_HOSTBACKED=0` | 环境变量。设 1 会让超池时 worker 崩溃（`Paged KV reservation invariant was violated` → HTTP 500） |

> **★ 2026-10-21 更正**：旧版本文在这里的 `NINFER_KV_RING=1` 与
> `NINFER_KV_WINDOW` / `RETRIEVE` / `HOST_PAGEABLE` **不是必留项**。
> - `NINFER_KV_RING` 在二进制里**零命中**，从来不是真变量（别照旧文档设它）
> - `NINFER_KV_WINDOW` 是**开环开关**：池等于上下文时（当前生产档）**必须不设**，
>   设了才会去开环、去丢页
> - 这三个都是「主机内存 KV 层」的开关，只在开环 + 大 host tier 时才需要；
>   生产档 `--host-kv-mib` 也没设，host KV 一直是 0 B

---

## 3. 显存账本与容量公式

### 3.1 当前生产档的真实账本（2026-10-21 实测）

```
16,303 MiB  显卡总量
  -760 MiB  桌面/系统占用（dwm、浏览器等，**会变**）
-11,571 MiB 模型权重（文件 10.33 GiB；引擎自报常驻 9.71 GiB，但 nvidia-smi 口径是 11.5 GiB）
 -3,768 MiB KV 池 runtime（196,608 token @ rk2v4-e8，3,072 页）
   -204 MiB 引擎其他开销
=    ≈0 MiB 引擎自报 free（**1020.5 MiB**，见下）
```

引擎日志自己写的是：

```
capacity | KV 196,608 tokens, rk2v4-e8, explicit | pages 3,072/3,072 | runtime 3.68 GiB | free 1020.5 MiB
```

> **两个 free 不一样，别混。** 引擎的 `free` 是它自己留的余量口径（1020.5 MiB）；
> `nvidia-smi` 看到的 `memory.free` 通常只有 **800 MiB 左右**。
> 排障与红线判定**一律用 `nvidia-smi` 的口径**（铁律 3：≥ 600 MiB）。

### 3.2 公式（按 dtype 分别拟合，**只能用来自估起点**）

```
每 token KV 开销 = runtime ÷ 池容量       （同一 dtype 下近似线性）
池上限 ≈ (显存总量 − 桌面 − 权重 − 其他开销 − 安全余量) ÷ 每 token 开销
```

本卡实测的每 token 开销：

| dtype | 每 token KV | 依据 |
|---|---|---|
| `rk2v4-e8` | **约 14.4 KiB** | 196,608 token → runtime 3.68 GiB（2026-10-21） |
| `nvfp4` | 约 18.8 KiB | 106,496 token → runtime 2.816 GiB（2026-10-11） |
| `fp8` | 约 24.5 KiB | 32,768 token → 2.01 GiB（`probe-dtype.py`） |

即 **`rk2v4-e8` 每 +8,192 token ≈ +118 MiB**；`nvfp4` 是 ≈ +150 MiB。

### ⚠️ 公式只是起点，**必须实测验证**

公式是拟合值，实际还受驱动、桌面波动、CUDA Graph 额度、页对齐影响。
**永远用第 4 节的流程实测确认。** 本机已出现过「公式算 21 万、实测 212,992 就 free 0」的情况。

### 3.3 已实测的对照数据（RTX 5070 Ti 16 GB / 16,303 MiB）

这批数字**全部属于旧权重（IQ3_XXS，常驻 9.71 GiB）+ cg192 + nvfp4**，
只用于说明「池上限随 dtype 与权重变化」，**不要照抄**：

| dtype | 实测池上限 | runtime | 余量 |
|---|---|---|---|
| `nvfp4` | **221,184** | — | 起得来但 free 0 B |
| `nvfp4` | 184,320 | 4.23 GiB | 406 MiB（推荐档） |
| `nvfp4` | 176,128 | 4.08 GiB | 580 MiB |
| `rk2v4-e8` | **196,608**（当前生产档） | 3.68 GiB | 引擎自报 1,020.5 MiB |

> 池上限**随桌面占用漂移**：桌面 ~760 MiB 时 `nvfp4` 能到 221,184；
> 桌面 ~1,100 MiB 时只能到 212,992。
>
> 其他显卡（5090 32 GB、5080 等）本机**未实测**。上游 model card 对 5090 给过 240,000，
> 那是参考值，不是实测 —— Agent 必须自己标定。

### ★ `--vision` 很贵

实测（IQ3_S 权重、同池、只切换 `--vision`）：关 131,072 → 开 114,688，
差 32,768 token。按当时的 nvfp4 每 token 18.8 KiB 算 ≈ **615 MiB**。

即使 `--vision-residency overlay`（把视觉塔放主机内存）也省不掉这部分 ——
overlay 省的是**另一笔**（默认 residency 还要再多约 600 MiB）。

**决策**：用户会发截图就保留 `--vision`，把池降下来；
纯文本工作流可以关掉，换来更大的上下文。

---

## 4. ★ 按用户显卡自动定容（核心流程）

### 4.1 一键自动标定（推荐）

```bash
python tools/autoconfig.py --probe
```

它会：探测显卡 → 读出桌面占用 → 二分搜索池上限（每次真实启动引擎验证）→
打印推荐参数，并可选写入 `start-mtp-slim.bat`。

约 1~2 分钟。**这是唯一可信的定容方式。**

### 4.2 手动流程（自动脚本失败时）

**第一步：探测现场**

```bash
nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free --format=csv
```

`memory.used` 就是桌面占用。**注意：这是会变的**（浏览器、壁纸引擎、游戏）。
如果引擎启动失败又查不出原因，先让用户关掉占显存的程序再试。

**第二步：按公式估一个起点**

用第 3 节公式算出大致上限，向下取到 8,192 的整数倍。

**第三步：实测验证**

```bash
python tools/probe-poolsize.py "<候选值>"
```

它会以该池容量真实启动一次引擎，报告**能否就绪** + 引擎自报的 `capacity` 行。

**第四步：写进启动器**

```
--max-context <实测上限>  --kv-capacity <实测上限>
```

两个值**必须相同**（见第 2 节）。

### 4.3 选 KV 精度

`--kv-dtype` 决定每 token 的 KV 开销与速度。**下面两组数据必须分开看**：

**(甲) 单口径横比 —— 池 32,768、同配置、`tools/probe-dtype.py`（2026-10-11）**

| dtype | decode | prefill | KV runtime | 每 token |
|---|---|---|---|---|
| `nvfp4` | **140.3** | 1,625 | 1.54 GiB | ≈ 18.8 KiB |
| `fp8` | 149.2 | 1,631 | 2.01 GiB | ≈ 24.5 KiB |
| `rk4v4` | 97.3 | 1,840 | 1.65 GiB | — |
| `rk8v4` | 96.1 | 1,870 | 1.91 GiB | — |
| `rk2v4-e8` | 90.4 | 1,842 | 1.51 GiB | ≈ 14.4 KiB |
| `bf16`（默认） | — | — | 3.07 GiB | **本机放不下，会拒绝启动** |

> 这一组**只在小池下有意义**：它比较的是「同样 32K 上下文下谁快」。
> 它**不能**用来判断「换 dtype 之后整体快不快」。

**(乙) 生产档口径 —— 池 131,072、各 2 次中位（2026-10-21）**

| dtype | prefill | decode | runtime | nvidia-smi free |
|---|---|---|---|---|
| `rk2v4-e8`（现用） | **1,601** | 169.5 | 2.78 GiB | 1.77 GiB |
| `k8v4` | 1,405（−12%） | 178.8（+5.5%） | 4.20 GiB | 421.8 MiB（**破红线**） |
| `fp8` | 起不来（`requires 5528238592 bytes`） | — | — | — |
| `int8` | 起不来（`requires 5767008768 bytes`） | — | — | — |

> **真实负载里 prefill 占墙钟 61%，decode 只占 29%**（774 条请求统计）。
> 所以「拿 −12% prefill 换 +5.5% decode」是**净亏**，而且换了大 dtype 之后池必然要缩水。

**★ 结论：当前生产档 `rk2v4-e8` 不是"最省但最慢"，而是"唯一能撑起 196,608 池的那一档"。**
更大的 dtype 在这里买不到速度，只买到更小的上下文。

**选择建议**：
- 想开大上下文（≥15 万 token）→ 只有 `rk2v4-e8` 能在 16 GB 卡上做到
- 显存充裕、上下文需求小（≤8 万）→ `nvfp4`
- 只关心 decode 速度、不太发长 prompt → `fp8`（但要按 4.2 节重新标定池）

### 4.4 ★ `nvfp4` 不可用时怎么回退

**`nvfp4` 需要 Blackwell（sm_120a）的 FP4 硬件路径。** 上游明确说明 NVFP4 不能在
Ada / Hopper 上跑。注意区分两件事：

- **引擎二进制**本身只编译了 sm_120a —— 非 Blackwell 的卡需要各自架构的引擎编译版本
  （这类版本的包名/文件名会不同，engine 目录里也不止一个 exe，用 `pick-engine.bat` 选）
- **`--kv-dtype nvfp4`** 是 KV 的存储精度，同样依赖硬件的 FP4 支持

**先做这两步判断：**

1. 跑 `ninfer-serve-120a.exe --help`，看 `--kv-dtype` 列出的完整取值 ——
   那是**这台引擎支持的全集**
2. 若列表里有 `nvfp4` 但启动报错（或跑起来报 KV 相关错误），
   说明当前硬件不支持，按下面的链**依次回退**

**回退链（按推荐顺序，**这一节只讲「nvfp4 在本机起不来」时的退路**）：**

| 顺序 | `--kv-dtype` | 每 token KV 开销 | 说明 |
|---|---|---|---|
| 1 | `nvfp4` | ≈ 18.8 KiB（实测 1.54 GiB @ 32,768） | 仅 Blackwell。首选 |
| 2 | `fp8` | ≈ 24.5 KiB（2.01 GiB @ 32,768） | 兼容性最广的一档；32K 池下 decode 最快（149.2） |
| 3 | `int8` | ≈ +30% *（未实测）* | 上游官方基准就用它，稳 |
| 4 | `k8v4` | ≈ +25% *（未在 32K 池下实测）* | key 8bit / value 4bit；生产档 131K 池实测 prefill −12% |
| 5 | `rk4v4` | +7%（1.65 GiB） | 32K 池下 prefill 最快（1,840），decode 较慢（97.3） |
| 6 | `rk8v4` | +24%（1.91 GiB） | 32K 池下 decode 96 级 |
| 7 | `rk2v4-e8` | **最省**（≈14.4 KiB，1.51 GiB @ 32,768） | 32K 池下 decode 最慢（90.4），但**能撑起最大的池** → 见 4.3 |
| 8 | `bf16` | **翻倍**（3.07 GiB） | 引擎默认值，**最后手段**：16 GB 卡上连小池都放不下 |

> 上表除标注「未实测」的档外，都是 RTX 5070 Ti 上的实测值
> （池 32,768、同配置、`tools/probe-dtype.py`，2026-10-11）。

**★ 换 dtype 之后，池上限一定会变** —— 每 token 的 KV 开销不同了。
必须重跑标定，不能沿用旧值：

```bash
python tools/autoconfig.py --report     # 看新账本
python tools/autoconfig.py --probe      # 实测二分
python tools/autoconfig.py --probe --apply   # 写回启动器
```

**一句话原则：先保证能启动，再谈速度。** `nvfp4` 是兼容性最好的选择，**但不是本机的最终答案** ——
本机最终选了每 token 最省的 `rk2v4-e8`，因为它能换来更大的池（见 4.3 组乙）。

---

## 5. 参数速查表

**★ 下表第 3 列是「本机生产档的取值」，不是「所有人都该这么设」。**
判定依据见第 3、4 节和头部铁律。

| 参数 | 作用 | 本机取值 / 建议 |
|---|---|---|
| `--max-context` | 单请求逻辑上限，**会报给客户端当 context_window** | **196608**，必须 = `--kv-capacity` |
| `--kv-capacity` | 常驻 KV 池（决定显存与容量），`auto` 极度保守不可用 | **196608** |
| `--kv-dtype` | KV 存储精度 | **`rk2v4-e8`**（本机唯一能撑起 196,608 的一档）；起不来时按 4.4 回退 |
| `--gdn-state-fp16` | state 用 FP16，省 143 MiB | **开启** |
| `--cuda-graph-allowance-mib N` | 限制 CUDA Graph 占用 KV 预算的额度 | **192**（2026-10-16 实测：设 32 时引擎自报 graph 实际用了 42~207 MiB，那笔账是假的；192 只多花约 56 MiB 余量） |
| `--host-kv-mib N` | pinned **主机内存**（不是显存）KV 层 | **生产档不设**。只在开环模式需要；与 `--host-cache-mib` 互斥 |
| `--prefill-chunk 512` | 分块 prefill 大小（128 的倍数） | **512**；实测 1024 在 20K/80K 只差 −0.2% / +1.0%（噪声），不必改 |
| `--spec mtp --draft-tokens 4` | 投机解码 | **mtp + 4**（实测 3.82~3.83 token/轮，已顶满；`draft-tokens 16` 实测 −10%） |
| `--vision --vision-residency overlay` | 支持图片输入 | **保留**（overlay 比默认省约 600 MiB） |
| `--max-concurrency 1` | 并发请求数 | **1**（单用户场景；也正因如此引擎自动开 `--ngram-draft-tokens 15`） |
| `--max-shared-prefixes 7` | 跨会话共享前缀目录 | **7**（**必须配 `--derive-session-keys`**，见第 7 节） |
| `--default-max-tokens` | 请求未指定时的输出上限 | **32768**（客户端一般会自己指定） |
| `--request-log-jsonl` / `--request-log-max-mib` | 请求日志 + 滚动上限 | **开**（`logs/requests.jsonl` / 256 MiB）；**目录必须预先存在**，否则引擎 `FATAL: failed to open request JSONL log for append` |

**已实测「改了也没用」的参数（别浪费时间）**：

| 参数 | 实测结果 |
|---|---|
| `--prefill-chunk 1024` | 20K −0.2%、80K +1.0%（噪声） |
| `--prefill-chunk 2048` | **起不来**：`requires 5530454528 bytes, but only 5315231744 bytes are available` |
| `--lookup-ngram N` | ±0.5%（噪声）；ngram 草稿本来就在 `--max-concurrency 1` 下自动开 15 |
| `--no-prefill-a8`、`--prefill-cublas --no-prefill-cublas-projections`、`--mtp-attention-window 512` | 全部噪声（±0.6%） |
| `--lm-head-draft` | **−9%**，且权重 +0.29 GiB（反向） |
| `--lm-head-q4/-q6`、`--embedding-q4/-q6` | **FATAL**：要求 text/output_head 是 row-split Q8_G32 |
| `--wddm-evictable-budget` | **FATAL**：需要 `NINFER_D3D12_RESIDENCY=ON` 的构建 |
| `--device-profile calibrate` | 启动 45 s 全花在标定，prefill 只 +1.6%（噪声）；换回旧路由表也不更快 |
| `--draft-tokens 16` | **−10%** |
| 138 个 `NINFER_*` 环境变量里的 11 个 kernel 调度器 | 全部 ±0.6%（噪声；它们对应 MoE / ternary 路径，稠密模型不走） |
| 50 键路由表（`device-profiles.json`） | 强制重标定 −0.4%，各档强设 ±1%；decode 的 T=5 档新旧表都落在同一 kernel `r16c8w1` |

---

## 6. 排障：症状 → 处方

### 6.1 启动失败

**永远先拿退出码**（`probe-bat.py` 或直接看 bat 输出）：

- **3** → 模型文件路径错/不存在。检查 `MODEL` 变量与文件实际位置。
  **不要用硬编码盘符路径**，用 `%ROOT%`（脚本同目录）。
- **6** → 30 秒未就绪。两种可能：
  - **显存不足** → 用 `probe-poolsize.py` 二分找一个能启动的池
  - **参数非法** → 引擎 stderr 会有 `FATAL`，用重定向启动读它（见 6.3）

### 6.2 服务报错

| 症状 | 根因 | 处方 |
|---|---|---|
| `503 inference engine is unavailable` | worker 崩溃后拒绝服务（**进程还活着**，别急着重启） | 读控制台日志（见 6.3） |
| `500 internal error` + `worker crash: Paged KV reservation invariant was violated` | 超池 + 前缀共享/adopt | 确认 `NINFER_KV_REUSE_HOSTBACKED=0`；把池调大 |
| `Paged KV pool cannot resize the page reservation: usable N pages, allocated X, reserved Y, requested 1` | **该请求的 prompt 正好等于池容量**（X+Y = N），没给输出留空间 | 池调大，或让客户端压缩历史（开新会话） |
| 回复中途截断、`finish_reason=length`、正文 0 字符 | 思考预算吃光了 `max_tokens` | 保证 `--default-thinking-budget` 明显小于客户端 `max_tokens` |
| `400 context_length_exceeded` / `prepared prompt exceeds Engine max_context N` | **客户端记的窗口值比引擎大**。引擎按 `--max-context` 判，客户端按自己配置判；两者不一致时，客户端以为没超、引擎判超 | 把**客户端的 `contextWindow` 改成 `--max-context` 的同一个数**（本机 196608）。DSH 在 `~/.dsh/profiles/*/cordis.patch.yml`。**改前先备份** |
| 客户端报 `413 preflight_compress_failed` / `0 compressible range(s)` | 客户端算出的上下文超过了它以为的窗口，想压缩又找不到可折叠的历史中间态（**常见于导入的会话**：历史整体灌入、无中间态） | 同上，先修窗口值；若会话本身已超窗，只能重开会话或缩小导入量 |

### 6.3 怎么读引擎日志（关键技巧）

**引擎不写日志文件**（除非启动时给 `--request-log-jsonl <file>`）。
默认它只往自己的控制台窗口输出。两种取法：

**(A) 控制台缓冲区（不打扰正在运行的引擎）**

```bash
python tools/read-engine-console.py <引擎PID>
```

用 `AttachConsole` + `ReadConsoleOutputCharacterW` 读缓冲区，并**自动拼接折行**
（控制台宽 120 列，长日志会被硬折行，不拼接会读错）。
能拿到 `req#N done | prompt ... | cache N (P%, 原因) | TTFT ... | prefill ... | decode ...`。

**(B) 重定向启动（诊断用，会重启引擎）**

```bash
python tools/start-engine-trace.py     # 引擎独立进程 + 日志进 logs/engine-trace.log
```

### 6.4 速度慢

先分清是**哪一段**慢 —— 引擎日志的 `req#N done` 行直接给出答案：

| 现象 | 判据 | 处方 |
|---|---|---|
| 第一句慢（几十秒） | `cache 0 (0.0%)` | **冷启动，正常**。引擎重启后池是空的，必须全量 prefill |
| 后续轮次也慢 | `cache` 命中率低（< 90%） | 前缀没复用：确认 `--max-shared-prefixes 7` + `--derive-session-keys` 都在 |
| 每轮都慢且 `cache 0` | 客户端每次发的 prompt 前缀都不同 | 排查客户端是否注入了动态内容（时间戳、随机 ID）；会话带图片时尤其容易发生 |
| 整体都慢但 cache 正常 | `decode tok/s` 低 | 换 `--kv-dtype`；或上下文过长导致 KV 读取变慢（正常现象） |

---

## 7. 已知的坑（全部实测踩过）

1. **`.bat` 文件里绝对不能有非 ASCII 字节**
   一个中文字符就会让 cmd 解析整行崩掉，表现为刷 `'M' is not recognized as an internal or external command`，
   **引擎根本不会启动**。检查：
   ```python
   len([b for b in open('start-mtp-slim.bat','rb').read() if b > 127]) == 0
   ```

2. **`--max-shared-prefixes` 必须配 `--derive-session-keys`**
   只开前者会让 worker 崩溃（`active KV snapshot full page is not stable`）。

3. **超池 + 任何前缀共享机制 = 崩溃**
   超池本身只是变慢；一旦叠加共享前缀/adopt，就会 500 → 503。

4. **`--kv-capacity auto` 极度保守**
   在 16 GB 卡上只给 17,536 / 16,704。**不要用 auto，要显式指定数值。**

5. **引擎只接受注册过的 artifact identity**
   日志里会写 `artifact identity 'X' is not supported by target 'Y'`。
   换模型前先确认引擎版本支持它。

6. **桌面显存是会变的**
   同一个配置，桌面占用多 500 MiB 就可能起不来。**排障时先让用户关程序再复测。**

7. **不要用 `grep -i npu` 之类的方式判断引擎能力**
   引擎只链 `cudart` / `cublas` / `cublasLt`，是纯 CUDA。NPU 无法参与。

---

## 8. 证据与复现

所有数字都有原始日志，工具都在 `tools/`：

| 工具 | 用途 |
|---|---|
| `autoconfig.py` | ★ 按显卡自动定容（探测 + 二分实测 + 写配置） |
| `probe-poolsize.py` | 测某个池容量能否启动，报告 capacity 行 |
| `probe-bat.py` | 跑一次启动器，报告退出码 + 引擎进程 + 窗口标题 |
| `read-engine-console.py` | 读运行中引擎的控制台缓冲区（含折行拼接） |
| `start-engine-trace.py` | 带请求日志启动引擎（诊断用） |
| `probe-dtype.py` | 各 KV 精度的显存与速度对比 |
| `probe-vram.py` | 验证「上下文长度 / max-context 是否吃显存」 |
| `probe-reuse-live.py` | 测连续对话的前缀复用（不动运行中的引擎） |

**复现一条完整证据链**（约 3 分钟）：

```bash
python tools/autoconfig.py --probe            # 定容
python tools/probe-bat.py                     # 启动健康检查
python tools/probe-dtype.py                   # 精度对比（可选，较慢）
```

---

## 9. 给 Agent 的交付清单

当你按本文件完成部署后，应该能回答：

- [ ] 用户显卡是什么？显存多少？桌面占了多少？
- [ ] 实测的池上限是多少？（不是公式算的）
- [ ] `--max-context` 与 `--kv-capacity` 是否**相等**？
- [ ] 用的哪个 `--kv-dtype`？为什么？
- [ ] `--gdn-state-fp16` 开了吗？
- [ ] 启动退出码是 0 吗？
- [ ] 首轮慢（冷启动）和后续慢（缓存失效）能区分吗？

**做不到就说明还没交付完。**
