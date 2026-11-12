#requires -Version 7.0
<#
.SYNOPSIS
Validate or time the Windows PowerShell preload client.
.PARAMETER Git
Native path to the built Git. Its runtime DLLs must be on PATH. Test mode also
needs `sh` on PATH for the shell-alias cases.
.PARAMETER Manager
Native preload-manager executable path.
.PARAMETER Output
New artifact directory, containing a report and session logs.
.PARAMETER Mode
Test performs correctness checks; Preload and Direct collect warmed latency.
.PARAMETER Count
Positive number of measured calls, excluding twenty warm-up calls.
.PARAMETER DiscardOutput
Time the null-sink path instead of capturing and decoding command output.
#>
param(
  [string] $Git,
  [string] $Manager = 'preload-manager',
  [string] $Output,
  [ValidateSet('Test', 'Preload', 'Direct')] [string] $Mode = 'Test',
  [ValidateRange(1, 100000)] [int] $Count = 200,
  [ValidateRange(0, 100000)] [int] $Warmup = 20,
  [switch] $DiscardOutput
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../client/manager.ps1')
$Git = (Resolve-Path -LiteralPath $Git).ProviderPath
$null = New-Item -ItemType Directory -Path $Output -Force
$Output = (Resolve-Path -LiteralPath $Output).ProviderPath
if ($Mode -eq 'Test' -and -not (Get-Command sh -CommandType Application -ErrorAction SilentlyContinue)) {
  # Without sh, Git's shell aliases fail (MinGW builds crash) before any comparison.
  throw 'sh must be on PATH (e.g. C:\msys64\usr\bin) for the shell-alias cases'
}

function Invoke-DirectCapture {
  <#
  .SYNOPSIS
  Capture a native Git invocation concurrently as the correctness oracle.
  .PARAMETER Words
  Exact argument array, excluding the executable; empty strings preserved.
  #>
  param([AllowEmptyCollection()] [AllowEmptyString()] [string[]] $Words)
  $info = [System.Diagnostics.ProcessStartInfo]::new()
  $info.FileName = $Git
  $info.UseShellExecute = $false
  $info.CreateNoWindow = $true
  $info.WorkingDirectory = (Get-Location).ProviderPath
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  # Decode as the client does; the default is the console code page.
  $info.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
  $info.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
  foreach ($word in $Words) { $info.ArgumentList.Add($word) }
  $process = [System.Diagnostics.Process]::Start($info)
  try {
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(15000)) {
      $process.Kill($true)
      throw 'Direct command timed out'
    }
    return [pscustomobject]@{
      Stdout = $stdout.GetAwaiter().GetResult()
      Stderr = $stderr.GetAwaiter().GetResult()
      ExitCode = $process.ExitCode
    }
  } finally { $process.Dispose() }
}

