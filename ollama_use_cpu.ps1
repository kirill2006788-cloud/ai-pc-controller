# Ollama: force CPU only (fix "unable to allocate CUDA_Host buffer")
$ErrorActionPreference = "Stop"
[Environment]::SetEnvironmentVariable("OLLAMA_NUM_GPU", "0", "User")
Write-Host "OLLAMA_NUM_GPU=0 set for current user."
Write-Host ""
Write-Host "Next: Quit Ollama (tray icon -> Quit), then start Ollama again."
Write-Host "In JARVIS select Ollama - Qwen 2.5 32B. Model will run on CPU (slower, no GPU error)."
