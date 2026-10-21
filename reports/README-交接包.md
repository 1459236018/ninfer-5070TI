# NInfer 引擎调优交接包（2026-10-19）

这个包是**给下一位 AI Agent 的交接材料**，不是给引擎用的运行时文件。
安装说明在 `AGENTS.md`（请让 Agent 先读它）。

## 里面有什么

| 目录 | 内容 | 用途 |
|---|---|---|
| `AGENTS.md` | **主文档**（13 节） | 环境核对 → 当前配置 → 两条优化 → 机制与证据 → 红线 → 已证伪清单 → 复现指南 → 回滚 → 未解 |
| `config/start-mtp-slim.bat` | 已调优的生产启动器 | 直接替换源包的启动器；纯 ASCII |
| `config/ninfer-tray.py` | 已改的托盘宿主 | `stop_engine()` 用 CTRL_BREAK 优雅停止，前缀缓存才能落盘 |
| `reports/引擎逆向分析-2026-10-18.html` | 686 MB 二进制的静态逆向报告 | 架构、fatbin、API、内置硬件表、模块划分 |
| `bench/` | 12 个测量脚本 | 复现每一个数字；`_send_break.py` 是发信号的关键 helper |
| `re/` | 逆向中间产物 | `src_tree.txt`（247 个源文件）、`all_flags.txt`（131 个 CLI 标志）、`str_rdata.txt`（31,104 条字符串，可 grep） |

## 不含什么

- 模型 `.ninfer`（9.71 GiB）
- `engine/`（686 MB 的 exe + DLL）

这两样是引擎本体，不随这个知识包分发。

## 三句话总结

1. **关掉 KVMem 环**：池 == max-context 时它纯属空转，关掉白赚 186 MiB 显存和 8~15% prefill。
2. **换 hybrid 前缀缓存 + 优雅关闭**：重启后首轮从 11.8 s 降到 **0.16 s**。关键是 `FreeConsole → AttachConsole → GenerateConsoleCtrlEvent(CTRL_BREAK)`，不这样信号根本送不到引擎。
3. **`--host-kv-mib` 不占专用显存**，prefill 已到算子极限 —— 别再往这两个方向找收益。

## 使用顺序

1. 让 Agent 通读 `AGENTS.md`（尤其 §7 红线、§8 已证伪，能省掉几小时弯路）
2. 按 §2 核对环境（compute_cap 必须是 12.0）
3. 用 `config/` 下两个文件替换源包里的同名文件
4. 双击桌面图标启动 → 正常从托盘退出一次 → 再启动，即可看到 `prefix cache restored`
5. 按 §13 的七项自检确认

## 数据来源

所有数字都来自本机 RTX 5070 Ti 16 GB 的实测，脚本与日志判据都在 `bench/` 与 `AGENTS.md` §9。
不是推算，也不是上游文档抄来的。