try {
  if ($Mode -ne 'Direct') {
    Start-Preload -Executable $Git -Manager $Manager -Directory (Join-Path $Output 'session')
  }
  if ($Mode -eq 'Test') {
    $fixture = Join-Path $Output 'space ü repo'
    $null = Invoke-DirectCapture @('init', '-q', $fixture)
    $config = Join-Path $fixture 'arguments.config'
    $null = Invoke-DirectCapture @('config', '--file', $config, 'test.value', 'héllo 世界')
    $alias = '!f() { printf "<%s>\n" "$@"; }; f'
    $large = '!f() { i=0; while test "$i" -lt 4000; do printf "stdout-0123456789\n"; printf "stderr-0123456789\n" >&2; i=$((i+1)); done; }; f'
    $cases = @(
      @{ Name = 'version'; Words = @('--version') },
      @{ Name = 'zero-arguments'; Words = @() },
      @{ Name = 'cwd'; Words = @('rev-parse', '--show-toplevel') },
      @{ Name = 'unicode-path-value'; Words = @('config', '--file', $config, '--get', 'test.value') },
      @{ Name = 'arguments'; Words = @('-c', "alias.echo=$alias", 'echo', '', 'a b', 'quote"value', 'héllo 世界', "line`nbreak", 'END') },
      @{ Name = 'nonzero-stderr'; Words = @('not-a-real-subcommand') },
      @{ Name = 'large-both-streams'; Words = @('-c', "alias.large=$large", 'large') }
    )
    $passed = @()
    Push-Location -LiteralPath $fixture
    try {
      foreach ($case in $cases) {
        $expected = Invoke-DirectCapture -Words $case.Words
        $actual = Invoke-Preload -Arguments $case.Words
        if ($actual.Stdout -cne $expected.Stdout -or
          $actual.Stderr -cne $expected.Stderr -or
          $actual.ExitCode -ne $expected.ExitCode -or
          $global:LASTEXITCODE -ne $actual.ExitCode) {
          throw ("Mismatch: $($case.Name): expected " +
            "$($expected | ConvertTo-Json -Compress), actual " +
            "$($actual | ConvertTo-Json -Compress), LASTEXITCODE $global:LASTEXITCODE")
        }
        $passed += $case.Name
        $discarded = Invoke-Preload -Arguments $case.Words -DiscardOutput
        if ($discarded -isnot [int] -or $discarded -ne $expected.ExitCode -or
          $global:LASTEXITCODE -ne $expected.ExitCode) {
          throw "Discard mismatch: $($case.Name)"
        }
        $passed += ('discard-' + $case.Name)
      }
      $overflow = $false
      try { $null = Invoke-Preload -Arguments ([string[]]::new(65536)) }
      catch { $overflow = $_.Exception.Message -like '*65535*' }
      if (-not $overflow) { throw 'Argument overflow was not rejected' }
      $passed += 'overflow-rejected'
      $many = @('rev-parse', '--sq-quote') + (@('') * 65533)
      $actual = Invoke-Preload -Arguments $many
      if ($actual.ExitCode -ne 0 -or $actual.Stderr -ne '' -or
        $actual.Stdout -cne ((' ' + "''") * 65533 + "`n")) {
        throw '65535-parameter request failed'
      }
      $passed += '65535-parameters'
      $null = Invoke-Preload -Arguments @('--version')
      $passed += 'recovery'
      # A worker killed mid-command reports the process exit code a direct
      # call would: Stop-Process terminates with 0xFFFFFFFF, shown as -1.
      # Workers see EOF on stdin, so an alias holds the worker until the
      # release file exists, after releasing its output pipes. Every
      # worker of the manager is killed, so the pool also recovers.
      $release = (Join-Path $Output 'release-hold').Replace('\', '/')
      $hold = "!exec >/dev/null 2>&1 </dev/null; while test ! -e '$release'; do sleep .1; done"
      $killer = Start-ThreadJob -ScriptBlock {
        param($managerId, $workerName, $releasePath)
        Start-Sleep -Seconds 2
        Get-CimInstance Win32_Process -Filter "ParentProcessId = $managerId AND Name = '$workerName'" |
          ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
        $null = New-Item -ItemType File -Path $releasePath
      } -ArgumentList $script:PreloadSession.Process.Id, (Split-Path -Leaf $Git), $release
      $killed = Invoke-Preload -Arguments @('-c', "alias.hold=$hold", 'hold')
      Receive-Job $killer -Wait -AutoRemoveJob
      if ($killed.ExitCode -ne -1 -or $global:LASTEXITCODE -ne -1) {
        throw "Killed worker status: $($killed | ConvertTo-Json -Compress)"
      }
      $result = Invoke-Preload -Arguments @('--version')
      if ($result.ExitCode -ne 0) { throw 'Pool did not recover after killed workers' }
      $passed += 'killed-worker-status-and-recovery'
    } finally { Pop-Location }
    $rejected = $false
    try { Start-Preload -Executable $Git -Manager $Manager -Directory (Join-Path $Output 'duplicate') }
    catch { $rejected = $_.Exception.Message -like '*already active*' }
    if (-not $rejected) { throw 'Duplicate session was not rejected' }
    $passed += 'duplicate-start-rejected'
    $clientPath = (Get-Command Invoke-Preload).ScriptBlock.File
    $rejected = $false
    try { . $clientPath }
    catch { $rejected = $_.Exception.Message -like '*Stop-Preload before sourcing*' }
    if (-not $rejected) { throw 'Active session was lost on re-source' }
    $null = Invoke-Preload -Arguments @('--version')
    $passed += 'active-resourcing-rejected-and-session-retained'
    Stop-Preload
    if (Test-Path -LiteralPath (Join-Path $Output 'session/ready.json')) {
      throw 'Manager readiness file survived Stop-Preload'
    }
    Stop-Preload
    $passed += 'stop-and-repeated-stop'
    . $clientPath
    $rejected = $false
    try {
      Start-Preload -Executable (Join-Path $env:SystemRoot 'System32/whoami.exe') `
        -Manager $Manager -Directory (Join-Path $Output 'rejected-session')
    } catch { $rejected = $_.Exception.Message -like '*support --preload-worker*' }
    if (-not $rejected -or $script:PreloadSession) { throw 'Unsupported executable cleanup failed' }
    $passed += 'unsupported-executable-cleanup'
    Start-Preload -Executable $Git -Manager $Manager -Directory (Join-Path $Output 'restarted-session')
    $result = Invoke-Preload -Arguments @('--version')
    if ($result.ExitCode -ne 0) { throw 'Session restart failed' }
    $passed += 'restart-after-stop-and-failed-start'
    $report = @{ Mode = $Mode; Passed = $passed; PowerShell = $PSVersionTable.PSVersion.ToString() }
  } else {
    $samples = [System.Collections.Generic.List[double]]::new()
    for ($i = -$Warmup; $i -lt $Count; $i++) {
      # Give the same one-command refill interval to both clients.
      & $Git --version > $null
      $watch = [System.Diagnostics.Stopwatch]::StartNew()
      if ($Mode -eq 'Preload') {
        $result = Invoke-Preload -Arguments @('--version') -DiscardOutput:$DiscardOutput
        $code = if ($DiscardOutput) { $result } else { $result.ExitCode }
        if ($code -ne 0) { throw 'Preloaded benchmark command failed' }
      } else {
        & $Git --version > $null
        if ($LASTEXITCODE -ne 0) { throw 'Direct benchmark command failed' }
      }
      $watch.Stop()
      if ($i -ge 0) { $samples.Add($watch.Elapsed.TotalMilliseconds) }
    }
    $report = @{ Mode = $Mode; DiscardOutput = [bool]$DiscardOutput
      SamplesMs = $samples.ToArray(); PowerShell = $PSVersionTable.PSVersion.ToString() }
  }
  $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Output 'report.json') -Encoding utf8
  Write-Output "PRELOAD_POWERSHELL_PASS $Mode"
} finally { Stop-Preload }
