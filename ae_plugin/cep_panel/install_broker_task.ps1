# install_broker_task.ps1 - register the CorridorKey CUDA broker as a
# per-user logon Scheduled Task so it starts OUTSIDE After Effects' process
# tree (parent = Task Scheduler / svchost), in the user's interactive session
# with a clean token. That is the whole point: a broker spawned by the CEP
# panel would inherit CEF's sticky mitigations and crash CUDA (0xC0000005);
# one started by Task Scheduler does not.
#
# Run once (per user). Re-running is safe (-Force replaces). Also starts the
# broker immediately so you do not have to log off/on to use it now.
#
# Usage (from a normal PowerShell, NOT from inside AE):
#   powershell -ExecutionPolicy Bypass -File install_broker_task.ps1
#   powershell -ExecutionPolicy Bypass -File install_broker_task.ps1 -Uninstall

param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$TaskName = 'CorridorKey\CUDABroker'

$here   = $PSScriptRoot
$root   = (Resolve-Path (Join-Path $here '..\..')).Path
# pythonw = no console window for the long-lived server. Falls back to python.exe.
$py     = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $py)) { $py = Join-Path $root '.venv\Scripts\python.exe' }
$broker = Join-Path $here 'ck_broker.py'

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "CorridorKey broker task removed."
    return
}

if (-not (Test-Path $py))     { throw "venv python not found: $py" }
if (-not (Test-Path $broker)) { throw "broker not found: $broker" }

$action = New-ScheduledTaskAction -Execute $py -Argument "`"$broker`"" -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# Interactive logon -> user's session -> WDDM/CUDA works. Limited run level
# (no admin needed; the broker only spawns the engine).
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "Registered task: $TaskName"
Write-Host "  exec : $py"
Write-Host "  arg  : $broker"

# Start it now so the broker is live without a logoff/logon.
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ("Started. LastTaskResult={0} (0 or 267009=running)" -f $info.LastTaskResult)
Write-Host "Broker log: $env:TEMP\corridorkey_broker.log"
