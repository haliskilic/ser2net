$ErrorActionPreference = 'Stop'
$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

# The release's Windows executable and its SHA-256, from the release's SHA256SUMS
# (python tools/windows_channels.py chocolatey vX.Y.Z sets both lines). x64 only.
$url64 = 'https://github.com/haliskilic/ser2netweb/releases/download/v3.0.0/ser2netweb-windows-x64.exe'
$checksum64 = '079f7943adc1d446026fa0d42dd9ba5b95ff91fa010e571923f4d0237d48b632'

# Into the package's tools folder, where Chocolatey's automatic shim puts it on PATH as
# ser2netweb. ser2netweb knows this folder (lib\ser2netweb\tools, next to ser2netweb.nupkg)
# and keeps its data out of it: in %LOCALAPPDATA%\ser2netweb of the account that runs it.
$packageArgs = @{
  packageName    = $env:ChocolateyPackageName
  fileFullPath   = Join-Path $toolsDir 'ser2netweb.exe'
  url64bit       = $url64
  checksum64     = $checksum64
  checksumType64 = 'sha256'
}
Get-ChocolateyWebFile @packageArgs | Out-Null

Write-Host "Run 'ser2netweb' to start it (the first run asks for the web UI's address); its data is in %LOCALAPPDATA%\ser2netweb."
Write-Host "As a Windows service: 'ser2netweb --install-service' from an elevated prompt (https://github.com/haliskilic/ser2netweb/blob/main/docs/WINDOWS.md)."
if (Get-Service -Name 'ser2netweb' -ErrorAction SilentlyContinue) {
  Write-Warning "The ser2netweb service runs its own copy of the program: run 'ser2netweb --install-service' from an elevated prompt to upgrade it to this version."
}
