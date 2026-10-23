# Start scripts\agent_loop.ps1 unless it is already running or was stopped on purpose (logs\agent-loop.stop).
# The scheduled task 'goat agent loop' runs this at logon and every 5 minutes, so goat comes back after a reboot or
# sign-out, and after the Claude session that started the loop is closed: its processes go with it, and the loop died
# that way twice on 2026-10-22 (14:00 and 18:41, no exit line). Started from Task Scheduler, the loop belongs to no session.
#
#   Install: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ensure_agent_loop.ps1 -Install
#   Run now: Start-ScheduledTask -TaskName 'goat agent loop'
#   Remove:  Unregister-ScheduledTask -TaskName 'goat agent loop' -Confirm:$false
#   Pause:   New-Item logs\agent-loop.stop (the loop stops and this script leaves it stopped until the file is gone)
param([switch]$Install)
$root = Split-Path $PSScriptRoot -Parent

if ($Install) {
    # conhost --headless keeps the 5-minute check from flashing a console window
    $action = New-ScheduledTaskAction -Execute "conhost.exe" -WorkingDirectory $root `
        -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
        (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5))
    )
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName 'goat agent loop' -Action $action -Trigger $triggers -Settings $settings -Force `
        -Description "Keeps goat's agent loop (scripts\agent_loop.ps1 in $root) running; see the script's header." | Out-Null
    "installed 'goat agent loop'"
    return
}

if (Test-Path (Join-Path $root "logs\agent-loop.stop")) { return }
# '\agent_loop.ps1' only: this script's own command line ends in 'ensure_agent_loop.ps1'
$running = Get-CimInstance Win32_Process -Filter "name='powershell.exe'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match '[\\/]agent_loop\.ps1' }
if ($running) { return }
Start-Process powershell.exe -WorkingDirectory $root -WindowStyle Hidden `
    -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $root "scripts\agent_loop.ps1")
Add-Content -Path (Join-Path $root "logs\agent-loop.err") `
    -Value ("{0} watchdog: the agent loop was not running; started it" -f (Get-Date -Format s))
