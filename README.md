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
