# NInfer 引擎包 · N 卡专用（调优版 · 不含模型 · 2026-10-19）

> **⚠️ 仅限 NVIDIA 显卡（N 卡）。**
> 引擎只链 CUDA 运行时（`cudart64_13` / `cublas64_13` / `cublasLt64_13`），
> 启动第一步就跑 `nvidia-smi` 读算力；**AMD / Intel 显卡连第一关都过不了**（退出码 3），
> 不是调参数能解决的。

**只有引擎和调优配置，不含任何模型。** 模型请自备（见下方「放模型」）。

> **这个包是交给 AI Agent 部署的。**
> **Agent** → 直接读 `AGENTS.md`，从 **§0.1 部署流程** 开始，按 **§13 七项自检** 收尾。
> **人类用户** → 看下面的「第一次使用」和「创建桌面快捷方式」。

## 内置

| 项 | 说明 |
|---|---|
| `engine/` | `ninfer-serve-120a.exe`（686 MB，sm_120a 专用）+ 12 个 DLL（CUDA 13 / FFmpeg / libcurl） |
| `start-mtp-slim.bat` | **已调优的生产启动器**（纯 ASCII）。**已去掉双模型 fallback**：只认包根那一个 `.ninfer` |
| `start-mtp-slim-silent.vbs` | 桌面快捷方式的入口（隐藏控制台 → 托盘） |
| `ninfer.ico` | 托盘图标 |
| `tools/` | 托盘宿主 + 标定/诊断脚本（`ninfer-tray.py` 已改成优雅停止） |
| `bench/` | 本次优化用到的 12 个测量脚本 |
| `re/` | 逆向中间产物（247 个源文件路径 / 131 个 CLI 标志 / 31,104 条字符串） |
| `reports/` | 686 MB 二进制的静态逆向报告（HTML） |
| `AGENTS.md` | **主文档：给下一位 AI Agent 的 13 节交接材料** |
| `README-pack.md` / `MODEL.txt` / `SHA256SUMS.txt` / 教程 | 原包自带的说明与校验清单 |

## 没内置（刻意）

- **任何 `.ninfer` 模型** —— 单个就 9.7~10.3 GB，不适合塞进引擎包
- **Python 运行时** —— 托盘宿主需要它（见下节），但体积与版本绑定，请自行安装
- `start-uncensored.bat` —— 它是切换第二个模型的 wrapper，随双模型支持一起去掉了
- `logs/` —— 运行时生成

## 运行依赖：托盘需要 Python（重要）

**前缀缓存靠托盘在退出时发 CTRL_BREAK 才会写盘**，所以托盘不是可选装饰。它需要：

```
Python 3.9+ ，以及两个包：pystray、Pillow
pip install pystray pillow
```

`start-mtp-slim-silent.vbs` 按这个顺序找解释器：

1. 包内 `python\pythonw.exe`（想零依赖就把便携版 Python 解压到包内 `python\`）
2. PATH 里的 `pythonw.exe`
3. 都没有 → **退化成直接启动 `start-mtp-slim.bat`**

第 3 种情况下：**引擎照样能跑，但没有托盘图标、也不会写前缀缓存**，每次重启都是全量 prefill。
如果你发现重启后第一句仍然很慢，先确认托盘图标（蓝色小方块）有没有出现。

> 包里的 `start-mtp-slim-silent.vbs` 已去掉「选择模型」弹窗 —— 只认 `start-mtp-slim.bat`。
> 源机器那份仍保留弹窗（它有两个模型可选），两者仅此不同。

## 放模型

把 `.ninfer` 放到**包根**（和 `start-mtp-slim.bat` 同级），默认文件名：

```
swift15_iq3xxs_mtp.ninfer
```

用别的名字或路径时，二选一：

```bat
REM 方式一：启动前设环境变量
set MODEL=D:\models\my-model.ninfer
start-mtp-slim.bat

