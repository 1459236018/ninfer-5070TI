# 128k / dflash2 启动套件 —— 历史存档（已废弃）

> **这一整套已经不能用了，这份文件只留作历史记录。**

2026-10-10 到 2026-10-16 之间，本包用过一套 **dflash2 投机解码 + 128k 上下文**的配置。
2026-10-16 起换成 **mtp 投机解码 + 196,608 全稠密上下文**，旧档位作废。

## 为什么废弃

旧的 `start-dflash-128k-tuned.bat` 已经退休，包内同名文件现在是一个**拒绝启动的说明桩**
（`exit /b 5`）。原因有三条，任意一条都足以让它跑不起来：

1. **模型不在包里** —— 那个配置要 `gsq_rco_iq3_s_dflash2_prop.ninfer`
   （15,017,456,128 字节），本包不含模型权重，也不再分发这个 artifact。
2. **违反硬约束** —— 旧启动器写的是 `--max-context 131072` 配 `--kv-capacity 8192`。
   池容量必须等于上下文长度，两者不等引擎会直接拒绝启动。
3. **参数已全部过期** —— 端口、`--host-kv-mib`、`--media-cache-mib`、`--lm-head-draft`、
   `--fast-prefill-kernel` 这些在当前版本上要么不存在，要么语义变了。

## 现在的正确用法

**不要照抄本文件里的任何数字。** 以这两份为准：

- [`../AGENTS.md`](../AGENTS.md) —— 给 AI 助手看的部署与调优指南（含显存账本、容量公式、排障）
- [`MODEL.txt`](MODEL.txt) —— 模型格式要求、候选清单、池容量换算表

启动器用 `start-mtp-slim.bat`（或桌面快捷方式）。

## 当年记下来的东西（仅作历史，别照抄）

这些是 2026-10-10 / 10-11 在旧模型上的实测值，**和现在的引擎无关**：

| 项 | 旧值 |
|---|---|
| 模型 | `gsq_rco_iq3_s_dflash2_prop.ninfer`（15 GB） |
| 上下文 / 池 | `--max-context 131072` / `--kv-capacity 8192` |
| 投机解码 | `--spec dflash2 --draft-tokens 12 --lm-head-draft` |
| decode | 161.8 → 286.0 tok/s（调 `--draft-tokens 4→12` + `--host-kv-mib 16384→4096`） |
| 主机提交内存 | 35.8 → 23.5 GiB |
| 端口 | 8094（现在是**代理**端口，引擎在 8095） |

两条历史经验仍然成立，值得记住：

- **`NINFER_KV_REUSE_HOSTBACKED=1` 会让超池时崩溃**（`Paged KV reservation invariant was violated`
  → worker crash → HTTP 500）。现在启动器里固定为 `0`。
- **"思考了但不输出"** 是因为思考 token 和答案共用 `max_tokens` 预算。
  靠 `--default-thinking-budget` 明显小于客户端 `max_tokens` 来解决。
  这条在 [`../AGENTS.md`](../AGENTS.md) 第 6.2 节还写着。

想看完整原文（120 行，含 Hermes 追问 500 的完整排查过程），查 git 历史：

```bash
git log --follow -- docs/README-128k.txt
git show <commit>:docs/README-128k.txt
```
