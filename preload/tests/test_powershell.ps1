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
.PARAMETER ViaFunction
In Preload mode, time the generated command function (`git --version > $null`)
instead of Invoke-Preload.
.PARAMETER Python
Start the Python manager instead of -Manager.
#>
param(
  [string] $Git,
  [string] $Manager = 'preload-manager',
  [string] $Output,
  [ValidateSet('Test', 'Preload', 'Direct')] [string] $Mode = 'Test',
  [ValidateRange(1, 100000)] [int] $Count = 200,
  [ValidateRange(0, 100000)] [int] $Warmup = 20,
  [switch] $DiscardOutput,
  [switch] $ViaFunction,
  [switch] $Python
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
  .PARAMETER InputBytes
  Optional exact stdin bytes, written concurrently and then closed; without
  them stdin is not redirected.
  #>
  param([AllowEmptyCollection()] [AllowEmptyString()] [string[]] $Words,
    [AllowEmptyCollection()] [byte[]] $InputBytes)
  $info = [System.Diagnostics.ProcessStartInfo]::new()
  $info.FileName = $Git
  $info.UseShellExecute = $false
  $info.CreateNoWindow = $true
  $info.WorkingDirectory = (Get-Location).ProviderPath
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  $feedInput = $PSBoundParameters.ContainsKey('InputBytes')
  $info.RedirectStandardInput = $feedInput
  # Decode as the client does; the default is the console code page.
  $info.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
  $info.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
  foreach ($word in $Words) { $info.ArgumentList.Add($word) }
  $process = [System.Diagnostics.Process]::Start($info)
  try {
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if ($feedInput) {
      $source = [System.IO.MemoryStream]::new([byte[]]@($InputBytes), $false)
      $feed = [Preload.Client.PipeConnector]::Feed($source, $process.StandardInput.BaseStream)
    }
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
    Start-Preload -Executable $Git -Manager $Manager -Directory (Join-Path $Output 'session') -Python:$Python
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
        $actual = Invoke-Preload -Capture -Arguments $case.Words
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
      try { $null = Invoke-Preload -Capture -Arguments ([string[]]::new(65536)) }
      catch { $overflow = $_.Exception.Message -like '*65535*' }
      if (-not $overflow) { throw 'Argument overflow was not rejected' }
      $passed += 'overflow-rejected'
      $many = @('rev-parse', '--sq-quote') + (@('') * 65533)
      $actual = Invoke-Preload -Capture -Arguments $many
      if ($actual.ExitCode -ne 0 -or $actual.Stderr -ne '' -or
        $actual.Stdout -cne ((' ' + "''") * 65533 + "`n")) {
        throw '65535-parameter request failed'
      }
      $passed += '65535-parameters'
      $null = Invoke-Preload -Capture -Arguments @('--version')
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
      $killed = Invoke-Preload -Capture -Arguments @('-c', "alias.hold=$hold", 'hold')
      Receive-Job $killer -Wait -AutoRemoveJob
      if ($killed.ExitCode -ne -1 -or $global:LASTEXITCODE -ne -1) {
        throw "Killed worker status: $($killed | ConvertTo-Json -Compress)"
      }
      $result = Invoke-Preload -Capture -Arguments @('--version')
      if ($result.ExitCode -ne 0) { throw 'Pool did not recover after killed workers' }
      $passed += 'killed-worker-status-and-recovery'
      # Forwarded stdin: each case matches a direct run fed identical bytes.
      $utf8 = [System.Text.UTF8Encoding]::new($false)
      $blob = $utf8.GetBytes("batch content`n")
      $oid = (Invoke-DirectCapture -Words @('hash-object', '-w', '--stdin') -InputBytes $blob).Stdout
      $big = $utf8.GetBytes('preload stdin line' + "`n") * 60000
      $stdinCases = @(
        @{ Name = 'stdin-text'; Words = @('hash-object', '--stdin'); Bytes = $utf8.GetBytes("hello`n") },
        @{ Name = 'stdin-empty'; Words = @('hash-object', '--stdin'); Bytes = [byte[]]@() },
        @{ Name = 'stdin-binary-nul'; Words = @('hash-object', '--stdin'); Bytes = [byte[]](0..255) * 4 },
        @{ Name = 'stdin-cat-file-batch'; Words = @('cat-file', '--batch'); Bytes = $utf8.GetBytes($oid) },
        # A shell alias proves child processes inherit the forwarded stdin.
        @{ Name = 'stdin-and-both-outputs-exceed-pipe-buffers'
           Words = @('-c', 'alias.tee2=!tee /dev/stderr', 'tee2'); Bytes = $big },
        @{ Name = 'stdin-not-read'; Words = @('--version'); Bytes = $big }
      )
      foreach ($case in $stdinCases) {
        $expected = Invoke-DirectCapture -Words $case.Words -InputBytes $case.Bytes
        $actual = Invoke-Preload -Capture -Arguments $case.Words -InputBytes $case.Bytes
        if ($actual.Stdout -cne $expected.Stdout -or $actual.Stderr -cne $expected.Stderr -or
          $actual.ExitCode -ne $expected.ExitCode) {
          throw ("Stdin mismatch: $($case.Name): expected " +
            "$($expected | ConvertTo-Json -Compress), actual $($actual | ConvertTo-Json -Compress)")
        }
        $passed += $case.Name
      }
      $inputFile = Join-Path $Output 'stdin-input.bin'
      [System.IO.File]::WriteAllBytes($inputFile, $big)
      $expected = Invoke-DirectCapture -Words @('hash-object', '--stdin') -InputBytes $big
      $actual = Invoke-Preload -Capture -Arguments @('hash-object', '--stdin') -InputPath $inputFile
      if ($actual.Stdout -cne $expected.Stdout) { throw 'Stdin from -InputPath mismatched' }
      $passed += 'stdin-input-path'
      # Without input, the command still reads end-of-file at once.
      $expected = Invoke-DirectCapture -Words @('hash-object', '--stdin') -InputBytes ([byte[]]@())
      $actual = Invoke-Preload -Capture -Arguments @('hash-object', '--stdin')
      if ($actual.Stdout -cne $expected.Stdout) { throw 'Call without input did not read EOF' }
      $passed += 'no-input-reads-eof'
      # The generated function behaves like the executable run directly.
      $function = $script:PreloadSession.Name
      if ((Get-Command $function).CommandType -ne 'Function') { throw 'Command function was not defined' }
      $direct = & $Git version --build-options
      $fromFunction = & $function version --build-options
      if (($fromFunction -join "`n") -cne ($direct -join "`n") -or $fromFunction.Count -ne $direct.Count) {
        throw "Function pipeline output mismatch: $($fromFunction -join '|') vs $($direct -join '|')"
      }
      $passed += 'function-pipeline-lines'
      $direct = 'hello', 'world' | & $Git hash-object --stdin
      $fromFunction = 'hello', 'world' | & $function hash-object --stdin
      if ($fromFunction -cne $direct) { throw "Function stdin mismatch: $fromFunction vs $direct" }
      $passed += 'function-pipeline-stdin'
      $direct = & $Git rev-parse --verify no-such-ref 2>&1 | ForEach-Object { "$_" }
      $directCode = $LASTEXITCODE
      $fromFunction = & $function rev-parse --verify no-such-ref 2>&1 | ForEach-Object { "$_" }
      if ($LASTEXITCODE -ne $directCode -or ($fromFunction -join '|') -cne ($direct -join '|')) {
        throw "Function stderr/status mismatch: $($fromFunction -join '|') ($LASTEXITCODE) vs $($direct -join '|') ($directCode)"
      }
      $passed += 'function-stderr-and-status'
      $fromFunction = & $function config --file $config --get test.value
      if ($fromFunction -cne (& $Git config --file $config --get test.value)) {
        throw 'Function cwd/Unicode argument mismatch'
      }
      $passed += 'function-cwd-and-unicode'
      # No deadlines: a command outlasting the former 15-second limit completes.
      $slow = & $function -c 'alias.slow=!sleep 17; echo done' slow
      if ($slow -cne 'done' -or $LASTEXITCODE -ne 0) { throw "Long-running command failed: $slow" }
      $passed += 'function-no-deadline'
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
    $null = Invoke-Preload -Capture -Arguments @('--version')
    $passed += 'active-resourcing-rejected-and-session-retained'
    Stop-Preload
    if (Test-Path -LiteralPath (Join-Path $Output 'session/ready.json')) {
      throw 'Manager readiness file survived Stop-Preload'
    }
    Stop-Preload
    $passed += 'stop-and-repeated-stop'
    if ((Get-Command (Split-Path -LeafBase $Git) -ErrorAction SilentlyContinue).CommandType -eq 'Function') {
      throw 'Command function survived Stop-Preload'
    }
    $passed += 'function-removed-on-stop'
    . $clientPath
    $rejected = $false
    try {
      Start-Preload -Executable (Join-Path $env:SystemRoot 'System32/whoami.exe') `
        -Manager $Manager -Directory (Join-Path $Output 'rejected-session')
    } catch { $rejected = $_.Exception.Message -like '*support --preload-worker*' }
    if (-not $rejected -or $script:PreloadSession) { throw 'Unsupported executable cleanup failed' }
    $passed += 'unsupported-executable-cleanup'
    Start-Preload -Executable $Git -Manager $Manager -Directory (Join-Path $Output 'restarted-session')
    $result = Invoke-Preload -Capture -Arguments @('--version')
    if ($result.ExitCode -ne 0) { throw 'Session restart failed' }
    $passed += 'restart-after-stop-and-failed-start'
    $report = @{ Mode = $Mode; Passed = $passed; PowerShell = $PSVersionTable.PSVersion.ToString() }
  } else {
    $samples = [System.Collections.Generic.List[double]]::new()
    for ($i = -$Warmup; $i -lt $Count; $i++) {
      # Give the same one-command refill interval to both clients.
      & $Git --version > $null
      $watch = [System.Diagnostics.Stopwatch]::StartNew()
      if ($Mode -eq 'Preload' -and $ViaFunction) {
        # Call the generated function by name, as a user would type `git`.
        & $script:PreloadSession.Name --version > $null
        if ($LASTEXITCODE -ne 0) { throw 'Preloaded function benchmark command failed' }
      } elseif ($Mode -eq 'Preload') {
        $result = Invoke-Preload -Arguments @('--version') -DiscardOutput:$DiscardOutput -Capture:(-not $DiscardOutput)
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
      ViaFunction = [bool]$ViaFunction; PythonManager = [bool]$Python
      SamplesMs = $samples.ToArray(); PowerShell = $PSVersionTable.PSVersion.ToString() }
  }
  $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $Output 'report.json') -Encoding utf8
  Write-Output "PRELOAD_POWERSHELL_PASS $Mode"
} finally { Stop-Preload }
