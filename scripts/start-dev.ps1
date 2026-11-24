param([switch]$Docker)
$ErrorActionPreference = "Stop"
if ($Docker) { docker compose up --build; exit $LASTEXITCODE }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
$settingsLines = Get-Content .env
$tokenLine = $settingsLines | Where-Object { $_ -match '^API_AUTH_TOKEN=' } | Select-Object -First 1
$apiUrlLine = $settingsLines | Where-Object { $_ -match '^NEXT_PUBLIC_API_URL=' } | Select-Object -First 1
if ($tokenLine) { $env:API_AUTH_TOKEN = $tokenLine.Substring('API_AUTH_TOKEN='.Length) }
if ($apiUrlLine) { $env:NEXT_PUBLIC_API_URL = $apiUrlLine.Substring('NEXT_PUBLIC_API_URL='.Length) }
$python = if (Test-Path '.venv\Scripts\python.exe') { (Resolve-Path '.venv\Scripts\python.exe').Path } else { 'python' }
Write-Host "Ensure PostgreSQL is running and Ollama serves $((Get-Content .env | Select-String '^OLLAMA_MODEL=').ToString().Split('=')[1])."
$backendPath = Join-Path $PWD 'backend'
Start-Process -FilePath $python -ArgumentList '-m','uvicorn','app.main:app','--reload','--port','8000' -WorkingDirectory $backendPath -WindowStyle Hidden
Start-Process -FilePath $python -ArgumentList '-m','app.workers.runner' -WorkingDirectory $backendPath -WindowStyle Hidden
Set-Location frontend
if (-not (Test-Path 'node_modules')) { npm.cmd install }
npm.cmd run dev
