# Ollama: Enable GPU (RTX 4060)
$ErrorActionPreference = "Stop"

Write-Host "Enabling GPU acceleration for Ollama..." -ForegroundColor Cyan

# Remove the CPU-only override
[Environment]::SetEnvironmentVariable("OLLAMA_NUM_GPU", $null, "User")

Write-Host "OLLAMA_NUM_GPU override removed."
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "1. Right-click Ollama tray icon -> Quit Ollama"
Write-Host "2. Start Ollama again from the Start menu"
Write-Host "3. In JARVIS, try your model again. It should be MUCH faster, Sir!" -ForegroundColor Green
Write-Host ""
Write-Host "If you experience crashes, it might be due to model size (32B is heavy for 8GB VRAM)."
Write-Host "Recommendation: Use Qwen 2.5 14B or 7B for best stability on RTX 4060."
pause
