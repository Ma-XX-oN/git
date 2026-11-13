#requires -Version 7.0
<#
.SYNOPSIS
Windows PowerShell frontend for the native preload manager.
.DESCRIPTION
Dot-source this file, Start-Preload with executable and manager paths, then call
Invoke-Preload with a string array. Returns captured UTF-8 Stdout, Stderr and
ExitCode; this API does not install command overrides. Stdin is forwarded only
with -InputBytes or -InputPath; otherwise the command reads end-of-file.
By default output is drained concurrently but buffered until completion; use
-DiscardOutput to drain to a null sink and return only status. Call Stop-Preload
in finally. Workers inherit the environment at manager startup, as in Bash.
#>

if (Get-Variable -Name PreloadSession -Scope Script -ErrorAction SilentlyContinue) {
  if ($script:PreloadSession) { throw 'Stop-Preload before sourcing the client again' }
} else {
  $script:PreloadSession = $null
}
$script:PreloadEncoding = [System.Text.UTF8Encoding]::new($false, $true)

if (-not ('Preload.Client.PipeConnector' -as [type])) {
  # One shared typed connector avoids the PowerShell function output pipeline.
  # Add-Type uses PowerShell's bundled compiler; compilation is per process.
  Add-Type -Path (Join-Path $PSScriptRoot '../src/client/pipe.cs')
}

function Read-PreloadBytes {
  <#
  .SYNOPSIS
  Read exactly Count bytes from a borrowed pipe with a ten-second deadline.
  .PARAMETER Pipe
  Connected pipe, retained by the caller.
  .PARAMETER Count
  Positive byte count; throws on timeout or premature EOF.
  #>
  param($Pipe, [int] $Count)
  $buffer = [byte[]]::new($Count)
  $offset = 0
  while ($offset -lt $Count) {
    $read = $Pipe.ReadAsync($buffer, $offset, $Count - $offset)
    if (-not $read.Wait(10000)) { throw 'Preload reply timed out' }
    if ($read.Result -eq 0) { throw 'Preload reply ended prematurely' }
    $offset += $read.Result
  }
  return ,$buffer
}

function Send-PreloadControl {
  <#
  .SYNOPSIS
  Exchange a control line with the active manager; return its UTF-8 reply.
  .PARAMETER Line
  PRELOAD or STOP request without its terminating newline.
  #>
  param([string] $Line)
  $pipe = [Preload.Client.PipeConnector]::Open($script:PreloadSession.PipeName + '\request')
  try {
    $bytes = $script:PreloadEncoding.GetBytes($Line + "`n")
    $pipe.Write($bytes, 0, $bytes.Length)
    $reader = [System.IO.StreamReader]::new(
      $pipe, $script:PreloadEncoding, $false, 1024, $true)
    try {
      $read = $reader.ReadLineAsync()
      if (-not $read.Wait(15000)) { throw 'Manager control reply timed out' }
      return $read.Result
    } finally { $reader.Dispose() }
  } finally { $pipe.Dispose() }
}

function Send-PreloadStatus {
  <#
  .SYNOPSIS
  End a call whose worker died and return the manager's STATUS reply line.
  .PARAMETER Request
  GET connection held for the call; carries a bare STATUS and is disposed here,
  which ends the call.
  #>
  param($Request)
  try {
    $bytes = $script:PreloadEncoding.GetBytes("STATUS`n")
    $Request.Write($bytes, 0, $bytes.Length)
    $reader = [System.IO.StreamReader]::new(
      $Request, $script:PreloadEncoding, $false, 1024, $true)
    try {
      $read = $reader.ReadLineAsync()
      if (-not $read.Wait(15000)) { throw 'Manager status reply timed out' }
      return $read.Result
    } finally { $reader.Dispose() }
  } finally { $Request.Dispose() }
}

