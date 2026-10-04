[CmdletBinding()]
param()
$statePath=Join-Path $PSScriptRoot '.local\server-process.json'
if(-not(Test-Path $statePath)){Write-Host 'No managed process.';return}
$state=Get-Content $statePath -Raw|ConvertFrom-Json
$process=Get-Process -Id $state.id -ErrorAction SilentlyContinue
if($process -and [string]$process.StartTime.ToUniversalTime().Ticks -eq $state.start_ticks){
    # Graceful shutdown runs plugin Shutdown hooks and closes MCP/SSH children.
    try {
        Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/control/shutdown' -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
        for($attempt=0; $attempt -lt 150 -and -not $process.HasExited; $attempt++) { Start-Sleep -Milliseconds 200; $process.Refresh() }
    } catch {}
    if(-not $process.HasExited) {
        # Fall back only for this recorded, still-owned process tree.
        & taskkill.exe /PID $process.Id /T /F | Out-Null
    }
    Write-Host 'MyClaude stopped.'
}
Remove-Item -LiteralPath $statePath
