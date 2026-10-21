@echo off
rem ============================================================
rem  Hermes request-format fix proxy: 8096 forwards to the engine.
rem
rem  What it does: kvmem-responses-normalizer.py listens on 8096 and forwards to
rem  the engine, dropping reasoning entries the engine does not accept.
rem
rem  Usage:
rem    1) start the engine first (start-mtp-slim.bat, or the desktop icon)
rem    2) double-click this script
rem    3) point Hermes Desktop's backend URL at http://127.0.0.1:8096
rem
rem  2026-10-16 fixes:
rem    * upstream port 8094 changed to 8095. The warmup proxy owns 8094 now, so
rem      sending Hermes traffic there would push it through a proxy tuned for the
rem      other client (that one rewrites tool lists and folds content arrays).
rem    * this file used to be GBK-encoded Chinese. Every .bat in this pack must be
rem      pure ASCII: one non-ASCII byte can make cmd mangle a line and stop the
rem      script from running at all.
rem    * the old line called plain "python", which on this machine resolves to the
rem      Microsoft Store stub and fails with "Python was not found". The script now
rem      prefers the bundled WorkBuddy interpreter and only then falls back to PATH.
rem ============================================================
cd /d "%~dp0"

set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [hermes] listen 127.0.0.1:8096, forward to engine 127.0.0.1:8095
echo [hermes] interpreter: %PY%
echo [hermes] keep this window open while Hermes Desktop is in use.

"%PY%" kvmem-responses-normalizer.py 127.0.0.1:8095 8096 proxy.log
pause