@echo off
title Ollama CPU only
set OLLAMA_NUM_GPU=0
set OLLAMA_NUM_THREAD=6
echo Setting OLLAMA_NUM_GPU=0 in registry (for Start menu Ollama)...
setx OLLAMA_NUM_GPU 0 >nul 2>&1
echo.
echo IMPORTANT: Close ALL Ollama first:
echo   - Tray icon: right-click - Quit Ollama
echo   - Task Manager: end any "ollama.exe" or "Ollama" processes
echo Then press any key to start Ollama in this window with CPU only.
pause >nul
echo.
echo OLLAMA_NUM_GPU=0. Keep this window open.
echo.
set "OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"
if not exist "%OLLAMA_EXE%" set "OLLAMA_EXE=ollama.exe"
"%OLLAMA_EXE%" serve
echo.
echo If you still get CUDA_Host buffer error: restart PC (so Ollama picks up OLLAMA_NUM_GPU=0), then start Ollama from Start menu.
pause