function Start-Preload {
  <#
  .SYNOPSIS
  Start one owned manager and register one worker executable; returns no output.
  .PARAMETER Executable
  Native path to an executable supporting --preload-worker.
  .PARAMETER Manager
  Native C++ manager executable path or command name; defaults to
  preload-manager on PATH. Ignored with -Python.
  .PARAMETER Directory
  New private session directory; retained with manager logs after shutdown.
  .PARAMETER Python
  Run the Python manager (preload/manager/manager_windows.py) instead, with
  $env:PRELOAD_PYTHON or python on PATH.
  #>
  param(
    [Parameter(Mandatory)] [string] $Executable,
    [string] $Manager = 'preload-manager',
    [Parameter(Mandatory)] [string] $Directory,
    [switch] $Python
  )
  if (-not $IsWindows) { throw 'The PowerShell preload client requires Windows' }
  if ($script:PreloadSession) { throw 'A preload session is already active' }
  $exe = (Resolve-Path -LiteralPath $Executable).ProviderPath
  $name = [System.IO.Path]::GetFileNameWithoutExtension($exe)
  if ($name -notmatch '^[A-Za-z_.][A-Za-z0-9_.-]*$') { throw 'Invalid command name' }
  $directoryInfo = New-Item -ItemType Directory -Path $Directory -ErrorAction Stop
  $pipeName = 'preload-ps-' + [guid]::NewGuid().ToString('N')
  $info = [System.Diagnostics.ProcessStartInfo]::new()
  if ($Python) {
    $interpreter = if ($env:PRELOAD_PYTHON) { $env:PRELOAD_PYTHON } else { 'python' }
    $info.FileName = (Get-Command $interpreter -CommandType Application -ErrorAction Stop |
      Select-Object -First 1).Source
    $info.ArgumentList.Add((Join-Path $PSScriptRoot '../manager/manager_windows.py'))
  } else {
    $info.FileName = (Get-Command $Manager -CommandType Application -ErrorAction Stop).Source
  }
  $info.UseShellExecute = $false
  $info.CreateNoWindow = $true
  $info.RedirectStandardOutput = $true
  $info.RedirectStandardError = $true
  foreach ($word in @('--owner-pid', "$PID", '--directory', $directoryInfo.FullName,
      '--pipe-dir', ('\\.\pipe\' + $pipeName))) {
    $info.ArgumentList.Add($word)
  }
  $process = [System.Diagnostics.Process]::Start($info)
  $script:PreloadSession = @{
    Process = $process; PipeName = $pipeName; Name = $name
    Directory = $directoryInfo.FullName
    Log = $process.StandardOutput.ReadToEndAsync()
    Errors = $process.StandardError.ReadToEndAsync()
  }
  try {
    $watch = [System.Diagnostics.Stopwatch]::StartNew()
    while (-not (Test-Path -LiteralPath (Join-Path $Directory 'ready.json'))) {
      if ($process.HasExited) { throw 'Preload manager exited during startup' }
      if ($watch.Elapsed.TotalSeconds -gt 15) { throw 'Manager startup timed out' }
      Start-Sleep -Milliseconds 10
    }
    $reply = Send-PreloadControl "PRELOAD $name $exe"
    if ($reply -ne 'OK') { throw $reply }
  } catch {
    Stop-Preload
    throw
  }
}

function Invoke-Preload {
  <#
  .SYNOPSIS
  Run one command, returning captured Stdout, Stderr and ExitCode; sets LASTEXITCODE.
  .DESCRIPTION
  If the worker dies mid-command, ExitCode is its process exit code as a
  direct call would report it, e.g. -1073741819 for an access violation.
  .PARAMETER Arguments
  Zero to 65535 parameters excluding argv[0]; empty strings are preserved.
  NULs and null entries are rejected before GET. Cwd must be a filesystem path.
  .PARAMETER DiscardOutput
  Drain both output pipes directly to Stream.Null, skipping capture, copying
  into memory and UTF-8 decoding. Return only the integer exit status.
  .PARAMETER InputBytes
  Bytes for the command's stdin, sent exactly (NUL bytes included); an empty
  array gives immediate end-of-file. Without -InputBytes or -InputPath stdin is
  closed at once, so the command reads end-of-file.
  .PARAMETER InputPath
  File whose contents are streamed to the command's stdin.
  #>
  param(
    [AllowEmptyCollection()] [AllowEmptyString()] [string[]] $Arguments = @(),
    [switch] $DiscardOutput,
    [AllowEmptyCollection()] [byte[]] $InputBytes,
    [string] $InputPath
  )
  if (-not $script:PreloadSession) { throw 'No active preload session' }
  if ($Arguments.Count -gt 65535) { throw 'At most 65535 parameters are supported' }
  foreach ($word in $Arguments) {
    if ($null -eq $word -or $word.Contains([char]0)) { throw 'Invalid NUL/null argument' }
  }
  if ($PSBoundParameters.ContainsKey('InputBytes') -and $InputPath) {
    throw 'Supply either -InputBytes or -InputPath, not both'
  }
  # Opened before GET, so a missing file fails without allocating a worker.
  $source = if ($InputPath) {
    [System.IO.File]::OpenRead((Resolve-Path -LiteralPath $InputPath).ProviderPath)
  } elseif ($PSBoundParameters.ContainsKey('InputBytes')) {
    [System.IO.MemoryStream]::new([byte[]]@($InputBytes), $false)
  }
  $cwd = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath('.')
  $name = $script:PreloadSession.Name
  $fields = @($name) + $Arguments + @('cwd', ($cwd + "`n"), 'end', '')
  $payload = $script:PreloadEncoding.GetBytes([string]::Join([char]0, $fields))
  $request = $null
  $channels = @()
  $feed = $null
  $output = if ($DiscardOutput) { [System.IO.Stream]::Null } else { [System.IO.MemoryStream]::new() }
  $errors = if ($DiscardOutput) { [System.IO.Stream]::Null } else { [System.IO.MemoryStream]::new() }
  try {
    $request = [Preload.Client.PipeConnector]::Open($script:PreloadSession.PipeName + '\request')
    $line = $script:PreloadEncoding.GetBytes("GET $name 0`n")
    $request.Write($line, 0, $line.Length)
    $count = [System.BitConverter]::GetBytes([int]$Arguments.Count)
    $request.Write($count, 0, 4)
    $request.Write($payload, 0, $payload.Length)
    $frame = $script:PreloadEncoding.GetString((Read-PreloadBytes $request 8))
    if ($frame -notmatch '^[0-9a-f]{8}$' -or $frame -eq '00000000') {
      throw "Invalid/failed worker allocation: $frame"
    }
    $workerId = [Convert]::ToInt32($frame, 16)
    # $request stays open for the call: disposing it ends the call, so the
    # manager forgets the worker (about 0.15 ms faster per call than a
    # separate end-of-call request on a new connection).
    # The manager has already connected params-cwd; open the rest in the
    # worker's order.
    foreach ($channel in @('stdin', 'stdout', 'stderr', 'exit-code')) {
      $pipe = [Preload.Client.PipeConnector]::Open($script:PreloadSession.PipeName + "\worker-$workerId\$channel")
      $channels += $pipe
      $pipe.WriteByte(48)
    }
    # The worker writes one ACK byte (0x06) on stdin once it has read every
    # marker; reading it after opening all channels never waits on the opens.
    # Only then may stdin be written or closed without losing a marker. Wait
    # without a deadline, like Bash: the read ends with the ACK or with EOF.
    # EOF means the worker closed its end of the pipe, as its failure paths do
    # before exiting; the status below then reports what happened.
    $ackBuffer = [byte[]]::new(1)
    $null = $channels[0].Read($ackBuffer, 0, 1)
    if ($source) {
      # Input is written while output drains; Feed closes stdin at the end,
      # which the command reads as end-of-file.
      $feed = [Preload.Client.PipeConnector]::Feed($source, $channels[0])
      $source = $null
    } else {
      $channels[0].Dispose()  # No input: the command reads end-of-file.
    }
    $stdout = $channels[1].CopyToAsync($output)
    $stderr = $channels[2].CopyToAsync($errors)
    if (-not [System.Threading.Tasks.Task]::WaitAll(
        [System.Threading.Tasks.Task[]]@($stdout, $stderr), 15000)) {
      throw 'Worker output timed out'
    }
    if ($feed) {
      # Output EOF means the command has finished; the copy ends with it.
      try { $feed.Wait() } catch [System.AggregateException] { }
    }
    $reader = [System.IO.StreamReader]::new($channels[3], $script:PreloadEncoding)
    try {
      $read = $reader.ReadLineAsync()
      if (-not $read.Wait(10000)) { throw 'Worker status timed out' }
      $status = $read.Result
    } finally { $reader.Dispose() }
    if ($status -match '^EXIT ([0-9]+)$') {
      $global:LASTEXITCODE = [int]$Matches[1]
    } elseif ([string]::IsNullOrEmpty($status)) {
      # The worker died mid-command (crashed or killed) without an EXIT
      # record; report its process exit code as a direct call would.
      $lost = Send-PreloadStatus $request
      $request = $null
      if ($lost -notmatch '^EXIT ([0-9]+)$') { throw "No exit status for worker ${workerId}: $lost" }
      # Windows exit codes are unsigned; $LASTEXITCODE shows them as Int32.
      $global:LASTEXITCODE = [System.BitConverter]::ToInt32(
        [System.BitConverter]::GetBytes([uint32]$Matches[1]), 0)
    } else {
      throw "Invalid worker status: $status"
    }
    if ($DiscardOutput) { return $global:LASTEXITCODE }
    return [pscustomobject]@{
      Stdout = $script:PreloadEncoding.GetString($output.ToArray())
      Stderr = $script:PreloadEncoding.GetString($errors.ToArray())
      ExitCode = $global:LASTEXITCODE
    }
  } finally {
    if ($request) { $request.Dispose() }  # Ends the call.
    foreach ($pipe in $channels) { $pipe.Dispose() }
    # Disposing the stdin channel ends a still-running copy; Feed absorbs that.
    if ($feed) { try { $feed.Wait() } catch [System.AggregateException] { } }
    if ($source) { $source.Dispose() }
    $output.Dispose()
    $errors.Dispose()
  }
}

function Stop-Preload {
  <#
  .SYNOPSIS
  Stop the active session, preserve logs and release the process; no output.
  .DESCRIPTION
  An inactive session is explicitly a no-op, allowing use in finally blocks.
  #>
  if (-not $script:PreloadSession) { return }
  $session = $script:PreloadSession
  try {
    if (-not $session.Process.HasExited) {
      $null = Send-PreloadControl 'STOP'
      if (-not $session.Process.WaitForExit(5000)) { $session.Process.Kill($true) }
    }
  } finally {
    if (-not $session.Process.HasExited) { $session.Process.Kill($true) }
    $session.Process.WaitForExit()
    [System.IO.File]::WriteAllText((Join-Path $session.Directory 'manager.log'),
      $session.Log.GetAwaiter().GetResult() + $session.Errors.GetAwaiter().GetResult())
    $session.Process.Dispose()
    $script:PreloadSession = $null
  }
}
