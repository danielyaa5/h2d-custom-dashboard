# Logs the H2D's status (chamber, bed, print stage) to status-log.csv in this folder. Read-only: see H2DStatus.cs.
# Fill in config.json first (copy config.example.json). The access code never leaves this folder and is not printed.
#   & .\h2d-log.ps1                 log for 2 hours, one line every 10 s
#   & .\h2d-log.ps1 -Minutes 1      quick connection test
#   -NoPushAll: only listen; don't ask the printer for a full status report
param([int]$Minutes = 120, [int]$EverySeconds = 10, [switch]$NoPushAll)
$ErrorActionPreference = "Stop"
$dir = $PSScriptRoot
$cfgPath = "$dir\config.json"
if (-not (Test-Path $cfgPath)) { throw "config.json not found. Copy config.example.json to config.json and fill in ip, serial and access_code." }
$cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
foreach ($k in "ip", "serial", "access_code") { if (-not $cfg.$k) { throw "config.json: '$k' is empty" } }
Add-Type -Path "$dir\H2DStatus.cs" -ReferencedAssemblies System.Web.Extensions
"Logging $($cfg.ip) for $Minutes min to $dir\status-log.csv (Ctrl+C to stop)"
[H2DStatus]::Log($cfg.ip, $cfg.serial, $cfg.access_code, $dir, $Minutes, $EverySeconds, -not $NoPushAll)
