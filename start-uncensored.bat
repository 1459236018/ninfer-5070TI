@echo off
REM ============================================================================
REM  start-uncensored.bat -- launcher for the abliterated (uncensored) model
REM
REM  This is a thin WRAPPER: it only sets MODEL and hands off to the production
REM  launcher (start-mtp-slim.bat), so every engine parameter (pool size, dtype,
REM  CUDA graph, vision, thinking budget ...) stays defined in ONE place.
REM
REM  Model : Huihui-Qwen3.8-27B-abliterated-Swift-1.5-GSQ-RCO-IQ3_XXS-mtp.ninfer
REM          (10.33 GiB, same quant recipe as the default model, abliterated weights)
REM
REM  Measured 2026-10-16 on this machine (36-question eval, greedy):
REM    default model     27/36      this model     23/36   (-11%)
REM    refusal test       0/5       refusal test    0/5     (both already comply)
REM  => kept as an OPTION only. The default launcher is the better model.
REM
REM  NOTE: engine binds port 8094, so ONLY ONE launcher may run at a time.
REM        Stop the running engine first (tray menu -> stop) before switching.
REM ============================================================================
setlocal
set "MODEL=%~dp0Huihui-Qwen3.8-27B-abliterated-IQ3_XXS-mtp.ninfer"
if not exist "%MODEL%" (
  echo.
  echo ERROR: uncensored model not found:
  echo        %MODEL%
  echo.
  echo Download it from:
  echo   https://huggingface.co/fyb1214/Huihui-Qwen3.8-27B-Abliterated-Swift-1.5-GSQ-RCO-NInfer
  echo   file: Huihui-Qwen3.8-27B-abliterated-Swift-1.5-GSQ-RCO-IQ3_XXS-mtp.ninfer
  echo.
  pause
  exit /b 3
)
call "%~dp0start-mtp-slim.bat" %*
exit /b %errorlevel%
