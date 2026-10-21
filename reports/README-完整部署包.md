# NInfer 完整部署包（2026-10-19）

**这是一个可直接运行的完整包** —— 引擎、模型、调优后的启动器、托盘、文档都在里面。
解压后双击 `start-mtp-slim-silent.vbs`（或桌面快捷方式）即可。

## 内置

| 项 | 说明 |
|---|---|
| `engine/` | `ninfer-serve-120a.exe`（686 MB，sm_120a 专用）+ 12 个 DLL（CUDA 13 / FFmpeg / libcurl） |
| `swift15_iq3xxs_mtp.ninfer` | 默认模型，IQ3_XXS，9.71 GiB 常驻，artifact `swift-1.5-qwen3.8-27b-gsq-rco-iq3xxs` |
| `start-mtp-slim.bat` | **已调优的生产启动器**（纯 ASCII） |
| `ninfer-tray.py`（在 `tools/`） | **已改的托盘**：停止时走 CTRL_BREAK 优雅关闭，前缀缓存才能落盘 |
| `tools/` | 原包自带的标定与诊断脚本 |
| `bench/` | 本次优化用到的 12 个测量脚本 |
| `re/` | 逆向中间产物（247 个源文件路径 / 131 个 CLI 标志 / 31,104 条字符串） |
| `reports/` | 686 MB 二进制的静态逆向报告（HTML） |
| `AGENTS.md` | **主文档：给下一位 AI Agent 的 13 节交接材料** |

## 没内置

- `Huihui-Qwen3.8-27B-abliterated-IQ3_XXS-mtp.ninfer`（另一个可选模型，10.33 GB）。需要就另行放入包根，
  `start-uncensored.bat` 会用它。
- `logs/`（运行时生成）

## 三项关键结论（详见 AGENTS.md）

1. **KVMem 环已关闭** —— 池 == max-context 时它纯属空转，关掉白赚 186 MiB 显存 + 8~15% prefill。
2. **已切到 hybrid 前缀缓存** —— 重启后首轮 **11.8 s → 0.16 s**。前提是「优雅退出」一次，
   而优雅退出的信号必须 `AttachConsole` 才送得到（托盘已改好，从菜单退出就会自动存盘）。
3. **`--host-kv-mib` 不占专用显存**，prefill 已到算子极限 —— 这两条别再找收益。

## 第一次使用

1. 解压到任意目录（**路径不要含空格或中文**，`.bat` 里用 `%~dp0` 取根目录，但空格仍可能出问题）
2. 双击 `start-mtp-slim-silent.vbs`（会启动引擎 + 托盘，控制台最小化）
3. 第一次对话仍是全量 prefill（此时还没有缓存文件）
4. 从**托盘菜单退出**（别用任务管理器强杀），日志会写 `prefix cache saved: ...`
5. 再启动，日志出现 `prefix cache restored` —— 之后每次首轮都是 0.1 秒级

## 校验说明

`SHA256SUMS.txt` 是**原包**的清单。本包改过 `start-mtp-slim.bat` 与 `tools/ninfer-tray.py`，
所以这两个文件的哈希与原清单不一致，属预期；其余文件未改动。

## 硬件要求

- NVIDIA RTX 50 系（**compute capability 12.0**，sm_120a）。30/40 系需要各自架构的 exe，本包只带 12.0 的。
- 显存 ≥ 16 GB（当前配置引擎占约 14.4 GiB）
- 系统内存 ≥ 16 GB（`--host-cache-mib 4096` 会 pin 4 GiB）