REM 方式二：改 start-mtp-slim.bat 里那一行
set "MODEL=%ROOT%swift15_iq3xxs_mtp.ninfer"
```

**换模型后必须重新标定池上限**（权重体积直接决定能开多大池：每省 1 GiB ≈ 多开 5.7 万 token）。
用 `tools/autoconfig.py --probe` 或 `tools/probe-poolsize.py <候选值>`。

## 创建桌面快捷方式

**包里没有 `.lnk`** —— 快捷方式内部写死了目标的**绝对路径和卷序列号**，换机器、换目录立刻失效，
所以只能各自生成。

### 方法 A：右键（最简单，不装任何东西）

1. 右键 `start-mtp-slim-silent.vbs` → **发送到** → **桌面快捷方式**
2. 右键桌面上那个快捷方式 → **属性** → **更改图标** → 选包里的 `ninfer.ico`
3. 可改名为「AI引擎」

### 方法 B：脚本生成（适合批量 / 自动化）

```bat
pip install pylnk3
python tools\make_shortcut.py ^
  --target  "%CD%\start-mtp-slim-silent.vbs" ^
  --workdir "%CD%" ^
  --icon    "%CD%\ninfer.ico" ^
  --name    "AI引擎"
```

- 默认写到 `%USERPROFILE%\Desktop`，用 `--outdir` 改位置；`--minimized` 让窗口最小化启动
- **`pylnk3` 必须先装**：它负责生成 target IDList。不装也能出 `.lnk`，但会**缺 IDList**
  （实测 310 字节 vs 完整版 575 字节），资源管理器对残缺 lnk 支持不好。脚本会明确打印
  `idlist  : SKIPPED -- pylnk3 not installed`

### 为什么目标是 `.vbs` 而不是 `.bat`

`start-mtp-slim-silent.vbs` 负责**隐藏控制台 → 起托盘 → 托盘再拉 bat**。
直接把快捷方式指向 `.bat` 会留一个控制台窗口，而**关掉那个窗口就等于杀掉引擎**。

### 图标

`ninfer.ico` 是手工拼的 9 帧图标（16~256 px）。引擎 exe 本身**没有图标资源**，
所以 `--icon` 必须指向这个 `.ico` 文件，指向 exe 只会得到空白图标。

## 放模型

包里**不含任何模型**。把**任意一个** `.ninfer` 放到包根（和 `start-mtp-slim.bat` 同级）就行，
**不用改任何配置** —— 启动器会自动使用它找到的那唯一一个 `.ninfer`：

```
<包根>\你的模型.ninfer
```

文件名随意（`start-mtp-slim.bat` 里**没有硬编码任何模型名**，是自动发现的）。
包根有多个 `.ninfer` 时，用环境变量指定要哪个：

```bat
set MODEL=D:\models\my-model.ninfer
start-mtp-slim.bat
```

换模型后**必须重新标定池上限** —— 权重体积直接决定能用多大上下文，见下面「硬件要求」里的显存表。

## 第一次使用

1. 解压到任意目录（**路径别含空格**；中文路径也尽量避开）
2. 放好模型
3. 双击 `start-mtp-slim-silent.vbs`
4. 第一次对话仍是全量 prefill（还没有缓存文件，日志会写 `no saved prefix cache ... yet`）
5. 从**托盘菜单退出**（别用任务管理器强杀），日志写 `prefix cache saved: ...`
6. 再启动 → 日志出现 `prefix cache restored` → 之后每次首轮都是 0.1 秒级

## 三项关键结论（详见 AGENTS.md）

1. **KVMem 环已关闭** —— 池 == max-context 时它纯属空转，关掉白赚 186 MiB 显存 + 8~15% prefill。
2. **前缀缓存用引擎内置的检查点目录**（legacy）—— 同一会话从第二轮起 **0.31 秒**响应（命中 99.9%）。
   重启后的第一句是全量 prefill，这是当前配置的取舍。
3. **`--host-kv-mib` 不占专用显存**，prefill 已到算子极限 —— 这两条别再找收益。

## ⚠️ 关于 hybrid 前缀缓存：本包**不启用**

早期版本用过 hybrid 前缀缓存（`--use-alt-prefix-caching` + `--prefix-cache-file`），能让"重启后首轮"
从十几秒降到 0.16 秒。**但它有一个致命的 bug，已经移除：**

缓存文件只增不删，实测攒到 **1.3~2.9 GiB** 后，引擎 restore 时会让某一层的 CUDA event 句柄失效：

```
CUDA_CHECK(cudaStreamWaitEvent(...)) failed: cudaErrorInvalidResourceHandle
```

**引擎会在第一个请求上直接退出**（Application 日志里没有任何崩溃记录，看起来像"用着用着突然没了"）。

**本包的做法**：完全不启用 hybrid。代价是重启后第一句要重新 prefill（4 万 token 约 30 秒），
收益是**这条崩溃路径彻底消失** —— 缓存文件根本不会生成。

> 如果你自己想启用，务必配上 token 体积上限（如 768 MiB）并在**启动前**清理超限的缓存文件，
> 详见 `AGENTS.md` §5.7。这个坑在原厂引擎和社区分支上都存在，换引擎躲不掉。

## 校验说明

`SHA256SUMS.txt` 是**原包**的清单。本包改过 `start-mtp-slim.bat`（去双模型）与
`tools/ninfer-tray.py`（优雅停止），这两个的哈希与原清单不一致，属预期；其余文件未改动。

## 硬件要求

- **NVIDIA 显卡**（N 卡专用 —— AMD / Intel 在第一步就以退出码 3 结束）
- **显存 ≥ 8 GB**。8 GB 是"能跑"的下限（要关 `--vision`、用 4~5 GiB 的 artifact）；
  **12 GB 起比较舒服，16 GB 是这套参数标定时用的档位**
- **compute capability 必须是 8.0 / 8.6 / 8.9 / 12.0 之一**（引擎只支持这四档）。
  本包只自带 **12.0（RTX 50 系）** 的 exe；30/40 系要先补对应架构的 exe 进 `engine\`
- 系统内存 ≥ 16 GB（`--host-cache-mib 4096` 会 pin 4 GiB，可下调）

### 按显存选 artifact（权重体积决定上下文）

引擎不认具体型号，只认 `.ninfer` 的 artifact identity 和**体积**。
体积直接决定池上限：**权重每省 1 GiB，池可多开约 5.7 万 token**。

| 显存 | 建议权重体积 | 大致规模 | `--vision` | 预期池 |
|---|---|---|---|---|
| **8 GB** | ≤ 4.5 GiB | 10~14B，IQ3_XXS 级 | 建议关 | 20k~25k |
| 12 GB | ≤ 7 GiB | 14~20B，IQ3_XXS 级 | 可关 | 60k~70k |
| **16 GB** | ≤ 11 GiB | 24~32B，IQ3_XXS 级 | overlay | 150k~190k |
| 24 GB | ≤ 18 GiB | 32B @ IQ4，或更大参数 @ IQ3 | 开 | 300k+ |
| 32 GB | ≤ 26 GiB | 32B @ Q6/bf16 | 开 | 400k+ |

> 表里是**估算**，按 `池 ≈ (显存 − 桌面1.2G − 权重 − vision − 0.55G) ÷ 18.8 KiB` 推算。
> **实际必须用 `python tools\autoconfig.py --probe` 标定**（它会真实启动引擎做二分搜索）。

### ⚠️ 参数不是通用的

包里的 `start-mtp-slim.bat` 是**按 16 GB 卡 + 约 9.7 GiB 权重**标定的参考档
（`--max-context 163840 --kv-capacity 163840`）。

**显存或模型不一样，必须改这两个数，而且必须一起改** ——
`--kv-capacity` 不能大于 `--max-context`，否则引擎直接拒绝启动。

### 不是 50 系？按这张表换两处

| 架构 | 卡 | 需要的 exe | `--kv-dtype` 起步档 |
|---|---|---|---|
| sm_120 | RTX 50 系 | `ninfer-serve-120a.exe`（**包内自带**） | `nvfp4` |
| sm_89 | RTX 40 系 | `ninfer-serve-89.exe`（需自备） | `fp8` |
| sm_86 | RTX 30 系 | `ninfer-serve-86.exe`（需自备） | `rk4v4` |
| sm_80 | A100 | `ninfer-serve-80.exe`（需自备） | `rk4v4` |

> ⚠️ 如果 artifact 的**权重量化是 NVFP4**，在 40/30 系上 prefill 会被限制到 16 列宽（几乎不可用），
> 需要换 groupwise-int 的 artifact（Q4/Q5/Q8 系）。IQ3_XXS 属 groupwise-int，不受此限。
