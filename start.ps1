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

# The .env file is authoritative for database settings. pydantic-settings prefers
# environment variables over .env, so drop any stale DB_* overrides first -
# a leftover DB_PASSWORD/DB_PORT in the shell can silently point the app at the
# wrong database (auth failures, wrong port).
Remove-Item Env:MISTRAL_CHAT_MODEL -ErrorAction SilentlyContinue
@("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_DRIVER", "DATABASE_URL") | ForEach-Object {
    Remove-Item "Env:$_" -ErrorAction SilentlyContinue
}
# Belt-and-suspenders: never let a wrong cwd/override change the database name.
$env:DB_NAME = "Knowledge_Gen_Agent"

# Show the real database target (resolved from .env) instead of a hardcoded one.
# Falls back to the application default (5433) when .env omits DB_PORT.
$resolvedPort = "5433"
$envFilePath = Join-Path $projectRoot ".env"
if (Test-Path -LiteralPath $envFilePath) {
    $dbPortLine = Get-Content -LiteralPath $envFilePath | Where-Object { $_ -match "^\s*DB_PORT\s*=" } | Select-Object -First 1
    if ($dbPortLine) {
        $resolvedPort = (($dbPortLine -split "=", 2)[1]).Trim().Trim('"')
    }
}

Write-Host "Starting Knowledge_Gen_Agent backend from $projectRoot"
Write-Host "Database: $($env:DB_NAME) @ localhost:$resolvedPort"
& $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000