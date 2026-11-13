#requires -Version 7.0
<#
.SYNOPSIS
Windows PowerShell frontend for the native preload manager.
.DESCRIPTION
Dot-source this file and Start-Preload with the executable path. Start-Preload
also defines a function named after the executable (git.exe -> git) that runs
it through the preload worker like a native command: arguments, the current
directory and pipeline input are forwarded, output streams to the pipeline and
error stream, and $LASTEXITCODE is set; `git.exe` still runs the executable.
Invoke-Preload is the programmatic API: it returns captured UTF-8 Stdout,
Stderr and ExitCode, buffered until completion (-DiscardOutput drains to a
null sink and returns only status). Its stdin is forwarded only with
-InputBytes or -InputPath; otherwise the command reads end-of-file. Call
Stop-Preload in finally; it also removes the function. Workers inherit the
environment at manager startup, as in Bash.
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
  Read exactly Count bytes from a borrowed pipe, waiting without a deadline.
  .PARAMETER Pipe
  Connected pipe, retained by the caller.
  .PARAMETER Count
  Positive byte count; throws on premature EOF.
  #>
  param($Pipe, [int] $Count)
  $buffer = [byte[]]::new($Count)
  $offset = 0
  while ($offset -lt $Count) {
    $read = $Pipe.ReadAsync($buffer, $offset, $Count - $offset)
    $read.Wait()
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
      # The manager itself bounds how long it waits for the dead worker.
      $read = $reader.ReadLineAsync()
      $read.Wait()
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
    # Define a function named after the executable, as the Bash client does.
    # It forwards its arguments, the current directory and any pipeline input;
    # without a session it runs the executable. `name.exe` still runs it directly.
    $quoted = $exe.Replace("'", "''")
    $body = @"
`$items = @(`$input)
if (`$script:PreloadSession -and `$script:PreloadSession.Name -eq '$name') {
  if (`$items.Count) { Invoke-Preload -Arguments `$args -InputLines `$items }
  else { Invoke-Preload -Arguments `$args }
} elseif (`$items.Count) { `$items | & '$quoted' @args }
else { & '$quoted' @args }
"@
    Set-Item -Path "function:script:$name" -Value ([scriptblock]::Create($body))
    $script:PreloadSession.Function = $name
  } catch {
    Stop-Preload
    throw
  }
}

function Open-PreloadCall {
  <#
  .SYNOPSIS
  Allocate a worker, send the request and connect its channels; returns the call.
  .DESCRIPTION
  Returns a hashtable with Request (the held GET connection), Channels (stdin,
  stdout, stderr, exit-code), Feed (the stdin copy task, or $null) and
  WorkerId. Stdin is already fed or closed. The caller drains Channels[1] and
  Channels[2], then calls Complete-PreloadCall, and disposes via Close-PreloadCall.
  .PARAMETER Arguments
  Validated parameters excluding argv[0].
  .PARAMETER Source
  Owned input stream for stdin, or $null for immediate end-of-file.
  #>
  param([AllowEmptyCollection()] [AllowEmptyString()] [string[]] $Arguments, $Source)
  $cwd = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath('.')
  $name = $script:PreloadSession.Name
  $fields = @($name) + $Arguments + @('cwd', ($cwd + "`n"), 'end', '')
  $payload = $script:PreloadEncoding.GetBytes([string]::Join([char]0, $fields))
  $call = @{ Request = $null; Channels = @(); Feed = $null; WorkerId = 0; Source = $Source }
  try {
    $call.Request = [Preload.Client.PipeConnector]::Open($script:PreloadSession.PipeName + '\request')
    $line = $script:PreloadEncoding.GetBytes("GET $name 0`n")
    $call.Request.Write($line, 0, $line.Length)
    $count = [System.BitConverter]::GetBytes([int]$Arguments.Count)
    $call.Request.Write($count, 0, 4)
    $call.Request.Write($payload, 0, $payload.Length)
    $frame = $script:PreloadEncoding.GetString((Read-PreloadBytes $call.Request 8))
    if ($frame -notmatch '^[0-9a-f]{8}$' -or $frame -eq '00000000') {
      throw "Invalid/failed worker allocation: $frame"
    }
    $call.WorkerId = [Convert]::ToInt32($frame, 16)
    # Request stays open for the call: disposing it ends the call, so the
    # manager forgets the worker (about 0.15 ms faster per call than a
    # separate end-of-call request on a new connection).
    # The manager has already connected params-cwd; open the rest in the
    # worker's order.
    foreach ($channel in @('stdin', 'stdout', 'stderr', 'exit-code')) {
      $pipe = [Preload.Client.PipeConnector]::Open(
        $script:PreloadSession.PipeName + "\worker-$($call.WorkerId)\$channel")
      $call.Channels += $pipe
      $pipe.WriteByte(48)
    }
    # The worker writes one ACK byte (0x06) on stdin once it has read every
    # marker; reading it after opening all channels never waits on the opens.
    # Only then may stdin be written or closed without losing a marker. Wait
    # without a deadline, like Bash: the read ends with the ACK, or with EOF
    # only if the worker process has ended; the status then reports it.
    $ackBuffer = [byte[]]::new(1)
    $null = $call.Channels[0].Read($ackBuffer, 0, 1)
    if ($Source) {
      # Input is written while output drains; Feed closes stdin at the end,
      # which the command reads as end-of-file.
      $call.Feed = [Preload.Client.PipeConnector]::Feed($Source, $call.Channels[0])
      $call.Source = $null
    } else {
      $call.Channels[0].Dispose()  # No input: the command reads end-of-file.
    }
    return $call
  } catch {
    Close-PreloadCall $call
    throw
  }
}

