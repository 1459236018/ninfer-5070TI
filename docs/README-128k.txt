NInfer sm_120a 128k 启动套件 — 2026-10-10 打包
=================================================

> **⚠️ 本文档已过期，仅作历史存档（2026-10-21 标注）。**
>
> 它描述的 `start-dflash-128k-tuned.bat` **已于 2026-10-16 退休**：其引用的模型
> `gsq_rco_iq3_s_dflash2_prop.ninfer` 已不在本包内，且该启动器的
> `--max-context 131072` 配 `--kv-capacity 8192` 违反硬约束（引擎拒绝启动）。
> 现在的包内同名文件是一个**拒绝启动的说明桩**（`exit /b 5`）。
>
> **请以根目录的 `AGENTS.md` 与 `docs/MODEL.txt` 为准**，本节之后的数字
> （decode 286、draft-tokens 12、128k 档位表等）都属于旧模型 dflash2，**不要照抄**。

这张 zip 是"启动程序"部分（引擎 + 启动器 + 文档），按本包设计，模型单独分发：
模型文件 gsq_rco_iq3_s_dflash2_prop.ninfer（15,017,456,128 字节，sha256
D44903147D72DD26D447CDFFE02AA33E713CFC4B7B427AF587B5D17BE8AB6496）
需要放到 start-dflash-128k-tuned.bat 旁边（同一目录），或运行前：
    set MODEL=D:\path\to\gsq_rco_iq3_s_dflash2_prop.ninfer
    start-dflash-128k-tuned.bat

适用卡：RTX 50 系（compute capability 12.0 = sm_120a），本套件只在
RTX 5070 Ti 16 GB 上验证过。

★ 2026-10-11 优化（实测，详见 引擎优化报告.html）：
  新增两个启动器；原出货档 start-pq2-dflash-128k.bat 与 start-pq2-mtp-128k.bat
  已移除（连同本机无对应模型的 PTQ1_0 / PQ2 两档），移入 .workbuddy/_trash/ 可恢复。
  - start-dflash-128k-tuned.bat  吞吐档：decode 161.8 -> 286.0 tok/s（+77%），
      同显存；主机提交内存 35.8 -> 23.5 GiB（-12.3 GiB）。
      改动：--draft-tokens 4->12；--host-kv-mib 16384->4096；
            新增 --fast-prefill-kernel。
  - start-mtp-slim.bat           省显存档：mtp 模型 + CUDA graph，
      加载后显存 ~14.6 GiB（比 dflash2 省 ~1 GiB），decode 180 tok/s。
  长上下文实测（tuned 档）：11.4k / 15.2k 题面追问 HTTP 200、缓存 99.7~99.8%、
      TTFT 174~178 ms、零 500。旧 README 的"追问 11s / 500"在 tuned 档下已不复现。
  注：CUDA graph 对 dflash2 仍不可用（需 1.80 GiB 预留 -> 拒绝）；
      --kv-capacity 16384 不可用（需 1.06 GiB 预留 -> 拒绝）。

档位与配置（全部实测，见 start-dflash-128k-tuned.bat 注释）：
- dflash2 128k（吞吐档）：start-dflash-128k-tuned.bat（端口 8094）
    --max-context 131072 --kv-capacity 8192 --no-cuda-graph
    --prefill-chunk 256 --media-cache-mib 0
    --spec dflash2 --draft-tokens 12 --lm-head-draft --fast-prefill-kernel
    --vision --vision-residency cpu
    --default-reasoning-effort high --default-thinking-budget 2048
    --thinking-budget-message "Thinking budget reached. Stop deliberating and
    write your final answer now."（防止"思考了但不输出"，见文末第 9 节）
    环境变量 NINFER_KV_REUSE_HOSTBACKED=0（Hermes /v1/responses 追问修复，见文末；
    =1 会在会话超出设备池时 adopt 保留前缀失败 → worker crash / HTTP 500）。
- 省显存档（mtp + CUDA graph，余量约多 1 GiB）：start-mtp-slim.bat（端口 8094）
    --max-context 131072 --kv-capacity 17920 --prefill-chunk 512
    --spec mtp --draft-tokens 4 --vision --vision-residency overlay
    --default-reasoning-effort high --default-thinking-budget 2048
    --thinking-budget-message "..."（防止"思考了但不输出"，见文末第 9 节）
    环境变量 NINFER_KV_REUSE_HOSTBACKED=0（同上）
    （模型用 mtp 变体 swift-qwen38-rco-iq3s.ninfer，另在
    D:\infer-engine-sm120a 或 set MODEL 指定）

注意：
- dflash2 档启动后显存仅剩 ~0.22 GiB（runtime 779.8 MiB，实测），壁纸引擎/
  浏览器/模拟器等显存大户可能 OOM 引擎；启动前先关掉大户。
