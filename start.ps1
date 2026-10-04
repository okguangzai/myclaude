[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Run .\install.ps1 first.' }
$statePath = Join-Path $PSScriptRoot '.local\server-process.json'
if (Test-Path $statePath) {
    $state = Get-Content $statePath -Raw | ConvertFrom-Json
    $process = Get-Process -Id $state.id -ErrorAction SilentlyContinue
    if ($process -and [string]$process.StartTime.ToUniversalTime().Ticks -eq $state.start_ticks) {
        Write-Host 'MyClaude is running: http://127.0.0.1:8000'; return
    }
}
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { throw 'Port 8000 is occupied by another process.' }
$local = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Force $local | Out-Null
$process = Start-Process $python -ArgumentList @('backend/run_server.py') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $local 'server.stdout.log') -RedirectStandardError (Join-Path $local 'server.stderr.log')
@{id=$process.Id;start_ticks=[string]$process.StartTime.ToUniversalTime().Ticks} | ConvertTo-Json | Set-Content $statePath -Encoding UTF8
for ($i=0;$i -lt 50;$i++) {
    if ($process.HasExited) { throw 'Startup failed. See .local\server.stderr.log.' }
    try { $result=Invoke-RestMethod http://127.0.0.1:8000/api/health -TimeoutSec 2; if($result.service -eq 'myclaude'){Write-Host 'MyClaude ready: http://127.0.0.1:8000';return} } catch { Start-Sleep -Milliseconds 300 }
}
throw 'Startup timed out. See .local\server.stderr.log.'