function Complete-PreloadCall {
  <#
  .SYNOPSIS
  After output EOF, read the exit status, set LASTEXITCODE and return it.
  .DESCRIPTION
  If the worker died mid-command, the status is its process exit code as a
  direct call would report it, e.g. -1073741819 for an access violation.
  .PARAMETER Call
  Open call from Open-PreloadCall whose output channels reached EOF.
  .PARAMETER TimeoutMs
  Status-read deadline in milliseconds, or -1 to wait without one.
  #>
  param($Call, [int] $TimeoutMs = -1)
  if ($Call.Feed) {
    # Output EOF means the command has finished; the copy ends with it.
    try { $Call.Feed.Wait() } catch [System.AggregateException] { }
  }
  $reader = [System.IO.StreamReader]::new($Call.Channels[3], $script:PreloadEncoding)
  try {
    $read = $reader.ReadLineAsync()
    if (-not $read.Wait($TimeoutMs)) { throw 'Worker status timed out' }
    $status = $read.Result
  } finally { $reader.Dispose() }
  if ($status -match '^EXIT ([0-9]+)$') {
    $global:LASTEXITCODE = [int]$Matches[1]
  } elseif ([string]::IsNullOrEmpty($status)) {
    # The worker died mid-command (crashed or killed) without an EXIT
    # record; report its process exit code as a direct call would.
    $lost = Send-PreloadStatus $Call.Request
    $Call.Request = $null
    if ($lost -notmatch '^EXIT ([0-9]+)$') {
      throw "No exit status for worker $($Call.WorkerId): $lost"
    }
    # Windows exit codes are unsigned; $LASTEXITCODE shows them as Int32.
    $global:LASTEXITCODE = [System.BitConverter]::ToInt32(
      [System.BitConverter]::GetBytes([uint32]$Matches[1]), 0)
  } else {
    throw "Invalid worker status: $status"
  }
  return $global:LASTEXITCODE
}

function Close-PreloadCall {
  <#
  .SYNOPSIS
  Release a call's connections; disposing the held GET connection ends the call.
  .PARAMETER Call
  Call state from Open-PreloadCall, possibly partial; safe to close twice.
  #>
  param($Call)
  if ($Call.Request) { $Call.Request.Dispose(); $Call.Request = $null }
  foreach ($pipe in $Call.Channels) { $pipe.Dispose() }
  # Disposing the stdin channel ends a still-running copy; Feed absorbs that.
  if ($Call.Feed) { try { $Call.Feed.Wait() } catch [System.AggregateException] { } }
  if ($Call.Source) { $Call.Source.Dispose(); $Call.Source = $null }
}

