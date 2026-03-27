@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0ollama_pull_qwen32.ps1"
pause
