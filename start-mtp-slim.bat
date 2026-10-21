@echo off
REM ============================================================
REM  start-mtp-slim.bat -- VRAM-SAVING tier (measured 2026-10-11)
REM  sm_120a engine, RTX 5070 Ti 16 GB, MTP model, 128k context, vision
REM
REM  Model : swift15_iq3xxs_mtp.ninfer  (this folder, engine reports 9.71 GiB resident)
REM          Default since 2026-10-15 (IQ3_XXS). If it is missing the launcher falls
REM          back to Huihui-Qwen3.8-27B-abliterated-IQ3_XXS-mtp.ninfer in this folder.
REM          The old gsq_rco_iq3_s_dflash2_prop.ninfer is NO LONGER in this folder
REM          (verified 2026-10-16) -- do not repoint MODEL at it, the launch dies
REM          at the model-not-found guard with exit code 3.
REM  Port  : 8095 (engine). The warmup proxy owns 8094 and the client keeps pointing
REM          at 8094, so the engine must NOT take 8094 back while the proxy is live.
REM
REM  Use this instead of start-dflash-128k-tuned.bat when you need MORE FREE
REM  VRAM (browsers / Wallpaper Engine / a game running alongside):
REM    - two GiB smaller weights  => ~1.0 GiB more free VRAM
REM    - and it still fits CUDA Graphs (the dflash2 model cannot)
REM  Measured (counting corpus, 1000 out, temp 0):
REM    after-load VRAM 14,586 MiB   (dflash2 tuned: ~15.6 GiB)  => ~1.0 GiB headroom
REM    decode 180.4 tok/s  (vs 286.0 on the dflash2 tuned tier)
REM    prefill 1.54k tok/s | TTFT 738 ms
REM  So: trade ~37% decode for ~1 GiB of VRAM. Pick per workload.
REM
REM  Same memory fix as the tuned tier: --host-kv-mib 4096 (131072 context needs
REM  only ~3.0 GiB of host KV; 16384 cost 12.3 GiB of committed RAM for nothing).
REM  CUDA Graph stays ON here (mtp weights are small enough to fit it).
REM
REM  Vision residency: OVERLAY (changed 2026-10-11). Passing only --vision left
REM  the vision tower RESIDENT in device memory -- that is the engine default,
REM  NOT "on the CPU". Measured on this card (same args, 2 runs each, delta =
REM  after-load VRAM minus desktop baseline):
REM      --vision                 13856 / 13940 MiB   runtime 2.39 GiB  free ~1.0 GiB
REM      ...residency overlay     13259 / 13318 MiB   runtime 2.07 GiB  free ~1.5 GiB
REM      ...residency cpu         13256 / 13270 MiB   runtime 2.07 GiB  free ~1.6 GiB
REM      no --vision at all       13278 / 13239 MiB   runtime 2.06 GiB  free ~1.6 GiB
REM  So the default residency costs ~600 MiB of VRAM. overlay keeps the tower in
REM  host memory and borrows device memory per image => reclaims it, and unlike
REM  'cpu' it keeps the full 16384 merged-token default ('cpu' silently caps
REM  --vision-max-merged at 256 -- that is what the tuned tier runs with).
REM  Cost: vision runs a bit slower and a large image borrows device memory.
REM  If you never send images, drop --vision entirely (same ~600 MiB, no tower).
REM
REM  --adaptive-mtp measured the same 180 tok/s as fixed draft 4 -> left off.
REM
REM  Reasoning effort: none -> low -> HIGH (2026-10-11, user request). mtp CAN think
REM  -- the model is not the limit, the flag is. Levels: none,minimal,low,medium,
REM  high,xhigh,max. Measured: mtp + low = 6/6 (visible think chain, same as the
REM  dflash2 tuned tier); mtp + none missed one multi-step algebra item.
REM
REM  WARNING -- unbounded thinking starves the answer (measured 2026-10-11).
REM  Thinking tokens share the SAME max_tokens budget as the answer. With effort
REM  high on a hard prompt the model thought for the ENTIRE 32768-token output
REM  budget and wrote ZERO answer text (272 s, finish_reason=length, content
REM  empty) -- the client shows "thinking, but no output". Same runaway at
REM  low/minimal; medium stopped early but still wrote nothing. Only "none"
REM  always answered.
REM
REM  Fix = a thinking-budget circuit breaker (engine force-closes the reasoning
REM  block so the answer can start):
REM    --default-thinking-budget 2048
REM    --thinking-budget-message "..."
REM  Measured, effort high, same hard prompt: 272 s / 0 chars before,
REM  20 s / 769 chars with the answer after.
REM
REM  Keep the budget WELL BELOW the max_tokens your client sends:
REM    client max_tokens 32768 (or unset): budget 4096 ok, 2048 ok
REM    client max_tokens 2048           : budget 1024 required (2048 failed)
REM  If your client pins a small max_tokens, lower this to 1024.
REM ============================================================
setlocal
REM  /silent  -- suppress every pause (used by the desktop shortcut, which runs
REM  this script with a hidden console). Without this, a hidden-run "pause"
REM  would hang forever waiting for a key that can never be pressed.
set "SILENT="
if /i "%~1"=="/silent" set "SILENT=1"
set "ROOT=%~dp0"
REM 2026-10-13: MODEL default REPOINTED. It used to be hardcoded to
REM   D:\infer-engine-sm120a\swift-qwen38-rco-iq3s.ninfer
REM That whole folder is gone from this machine (verified 21:26 by searching
REM C:, D: and E: for *.ninfer, and by inspecting the recycle bins), so every
REM launch died at the "model not found" guard with exit code 3 -- which the
REM tray reports as "launch failed / engine process exited".
REM The model actually in use is the pack's own gsq artifact. Verified working
REM with the full production arg set on 2026-10-13 21:30:
REM   engine ready | qwen3.8-27b-gsq-rco-iq3s | weights 11.3 GiB | total 5.7s
REM   capacity | KV 81,920 tokens, rk4v4, explicit | runtime 2.52 GiB | free 672 MiB
REM   context cache | host 8 states, 4.00 GiB KV | private 2 | shared 7
REM Same 11.3 GiB weights and same --spec mtp support as the old swift artifact,
REM so every 2026-10-13 tuning result applies unchanged.
if defined MODEL goto :model_ok
set "MODEL=%ROOT%swift15_iq3xxs_mtp.ninfer"
REM fallback: if the primary model is missing, use the other local model.
REM The old D:\infer-engine-sm120a\ path is gone -- do not point back at it.
if not exist "%MODEL%" set "MODEL=%ROOT%Huihui-Qwen3.8-27B-abliterated-IQ3_XXS-mtp.ninfer"
:model_ok
if not exist "%MODEL%" (
  echo REFUSE: model not found: "%MODEL%"
  echo         Put swift15_iq3xxs_mtp.ninfer next to this script, or the Huihui
  echo         abliterated artifact, or run:
  echo           set MODEL=D:\path\to\model.ninfer  ^&^& start-mtp-slim.bat
  if not defined SILENT pause
  exit /b 3
)
set "CUDA_BIN=%ROOT%engine"
set "PATH=%CUDA_BIN%;%CUDA_BIN%\x64;%PATH%"
REM 2026-10-19: NINFER_KV_WINDOW REMOVED -- the KVMem ring is now OFF.
REM Measured on this card (pool 184320 == max-context, so the ring never has a
REM demotion job to do; prompt peaks around 68k are nowhere near the pool):
REM   engine VRAM   14558 to 14372 MiB  (-186)
REM   cold prefill  1.59k to 1.63-1.66k tok/s
REM   cached TTFT   98 to 70 ms on an identical prompt
REM   prefix reuse  UNCHANGED (99.9 pct hit both with and without the ring,
REM                 including shared-prefix catalog and checkpoint states)
REM Also: NINFER_KV_RING was never a real variable - the binary has zero hits
REM for it, the ring is opened by NINFER_KV_WINDOW alone. Dropped the dead line.
REM To restore the ring: add  set "NINFER_KV_WINDOW=16384"  back here.
REM Spare VRAM now allows pool 196608 (measured OK, free 342 MiB) if ever needed.
REM 2026-10-19: SWITCHED TO THE HYBRID PREFIX CACHE (cross-restart reuse).
REM Why: the first turn after an engine restart was a full prefill (18k prompt =
REM 11.8 s). The hybrid cache can save its blocks on a graceful shutdown and load
REM them back at startup. Measured on this card:
REM   engine restarts, "prefix cache restored | 284 blocks | 642.0 MiB | 273 ms"
REM   first turn 18,241 tokens: 11.84 s -> 0.16 s (cache 18,232 = 100%)
REM   same prefix with a different tail: 0.16 s (99.9%) -- legacy was 0% here
REM Cost, measured: pool stays 184,320 (same as before), host RAM is 4 GiB via
REM host-cache-mib (the old host-kv-mib was also 4 GiB), graceful stop 0.5 s.
REM host-cache-mib costs ONLY system RAM, never VRAM: 2/4/8 GiB all measured the
REM same pool (184,320) and the same engine VRAM (14,412-14,423 MiB). 4 GiB holds
REM 2-3 distinct prefixes; a 36k-token prompt is about 1.1 GiB of cache file.
REM The cache file lives in logs\ and grows to roughly 0.6-1.2 GiB.
REM
REM REQUIRED PAIRING (both were verified together):
REM   --use-alt-prefix-caching  +  --prefix-cache-file
REM   It CANNOT be combined with --derive-session-keys or --max-shared-prefixes
REM   ("... configures the default prefix cache and cannot be combined with
REM   --use-alt-prefix-caching"), so both were removed.
REM The cache is only written on a CLEAN shutdown. The tray host now stops the
REM engine with CTRL_BREAK (AttachConsole + GenerateConsoleCtrlEvent) instead of
REM taskkill /f; a forced kill still works but simply skips the save.
REM To revert to the legacy checkpoint catalog: restore the two removed flags,
REM drop --use-alt-prefix-caching / --prefix-cache-file / --host-cache-mib and
REM put --host-kv-mib 4096 back. Backup: .workbuddy\_trash\hybrid-20261019\
REM
REM  NINFER_KV_REUSE_HOSTBACKED=0  (Hermes fix; was wrongly 1, re-fixed 2026-10-11)
REM  With =1, a follow-up whose retained host-KV prefix no longer fits the device
REM  pool makes the engine ADOPT that whole prefix into the active KV. Once the
REM  session grows past the pool (seen: prompt 51,944 tok vs pool 17,920) the
REM  adopt cannot fit ("adopt host-backed: need=522 usable=280"), demote frees
REM  nothing ("demote-other ... freed=0"), and the engine dies with "worker crash:
REM  Paged KV reservation invariant was violated" and req failed, HTTP 500, which
REM  Hermes shows as a broken reply. =0 keeps the KVMem ring and content scoring
REM  but re-prefills every turn: slower follow-up TTFT, but no 500 and no crash.
REM  See fix-summary.html and README-128k.txt section "Hermes zhuiwen 500 fix".
set "NINFER_KV_REUSE_HOSTBACKED=0"
REM 2026-10-20: fusion engine ring. Logical context 262144, device pool 163840.
REM KV_WINDOW arms the ring; retrieval only fires when a prompt exceeds the pool,
REM so requests under 160K stay on the dense path (measured decode 119.9 vs the
REM stock engine's 120.8 medians). NINFER_HOST_PAGEABLE lets the overflow pages
REM live in host RAM instead of failing.
REM 2026-10-20: fusion also carries the kvmem_score selector, whose default budget is
REM only 32768 tokens (512 pages). On an over-pool prompt that discarded 84% of the pages
REM while the four-bucket ring budget said it kept 1279 -- two mechanisms fighting.
REM Raise the scorer budget to 2048 pages (131072 tokens) so it matches the 2560-page pool.
REM 2026-10-20: head protection. PROTECT_FRAC is an INTEGER PERCENT (5 = 5%), not a
REM fraction -- 0.05 makes the engine refuse to start. With PROTECT=1 + FRAC=5 the log
REM shows the protect bucket climbing to 128 pages (5% of the 2560-page pool), which
REM keeps the start of a long prompt resident instead of letting the scorer evict it.
set "CUBLAS_WORKSPACE_CONFIG=:16:8"

if not exist "%ROOT%engine\ninfer-serve-120a.exe" (
  echo REFUSE: engine\ninfer-serve-120a.exe is missing from this pack
  if not defined SILENT pause
  exit /b 4
)
set "ENGINE=%ROOT%engine\ninfer-serve-120a.exe"

echo [engine] %ENGINE%
echo [model ] %MODEL%
echo [note  ] SLIM: mtp + CUDA graph, host-kv 4096, vision=overlay, reasoning=high, think-budget 2048 (~1.6 GiB more free VRAM)

REM 2026-10-12: --kv-capacity 17,920 to 49,152, then to 69,632; --kv-dtype k8v4 to rk4v4.
REM  Why: the WorkBuddy client sends a 33k-46k token prompt every request (measured
REM  in ~/.workbuddy/logs: prompt_tokens 33,309..45,890). With a 17,920 pool the
REM  engine said "prompt exceeds the resident Device KV pool ... part of the prompt
REM  is therefore not resident" and reused NOTHING: every turn re-prefilled all
REM  39k tokens (TTFT 31 s, and +30 tokens of new text still cost 33 s).
REM  Measured with the pool raised to 49,152 + rk4v4 (bench spec-W9):
REM    turn 1  : cache 0     TTFT 23.5 s  prefill 1.67k tok/s   (was 30.9 s / 1.27k)
REM    turn 2+ : cache 99.9% TTFT 0.18 s  wall 0.31 s           (was 33 s)
REM  Cost: KV runtime 2.55 GiB, after-load VRAM 15,189 MiB, free ~1.10 GiB.
REM  Also rk4v4 is CHEAPER than k8v4 here (2.55 vs 2.86 GiB) and prefills ~30%
REM  faster, so this is not a trade -- it is a straight win on both axes.
REM
REM 2026-10-12 (later): pool 49,152 raised to 69,632.
REM  Why: the client session keeps growing from 38k to 68k tokens. The engine only
REM  keeps a checkpoint while the whole prompt fits the pool, so the first turn that
REM  exceeded 49,152 discarded the checkpoint and EVERY later turn fell back to a
REM  full prefill (measured on the client log: prompt 63,549 cache 0% TTFT 45.6 s,
REM  67,445 cache 0% TTFT 50.0 s, 68,505 cache 0% TTFT 52.5 s).
REM  Same growing sequence with the pool at 69,632 (bench rungrow.sh, spec-W12):
REM    prompt 37,433 cache  0.0%  TTFT 22.3 s
REM    prompt 49,330 cache 75.9%  TTFT  9.1 s
REM    prompt 63,549 cache 77.6%  TTFT 11.8 s   (was 45.6 s)
REM    prompt 67,445 cache 94.2%  TTFT  3.6 s   (was 50.0 s)
REM    prompt 68,505 cache 98.4%  TTFT  1.2 s   (was 52.5 s)
REM  69,632 (1088 of 2048 pages) is the HARD ceiling for this card: 73,728 wants
REM  3,200,817,408 B of runtime and only about 3,192,795,136 B are available
REM  (short by 17 MB), and --kv-capacity auto hands out a useless 17,536.
REM  Cost: KV runtime 2.91 GiB, after-load VRAM about 15,449 MiB, free about 0.75 GiB.
REM  IMPORTANT: launching can fail if the desktop holds more VRAM than usual
REM  (close browsers or games first). The tray turns red and stays 20 s.
REM  Tested and REJECTED (bench spec-W11): --auto-long-anchors plus
REM  --context-cache-policy rolling plus --thorough-admission-search changed
REM  nothing on an over-pool prompt and then CRASHED the worker with
REM  "worker crash: Paged KV reservation invariant was violated" (HTTP 500, then 503).
REM
REM 2026-10-13: three changes. All measured, evidence in .workbuddy/bench/spec-W13..W16.
REM
REM  (1)+(2): --kv-capacity 69,632 to 81,920, plus --cuda-graph-allowance-mib 128.
REM  The help text says cuda-graph-allowance-mib is "taken from the KV sizing
REM  budget" (default 0 = computed per profile), i.e. the default silently parks
REM  roughly 0.7 GiB of that budget on CUDA Graph driver state. Capping it at
REM  128 MiB gave the budget back, and the pool ceiling moved 69,632 to 98,304:
REM      69,632 (old, allowance auto)  runtime 2.91 GiB  free 881 MiB
REM      73,728 + cg128                runtime 2.37 GiB  free 874 MiB
REM      81,920 + cg128                runtime 2.52 GiB  free 657 MiB   = this tier
REM      90,112 + cg128                runtime 2.66 GiB  free 509 MiB
REM      98,304 + cg128                runtime 2.81 GiB  free 359 MiB
REM     114,688 + cg128  REFUSED, short by 51 MB.  131,072 refused at cg 128/64/0.
REM  decode is NOT hurt by cg128: same five-turn bench, 170.3/167.9/163.7/122.2/98.3
REM  tok/s, versus 170.6/168.2/164.3/123.2/98.7 with the allowance on auto.
REM  A 1024-token single generation also ran clean. So CUDA Graphs still work.
REM
REM  (3): to 7, plus --derive-session-keys.  THE BIG ONE.
REM  A NEW conversation whose prompt starts with the same stable preamble
REM  (client system prompt + tool defs + skills + memory) now REUSES it instead
REM  of re-prefilling 30k tokens. bench spec-W13/W16, three sessions sharing a
REM  27,978-token preamble:
REM      session 1  cache  0.0%                    TTFT 15.6 s
REM      session 2  cache 99.9% (shared prefix)    TTFT 204 ms
REM      session 3  cache 99.9% (shared prefix)    TTFT 147 ms
REM  It does not disturb same-session growth: five turns 37k to 68k still give
REM  cache 75.9/77.6/94.2/98.4% and TTFT 8.9/11.4/3.4/1.1 s.
REM  --derive-session-keys is REQUIRED. Opening the shared catalog alone crashes
REM  the worker (measured twice: "active KV snapshot full page is not stable" and
REM  "page ownership is not ordered"). Never set shared-prefixes without it.
REM
REM  WARNING, measured, keep in mind: --host-state-slots 0 stops that crash but
REM  also kills shared-prefix reuse entirely (cache 0%), so it is not a fix.
REM
REM  WARNING, measured: if a request whose prompt EXCEEDS the pool is followed by
REM  any shared-prefix reuse, the worker crashes with "Paged KV single-page
REM  materialization exceeds reservation" (HTTP 500, then the server is down).
REM  --context-cache-policy rolling does not prevent it. This is why the pool was
REM  raised instead of relying on 69,632: it now covers the client's measured
REM  text peak (68,101 tokens) plus about six 1080p screenshots (2,104 tokens
REM  each, measured with a real 1920x1080 PNG).
REM  IF THE ENGINE EVER DIES WITH THAT MESSAGE: set --max-shared-prefixes back to
REM  0 first, then raise --kv-capacity. Do not add more cache flags.
REM
REM 2026-10-14: --kv-dtype rk4v4 to nvfp4. Measured, evidence in
REM .workbuddy/bench/probe-dtype.py and dtype-*.log (pool 32,768, same prompt):
REM   dtype           decode t/s   prefill t/s   KV runtime
REM   rk4v4               97.3        1,840        1.65 GiB   (was)
REM   nvfp4              140.3        1,625        1.54 GiB   (now)
REM   fp8                149.2        1,631        2.01 GiB
REM   rk8v4               96.1        1,870        1.91 GiB
REM   rk2v4-e8            90.4        1,842        1.51 GiB
REM   bf16 (default)     REFUSED, needs 3.07 GiB runtime at pool 32,768 alone
REM nvfp4 is decode +44% AND 0.11 GiB smaller at pool 32,768; scaled to pool
REM 81,920 that is about 0.28 GiB saved, so free should go from ~120 MiB to
REM ~400 MiB. Prefill pays about 12%. The rk family is the slow one here (all
REM 90-97 t/s) while fp8 and nvfp4 reach 140+, which points at a hardware
REM dequant path for fp8/fp4 on Blackwell that the rk family cannot use.
REM GOTCHA: bf16 is the documented default, but it does NOT fit this card even
REM at pool 32,768. Never delete --kv-dtype; the default refuses to start.
REM Both rk4v4 and nvfp4 are 4-bit, so the quality risk class is unchanged.
REM 2026-10-14 (later): --max-context 131072 to 81920, ALIGNED WITH THE POOL.
REM Root cause of a real 503 outage at 11:09 that day:
REM   req#20 prompt 68,196 | req#21 70,937 | req#22 71,578 (cache ~100%, all fine)
REM   req#24 with 56 messages then blew past the 81,920 pool:
REM     ERROR engine | worker crash: Paged KV reservation invariant was violated
REM     ERROR req#24 ... HTTP 500, then req#25/req#26 HTTP 503 service unavailable
REM The trap was the mismatch: /v1/models advertised context_window 131,072
REM (taken from --max-context) while the KV pool only held 81,920, so the client
REM kept growing the conversation and only found out by crashing into the pool edge.
REM With max-context equal to kv-capacity the client is told the real ceiling and
REM compacts earlier. Measured: a 53,829-token two-turn session reuses 100%, and the
REM pool carries a 71k conversation comfortably, so 81,920 is a workable ceiling.
REM Raising BOTH to 98,304 is possible but leaves only about 360 MiB of free VRAM.
REM NEVER set --max-context above --kv-capacity.
REM 2026-10-14 (even later): the context ceiling is a HARD TRIANGLE. Read first.
REM   (a) kv-capacity must NOT exceed max-context; enforced at startup:
REM         FATAL, kv_capacity is outside the usable range for max_context
REM         and max_concurrency
REM   (b) max-context is what /v1/models advertises as context_window, so it also
REM       decides when the client compacts its history.
REM   (c) one request needs room for its prompt PLUS its output inside the pool.
REM (a) and (b) together force kv-capacity == max-context. Then (c) fails only for
REM a prompt sitting exactly at the ceiling with nothing left for the answer --
REM which is what hit at 11:13 on 10-14:
REM   Paged KV pool cannot resize the page reservation: usable 1280 pages,
REM   allocated 523, reserved 757, requested 1   (523+757 = 1280 = whole pool)
REM So do NOT shrink max-context to create headroom: that makes kv-capacity
REM illegal and the engine refuses to start (tried 65536 with 81920, exit 1).
REM Raise BOTH together instead, after freeing VRAM:
REM   81920 to 90112 costs about 143 MiB more; 98304 costs about 358 MiB more.
REM Current setting: 81920 with 81920.
REM 2026-10-14 (final for today): pool raised 81920 to 98304, plus --gdn-state-fp16.
REM Both numbers moved together, per the TRIANGLE note above.
REM Measured on this card with the desktop at about 1375 MiB:
REM   81920             runtime 2.44 GiB, free about 600 MiB
REM   98304             runtime 2.74 GiB, free 317 MiB
REM   98304 + gdn-fp16  runtime 2.60 GiB, free 363 MiB   = this tier
REM   114688            FAILS to start (with or without gdn-fp16), so 98304 is the ceiling
REM --gdn-state-fp16 saves 143 MiB (help: "keep the GDN recurrent state in FP16").
REM Why it matters: an 82k-token conversation now fits with about 16k tokens to spare,
REM whereas at 81920 a prompt that reached the ceiling had nothing left for its answer
REM and died with "Paged KV pool cannot resize the page reservation".
REM NEXT STEP if 98304 is still not enough: a smaller artifact. Candidate:
REM   raghualgt/Qwen3.8-27B-OrcaRouter-GSQ-RCO-IQ3_XXS-NInfer
REM     qwen3.8-27b-orcarouter-iq3-xxs-vision-mtp.ninfer   10.33 GiB (13.99 now)
REM   its model card publishes a tested RTX 5080 (16 GB) profile using
REM   --max-context 147456 --kv-dtype rk4v4 --gdn-state-fp16 --vision,
REM   roughly 1.8x our pool. Needs a 10 GiB download, and this engine must accept
REM   its artifact identity, which is not verified yet.
REM  THIS FILE MUST STAY ASCII-ONLY. A single non-ASCII byte breaks cmd parsing
REM  of the whole line (measured: the engine never started and cmd spammed
REM  "'M' is not recognized as an internal or external command").
REM  2026-10-16 PORT SWAP v2: engine on 8095, warmup proxy owns 8094 so the client
REM  (models.json) needs NO config change. --request-log-jsonl is for diagnosing
REM  context-compression crashes. To revert: put 8094 back below, drop the
REM  request-log flags, and set PROXY_LISTEN back to 8093 in ninfer-tray.py.
REM  Backup of the pre-swap file: .workbuddy\_trash\port-swap-20261016\
REM  2026-10-16 (all numbers below are measured on THIS card; this file is the truth):
REM   (1) --cuda-graph-allowance-mib 32 raised to 192.
REM   The engine warned on run after run: "CUDA graphs used 38.9 .. 207.2 MiB but
REM   the KV sizing allowed 32.0 MiB" (21 occurrences in .workbuddy/bench/*.log,
REM   plus every 2026-10-16 probe run). The 32 was fake accounting: the overshoot
REM   came out of the free-VRAM margin. Measured 2026-10-16 (weights 9.71 GiB,
REM   pool 184,320, vision overlay, desktop about 1.07 GiB):
REM     cg32   runtime 4.07 GiB  free 462.5 MiB   + WARN, allowance exceeded
REM     cg192  runtime 4.23 GiB  free 406.5 MiB   this is what we now run
REM     cg256  runtime 4.29 GiB  free 395.0 MiB
REM     cg192 with pool 176,128 gives runtime 4.08 GiB  free 580.0 MiB
REM     cg256 with pool 180,224 gives runtime 4.21 GiB  free 476.4 MiB
REM   So cg192 costs about 56 MiB of free VRAM and buys honest accounting.
REM   If starts ever fail under a heavy desktop, drop BOTH to 180,224 with cg256.
REM
REM   (2) Every "98304 is the ceiling / 114688 FAILS" note above is OBSOLETE.
REM   Those were measured with the 11.3 GiB IQ3_S artifact and cg128. The IQ3_XXS
REM   candidate listed further down was adopted on 2026-10-15 (9.71 GiB resident),
REM   and with cg192 this card starts 184,320 (2,880 pages) today.
REM
REM   (3) Prefill is AT ITS CEILING on this engine -- stop looking there. Measured
REM   2026-10-16 on a 26,680-token cold prefill (baseline 1,254.1 tok/s):
REM     --no-prefill-a8                              1,252.6 tok/s  (noise)
REM     --prefill-cublas --no-prefill-cublas-projections  1,257.9    (noise)
REM     --mtp-attention-window 512                   1,266.3 tok/s  (noise)
REM     --lm-head-draft                              1,139.9 tok/s  WORSE, +0.29 GiB
REM   And these are REFUSED by this artifact / this build (do not retry):
REM     --lm-head-q4 / --lm-head-q6 / --embedding-q4 / --embedding-q6
REM         "requires text/output_head (token_embedding) to be stored as row-split Q8_G32"
REM     --wddm-evictable-budget
REM         "needs a Windows build with NINFER_D3D12_RESIDENCY=ON"
REM   --device-profile calibrate works but costs 45 s of startup for +-1.7% (noise).
REM
REM   (4) Clean numbers for reference (2026-10-16): startup 3.3-3.9 s; decode with
REM   MTP and no thinking 160.6 tok/s (acceptance 96%, 3.82 tokens/round); the same
REM   model under production load (thinking on, 35k prompt) does 90.1 tok/s median.
REM  2026-10-16 measured: an unopenable request log is FATAL, not a warning --
REM  "FATAL server failed during startup | failed to open request JSONL log for
REM  append: ...\logs\requests.jsonl" leaves the tray red and the engine down.
REM  So make sure the folder exists. A locked or read-only file behaves the same;
REM  if that ever happens, delete it or drop the two --request-log-* flags below.
if not exist "%ROOT%logs" mkdir "%ROOT%logs" 2>nul

set "ENGINE_ARGS="%MODEL%" --host 127.0.0.1 --port 8095 --model-id qwen3.8-27b --max-context 196608 --kv-capacity 196608 --kv-dtype rk2v4-e8 --gdn-state-fp16 --cuda-graph-allowance-mib 192 --prefill-chunk 512 --spec mtp --draft-tokens 4 --vision --vision-residency overlay --media-cache-mib 0 --default-max-tokens 32768 --default-reasoning-effort high --default-thinking-budget 2048 --thinking-budget-message "Thinking budget reached. Stop deliberating and write your final answer now." --max-concurrency 1 --presence-penalty 0 --thinking-presence-penalty 1.0 --temperature 0.7 --top-p 0.9 --top-k 20 --request-log-jsonl "%ROOT%logs\requests.jsonl" --request-log-max-mib 256"

REM  Engine console window (changed 2026-10-12, user request):
REM  the engine ALWAYS gets its own console window, started MINIMIZED.
REM  The tray host (.workbuddy\tools\ninfer-tray.py) hides it once the model is
REM  loaded, so the desktop/taskbar stay clean -- and left-clicking the tray
REM  icon shows it again (that IS the "running code" window: loading progress,
REM  per-request log and the stats panel from --log-stats-panel).
REM  NOTE: do NOT close that window -- closing a console kills the engine.
REM  Use the tray menu "hide engine window" to put it away instead.
REM  (This replaces the previous NINFER_NO_WINDOW headless mode; the engine has
REM  no --log-file option, so a console is the only way to watch it live.)
REM 2026-10-19: optional console capture. Set NINFER_CAPTURE_LOG=1 before launching
REM and the engine output is also written to logs\engine-console.log (the console
REM window then shows nothing). Diagnostics only -- a crashed engine leaves no log
REM at all, which is what made the 2026-10-19 11:35 exit impossible to explain.
REM 2026-10-19: optional console capture. Set NINFER_CAPTURE_LOG=1 before launching
REM and the engine output is also written to logs\engine-console.log (the console
REM window then stays blank). Diagnostics only -- a crashed engine leaves no log at
REM all, which is what made the 2026-10-19 11:35 and 19:44 exits hard to explain.
if defined NINFER_CAPTURE_LOG goto :capture
start "AI engine (port 8095)" /min "%ENGINE%" %ENGINE_ARGS%
goto :launched
:capture
start "AI engine (port 8095)" /min cmd /c ""%ENGINE%" %ENGINE_ARGS% > "%ROOT%logs\engine-console.log" 2>&1"
:launched

powershell -NoProfile -Command "$t=(Get-Date).AddSeconds(30); do { try { $r=Invoke-WebRequest -Uri 'http://127.0.0.1:8095/v1/models' -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop; if ($r.StatusCode -eq 200) { exit 0 } } catch {}; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $t); exit 1"
if not errorlevel 1 (
  echo.
  echo [ok] engine ready on 8095 ^(client talks to 8094 through the warmup proxy^) - left-click the tray icon for its log window.
  if not defined SILENT pause
  exit /b 0
)
echo [error] engine did not become ready - left-click the tray icon for its log window.
if not defined SILENT pause
exit /b 6