function Invoke-Preload {
  <#
  .SYNOPSIS
  Run one command through a preloaded worker, like the native command.
  .DESCRIPTION
  By default output streams: stdout lines go to the pipeline and stderr lines to
  the error stream as each line arrives, decoded with [Console]::OutputEncoding
  as for native commands, and $LASTEXITCODE is set. Stderr never stops a script,
  even with $ErrorActionPreference = 'Stop'. There are no deadlines, so
  long-running commands are unaffected. The function Start-Preload defines for
  the executable calls this. If the worker dies mid-command, the exit code is
  its process exit code as a direct call would report it, e.g. -1073741819 for
  an access violation.
  .PARAMETER Arguments
  Zero to 65535 parameters excluding argv[0]; empty strings are preserved.
  NULs and null entries are rejected before GET. Cwd must be a filesystem path.
  .PARAMETER Capture
  Return an object with Stdout and Stderr (UTF-8, captured in full) and ExitCode
  instead of streaming.
  .PARAMETER DiscardOutput
  Drain both output pipes directly to Stream.Null, skipping capture, copying
  into memory and decoding. Return only the integer exit status.
  .PARAMETER InputBytes
  Bytes for the command's stdin, sent exactly (NUL bytes included); an empty
  array gives immediate end-of-file.
  .PARAMETER InputPath
  File whose contents are streamed to the command's stdin.
  .PARAMETER InputLines
  Pipeline input, sent to stdin as lines encoded with $OutputEncoding, as
  PowerShell does for native commands. Without any input option stdin is closed
  at once, so the command reads end-of-file.
  #>
  [CmdletBinding()]
  param(
    [AllowEmptyCollection()] [AllowEmptyString()] [string[]] $Arguments = @(),
    [switch] $Capture,
    [switch] $DiscardOutput,
    [AllowEmptyCollection()] [byte[]] $InputBytes,
    [string] $InputPath,
    [object[]] $InputLines
  )
  if (-not $script:PreloadSession) { throw 'No active preload session' }
  if ($Arguments.Count -gt 65535) { throw 'At most 65535 parameters are supported' }
  foreach ($word in $Arguments) {
    if ($null -eq $word -or $word.Contains([char]0)) { throw 'Invalid NUL/null argument' }
  }
  if (@($PSBoundParameters.ContainsKey('InputBytes'), [bool]$InputPath,
      $PSBoundParameters.ContainsKey('InputLines')).Where({ $_ }).Count -gt 1) {
    throw 'Supply only one of -InputBytes, -InputPath and -InputLines'
  }
  if ($Capture -and $DiscardOutput) { throw 'Supply either -Capture or -DiscardOutput' }
  # Opened before GET, so a missing file fails without allocating a worker.
  $source = if ($InputPath) {
    [System.IO.File]::OpenRead((Resolve-Path -LiteralPath $InputPath).ProviderPath)
  } elseif ($PSBoundParameters.ContainsKey('InputBytes')) {
    [System.IO.MemoryStream]::new([byte[]]@($InputBytes), $false)
  } elseif ($PSBoundParameters.ContainsKey('InputLines')) {
    $newline = [Environment]::NewLine
    $text = (($InputLines | ForEach-Object { "$_" }) -join $newline) + $newline
    [System.IO.MemoryStream]::new($OutputEncoding.GetBytes($text), $false)
  }
  $call = $null
  $output = $null
  $errors = $null
  try {
    $call = Open-PreloadCall -Arguments $Arguments -Source $source
    $source = $null
    if ($Capture -or $DiscardOutput) {
      $output = if ($DiscardOutput) { [System.IO.Stream]::Null } else { [System.IO.MemoryStream]::new() }
      $errors = if ($DiscardOutput) { [System.IO.Stream]::Null } else { [System.IO.MemoryStream]::new() }
      [System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]@(
        $call.Channels[1].CopyToAsync($output), $call.Channels[2].CopyToAsync($errors)))
      $code = Complete-PreloadCall $call
      if ($DiscardOutput) { return $code }
      return [pscustomobject]@{
        Stdout = $script:PreloadEncoding.GetString($output.ToArray())
        Stderr = $script:PreloadEncoding.GetString($errors.ToArray())
        ExitCode = $code
      }
    }
    # Native stderr never stops a script, even with $ErrorActionPreference =
    # 'Stop', so stderr records are written with Continue in this scope.
    $ErrorActionPreference = 'Continue'
    $encoding = [Console]::OutputEncoding
    $out = [System.IO.StreamReader]::new($call.Channels[1], $encoding)
    $err = [System.IO.StreamReader]::new($call.Channels[2], $encoding)
    $pending = @{ 0 = $out.ReadLineAsync(); 1 = $err.ReadLineAsync() }
    while ($pending.Count) {
      $keys = @($pending.Keys)
      $index = [System.Threading.Tasks.Task]::WaitAny(
        [System.Threading.Tasks.Task[]]@($keys | ForEach-Object { $pending[$_] }))
      $key = $keys[$index]
      $line = $pending[$key].Result
      if ($null -eq $line) { $pending.Remove($key); continue }
      if ($key -eq 0) {
        $line
        $pending[0] = $out.ReadLineAsync()
      } else {
        # Native stderr lines become NativeCommandErrorMessage records, so
        # 2>&1 and 2>$null treat them as they treat native stderr.
        $PSCmdlet.WriteError([System.Management.Automation.ErrorRecord]::new(
          [System.Management.Automation.RemoteException]::new($line),
          'NativeCommandErrorMessage', 'NotSpecified', $null))
        $pending[1] = $err.ReadLineAsync()
      }
    }
    $null = Complete-PreloadCall $call
  } finally {
    if ($call) { Close-PreloadCall $call }
    if ($source) { $source.Dispose() }
    if ($output) { $output.Dispose() }
    if ($errors) { $errors.Dispose() }
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
  if ($session.Function) {
    Remove-Item -Path "function:$($session.Function)" -ErrorAction SilentlyContinue
  }
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
