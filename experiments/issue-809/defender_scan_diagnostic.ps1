$ErrorActionPreference = 'Stop'
# Git #809: read-only Windows Defender scan attribution.
# Never change Defender configuration, scanning exclusions, or services.
$outDir = Join-Path $PWD 'git809-defender-results'
New-Item -Path $outDir -ItemType Directory -Force | Out-Null
$out = [ordered]@{
  experiment = 'Git809-readonly-Defender-startup-scan-attribution'
  github_sha = $env:GITHUB_SHA
  support = $null
  defender = $null
  recording = $null
  scan_summary = $null
  limitations = 'No AV configuration changed. Defender performance data identifies scans; elapsed scan time is not automatically identical to blocking Git time. Raw ETL not uploaded.'
}
function Write-Report {
  $out | ConvertTo-Json -Depth 9 | Set-Content -Path (Join-Path $outDir 'windows-defender-diagnostic.json') -Encoding UTF8
}
try {
  $get = Get-Command Get-MpComputerStatus -ErrorAction SilentlyContinue
  $record = Get-Command New-MpPerformanceRecording -ErrorAction SilentlyContinue
  $report = Get-Command Get-MpPerformanceReport -ErrorAction SilentlyContinue
  $out.support = [ordered]@{
    defender_status = [bool]$get
    performance_recorder = [bool]$record
    performance_report = [bool]$report
  }
  if ($get) {
    $status = Get-MpComputerStatus
    $out.defender = [ordered]@{
      antivirus_enabled = $status.AntivirusEnabled
      realtime_enabled = $status.RealTimeProtectionEnabled
      engine_version = $status.AMEngineVersion
    }
  }
  if (-not ($record -and $report)) {
    $out.recording = 'unsupported: Defender Performance cmdlets not present'
    Write-Report
    return
  }
  $etl = Join-Path $env:RUNNER_TEMP 'git809-defender-scan.etl'
  $job = Start-Job -ScriptBlock {
    param([string]$target)
    Import-Module DefenderPerformance -ErrorAction SilentlyContinue
    New-MpPerformanceRecording -RecordTo $target -Seconds 18
  } -ArgumentList $etl
  Start-Sleep -Seconds 2
  $git = (Get-Command git.exe).Source
  $started = [System.Diagnostics.Stopwatch]::StartNew()
  for ($i = 0; $i -lt 180; $i++) {
    $p = Start-Process -FilePath $git -ArgumentList '--version' -NoNewWindow -Wait -PassThru -RedirectStandardOutput "$env:RUNNER_TEMP\git809-ver-out.txt" -RedirectStandardError "$env:RUNNER_TEMP\git809-ver-err.txt"
    if ($p.ExitCode -ne 0) { throw "Git exit $($p.ExitCode)" }
  }
  $started.Stop()
  $out.git_probe = [ordered]@{
    iterations = 180
    total_elapsed_ms = $started.Elapsed.TotalMilliseconds
    avg_elapsed_ms = $started.Elapsed.TotalMilliseconds / 180
    exact_git_binary = $git
  }
  $null = Wait-Job $job -Timeout 60
  $rec = Receive-Job $job -ErrorAction SilentlyContinue
  $out.recording = [ordered]@{
    state = $job.State
    error_text = @($job.ChildJobs | ForEach-Object { $_.Error | ForEach-Object { $_.Exception.Message } }) | Select-Object -First 5
    etl_exists = Test-Path $etl
  }
  if (Test-Path $etl) {
    try {
      # Keep only aggregate process names/count/duration in artifacts.
      $raw = Get-MpPerformanceReport -Path $etl -TopProcesses 20 -TopScans 30 -Raw
      $json = $raw | ConvertTo-Json -Depth 8
      $out.scan_summary = [ordered]@{
        data_returned = $null -ne $raw
        json_length = $json.Length
        # Raw Defender report is retained only on ephemeral runner.
        contains_git_process_mention = $json -match '(?i)git\.exe'
        contains_antimalware_process_mention = $json -match '(?i)MsMpEng'
      }
    }
    catch {
      $out.scan_summary = [ordered]@{ error = $_.Exception.Message }
    }
    Remove-Item $etl -Force -ErrorAction SilentlyContinue
  }
}
catch {
  $out.error = $_.Exception.Message
}
finally { Write-Report }
