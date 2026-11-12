$ErrorActionPreference = "Stop"

$env:QWEN_MODEL = if ($env:QWEN_MODEL) { $env:QWEN_MODEL } else { "qwen2.5-coder:latest" }
$env:QWEN_PROXY_PORT = if ($env:QWEN_PROXY_PORT) { $env:QWEN_PROXY_PORT } else { "8765" }
$env:OLLAMA_BASE_URL = if ($env:OLLAMA_BASE_URL) { $env:OLLAMA_BASE_URL } else { "http://127.0.0.1:11434/v1" }

Write-Host "Starting Qwen Codex Adapter..."
Write-Host "Model: $env:QWEN_MODEL"
Write-Host "Proxy: http://127.0.0.1:$env:QWEN_PROXY_PORT"
Write-Host "Ollama: $env:OLLAMA_BASE_URL"

python "$PSScriptRoot\qwen_codex_adapter.py"
