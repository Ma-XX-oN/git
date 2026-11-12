#requires -Version 7.0
<#
.SYNOPSIS
Check serial PowerShell calls, exact version output and client handle counts.
.PARAMETER Git
Native path to the built worker executable; its runtime DLLs must be on PATH.
.PARAMETER Manager
Native preload-manager executable path.
.PARAMETER Output
New private artifact directory; retained with the summary and manager log.
.PARAMETER Count
Positive number of captured calls after fifty warmup calls; default 6000.
#>
param(
  [string] $Git,
  [string] $Manager = 'preload-manager',
  [string] $Output,
  [ValidateRange(1, 100000)] [int] $Count = 6000
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../client/manager.ps1')
$Git = (Resolve-Path -LiteralPath $Git).ProviderPath
$null = New-Item -ItemType Directory -Path $Output
$Output = (Resolve-Path -LiteralPath $Output).ProviderPath
$expected = (& $Git --version) -join "`n"
if ($LASTEXITCODE -ne 0) { throw 'Direct version check failed' }
$process = [System.Diagnostics.Process]::GetCurrentProcess()
try {
  Start-Preload -Executable $Git -Manager $Manager -Directory (Join-Path $Output 'session')
  for ($i = -50; $i -lt $Count; $i++) {
    if ($i -eq 0) {
      $process.Refresh()
      $handlesBefore = $process.HandleCount
    }
    $result = Invoke-Preload -Arguments @('--version')
    if ($result.ExitCode -ne 0 -or $result.Stderr -ne '' -or
      $result.Stdout -cne ($expected + "`n")) {
      throw "Incorrect output/status at call $i"
    }
  }
  $process.Refresh()
  $handlesAfter = $process.HandleCount
  @{ Count = $Count; HandlesBefore = $handlesBefore; HandlesAfter = $handlesAfter
     PowerShell = $PSVersionTable.PSVersion.ToString() } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Output 'summary.json') -Encoding utf8
  Write-Output "PRELOAD_POWERSHELL_STRESS_PASS $Count handles=$handlesBefore->$handlesAfter"
} finally {
  Stop-Preload
  $process.Dispose()
}
