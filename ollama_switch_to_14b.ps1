# Remove heavy 32B model and install lighter, faster model (Qwen 2.5 14B)
# Run from project folder: .\ollama_switch_to_14b.ps1

Write-Host "Removing qwen2.5:32b-instruct..."
ollama rm qwen2.5:32b-instruct 2>$null
if ($LASTEXITCODE -ne 0) { Write-Host "Model not found or already removed." }

Write-Host ""
Write-Host "Pulling qwen2.5:14b-instruct (~9 GB, faster, still smart)..."
ollama pull qwen2.5:14b-instruct

Write-Host ""
Write-Host "Done. In JARVIS select Ollama - Qwen 2.5 14B or model qwen2.5:14b-instruct"