- KVMem 内容打分默认开启；发过请求后引擎日志应出现 "ring retrieve" 或
  "kvmem_score: SELECT" 行。
- 详细说明：教程-KVMem与短测-给agent与使用者.md；校验：SHA256SUMS.txt。

用 Hermes Desktop 连引擎（可选但推荐）：
- 引擎对 Responses API 的 reasoning item 严格要求 content 数组且只认
  reasoning_text；Hermes 在对话历史里发的 summary 型 reasoning 会被拒：
  HTTP 400 "reasoning Item must contain a content array" / "summary ... cannot
  reconstruct"。
- 修复：先启动引擎，再双击 start-hermes-proxy.bat（起本地代理 8096 -> 引擎
  8094，自动丢弃引擎用不上的 reasoning item），然后把 Hermes Desktop 的后端
  地址改成 http://127.0.0.1:8096（模型 qwen3.8-27b）。
- 停止：taskkill /f /im ninfer-serve-120a.exe；代理按端口停：
  for /f "tokens=5" %a in ('netstat -ano ^| findstr :8096 ^| findstr LISTENING') do taskkill /f /pid %a

Hermes 追问 HTTP 500 修复（2026-10-10 实测通过）：
- 症状：会话里第一条（含图）请求成功；追问（带图+工具历史的保留前缀 ~16k
  token）报 "AI服务返回了无法读取的回复"，Hermes 日志：
  Codex Responses stream terminal status=failed,
  error='retained prefix exceeds its active KV entitlement'（引擎 HTTP 500）。
- 根因：NINFER_KV_REUSE_HOSTBACKED=1 时，追问会把 ring 里保留的 host 页
  （约 16k token，> 设备池 4096）整体 adopt 进活动 KV，超配额即失败。
- 修复：启动器已设 NINFER_KV_REUSE_HOSTBACKED=0 —— ring 与内容打分保留
  （KVMem 检索照常），但每轮重新 prefill（16k 会话追问 TTFT 约 11s，
  实测 3 轮连续追问全部完成，status=complete / error_retained=False）。
- 想恢复 200ms 级追问：把 --kv-capacity 提到 >= 16384 可装下保留前缀，
  但需 ~1.31 GiB 运行预留，本卡当前余量不足，需先关显存大户。
- ★ 2026-10-11 复发与再修复：有人把两个启动器改成了 =1（为换 chat 命中率），
  Hermes 再次崩，这次更重：会话长到 prompt 51,944 token（> 设备池 17,920）时，
  引擎打印
      adopt host-backed: frontier=33402 need=522 usable=280 host_backed=390
      demote-other short: freed=0 want=390 slots=1 ... dup=0 flushfree
      ERROR engine | worker crash: Paged KV reservation invariant was violated
      ERROR req#15 failed during generation | HTTP 500 | internal error
  同一轮里 req#14（prompt 33,407）虽完成，但同样超池。两个启动器已改回
  NINFER_KV_REUSE_HOSTBACKED=0。
- 别把两件事混起来：
  * **崩溃/500** = REUSE=1 时 adopt 保留前缀失败（本页处方：改回 0）。
  * **静默答错** = 超池时"中段内容消失、HTTP 200 但答案是错的"——这是 KVMem
    要治的病，救它的前提是内容打分真的在跑（日志出现 kvmem_score: SELECT 行）。
  超池本身不等于崩溃；但超池 + REUSE=1 会崩。

"思考了但不输出"修复（2026-10-11 实测通过）：
- 症状：客户端里模型思考链（reasoning）流出来了，但最终答案一个字都没有。
- 根因：思考 token 与答案**共用同一个 max_tokens 预算**，引擎默认没给思考单独设
  上限。难题上（effort=high）模型把整个 32768 输出预算全烧在思考里，正文 0 字符、
  finish_reason=length。实测 273s / 32768 思考 token / 正文 0。
  降档位无效：minimal、low 同样烧光；medium 停在 17773 思考 token，正文仍为 0；
  只有 none 正常。所以不是"客户端 max_tokens 太小"，是思考本身不收敛。
- 修复：两个启动器都加了
      --default-thinking-budget 2048
      --thinking-budget-message "Thinking budget reached. ..."
  引擎到思考预算时会注入该提示，并自动补上思考闭合标记，逼模型转入正文。
  修复后同题同档实测：20s / 正文 769 字 / 答案正常给出。
- 关键约束：**思考预算必须明显小于客户端给你的 max_tokens**。
  实测：客户端 max_tokens 32768（或不传，用引擎默认 32768）→ 预算 4096/2048 都行；
  客户端 max_tokens 2048 → 预算必须降到 1024（2048 会失败、正文为空）。
  若你的客户端固定了小 max_tokens，把启动器里的 --default-thinking-budget 改成 1024。
- 简单题不受影响（实测思考仅 40 token、0.5s 出答案）。
- 详见 引擎优化报告.html 第 9 节。
