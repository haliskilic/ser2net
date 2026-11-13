$ErrorActionPreference = 'Stop'
$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# Before an upgrade or uninstall: Windows keeps the file of a running program, so a
# ser2netweb started from this package (a console window) would keep Chocolatey from
# replacing or removing the package's folder. It is stopped. The Windows service runs its
# own copy (C:\Program Files\ser2netweb) and is left alone.
$exe = Join-Path $toolsDir 'ser2netweb.exe'
$running = @(Get-Process -Name 'ser2netweb' -ErrorAction SilentlyContinue |
  Where-Object { $_.Path -eq $exe })
if ($running.Count -gt 0) {
  Write-Warning "Stopping the ser2netweb started from $exe (process $($running.Id -join ', '))."
  $running | Stop-Process -Force
  $running | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
}
