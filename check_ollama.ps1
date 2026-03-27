# Проверка Ollama: запущен ли сервис и есть ли модель qwen2.5:32b-instruct
# Запуск: PowerShell -ExecutionPolicy Bypass -File check_ollama.ps1
$base = $env:OLLAMA_BASE_URL
if (-not $base) { $base = "http://localhost:11434" }
$base = $base.TrimEnd("/")

Write-Host "Ollama: проверка $base"
try {
    $r = Invoke-RestMethod -Uri "$base/api/tags" -TimeoutSec 5 -ErrorAction Stop
    $models = @($r.models)
    Write-Host "Ollama запущен. Моделей: $($models.Count)"
    $qwen = $models | Where-Object { $_.name -like "*qwen*32*" }
    if ($qwen) {
        Write-Host "OK: найдена модель $($qwen.name) - можно выбирать в JARVIS: Ollama - Qwen 2.5 32B"
    } else {
        Write-Host "Модель qwen2.5:32b-instruct не найдена. Запусти: .\ollama_pull_qwen32.ps1"
    }
} catch {
    Write-Host "Ollama не отвечает. Запусти приложение Ollama (Пуск - Ollama) и повтори."
    exit 1
}
