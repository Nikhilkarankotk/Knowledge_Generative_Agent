# Forces the Hackathon Knowledge_Generative_Agent copy (imune to cwd).
# Guarantees the app uses DB "Knowledge_Gen_Agent" regardless of where this is run from.
$ErrorActionPreference = "Stop"

$projectRoot = $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

# Free port 8000 first so a leftover/stale server (e.g. the Portfolio copy)
# never blocks the correct backend from binding.
$stale = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
foreach ($conn in $stale) {
    try {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop
        Write-Host "Freed port 8000 (stopped stale server PID $($conn.OwningProcess))"
    } catch {
        Write-Host "Could not stop PID $($conn.OwningProcess): $_"
    }
}
if ($stale) { Start-Sleep -Seconds 2 }

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error ".venv not found at $python. Run: python -m venv .venv && pip install -e '.[dev]'"
    exit 1
}

Set-Location -LiteralPath $projectRoot
Remove-Item Env:MISTRAL_CHAT_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
$env:DB_NAME = "Knowledge_Gen_Agent"

Write-Host "Starting Knowledge_Gen_Agent backend from $projectRoot"
Write-Host "Database: $($env:DB_NAME) @ localhost:5433"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000