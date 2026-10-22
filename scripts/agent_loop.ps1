# Keep `goat run` alive: start it, and start it again whenever it exits (a crash, or a kill to load new code).
# Output from every run is appended to logs\agent-loop.log / logs\agent-loop.err, so one tail follows them all.
#
#   Start:  Start-Process powershell -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","scripts\agent_loop.ps1" -WindowStyle Hidden
#   Reload: kill the python process running `goat run` (this loop starts the new code within ~5 s)
#   Stop:   New-Item logs\agent-loop.stop, then kill the python process
#
# Never start a second agent by hand while this loop runs: two workers would share one browser.
# The run includes the Simplify scraper (every SCRAPE_INTERVAL_S): new postings in his roles go first in the queue.
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path $PSScriptRoot -Parent)
$err = "logs\agent-loop.err"
Remove-Item "logs\agent-loop.stop" -ErrorAction SilentlyContinue
while ($true) {
    if (Test-Path "logs\agent-loop.stop") { break }
    Add-Content -Path $err -Value ("{0} loop: starting goat run" -f (Get-Date -Format s))
    & cmd /c ".venv\Scripts\python.exe -m goat run >> logs\agent-loop.log 2>> logs\agent-loop.err"
    Add-Content -Path $err -Value ("{0} loop: goat run exited with code {1}" -f (Get-Date -Format s), $LASTEXITCODE)
    Start-Sleep -Seconds 5
}
Add-Content -Path $err -Value ("{0} loop: stopped" -f (Get-Date -Format s))
