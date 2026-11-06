# Git #809: default Windows GitHub CI kernel trace, no changes to machine settings.
# WPR records 40 native git.exe launches and native no-op control; raw
# system-wide ETL and CSV are never uploaded.
$ErrorActionPreference = 'Stop'
$outDir = Join-Path $PWD 'git809-default-ci-etw'
New-Item -Path $outDir -ItemType Directory -Force | Out-Null
$trace = Join-Path $env:RUNNER_TEMP 'git809-default-process.etl'
$dump = Join-Path $env:RUNNER_TEMP 'git809-default-process.csv'
$summary = Join-Path $env:RUNNER_TEMP 'git809-kernel-summary.txt'
$data = [ordered]@{
  experiment = 'Git809-default-Windows-CI-kernel-process-loader-probe'
  git_source = $env:GITHUB_SHA
  actual_git = (Get-Command git.exe -ErrorAction Stop).Source
  actual_python = (Get-Command python.exe -ErrorAction Stop).Source
  wpr_exists = [bool](Get-Command wpr.exe -ErrorAction SilentlyContinue)
  tracerpt_exists = [bool](Get-Command tracerpt.exe -ErrorAction SilentlyContinue)
  wpr_started = $false
  tracerpt_exit_code = $null
  git_invocations = 0
  elapsed_wall_ms = $null
  event_summary = @{}
  trace_errors = @()
  limitations = 'ETW is observer instrumentation, may change launch timings. Without parsed per-process loader/scheduling events, cause remains unknown. No system settings altered.'
}
$ownsTrace = $false
try {
  if (-not $data.wpr_exists -or -not $data.tracerpt_exists) {
    $data.trace_errors += 'Required built-in WPR/tracerpt unavailable'
  } else {
    $profiles = (& wpr.exe -profiles 2>&1 | Out-String)
    if ($profiles -notmatch '(?i)GeneralProfile') {
      $data.trace_errors += 'GeneralProfile not available'
    } else {
      # Do not disturb another active recording.
      $prior = (& wpr.exe -status 2>&1 | Out-String)
      if ($prior -match '(?i)recording is in progress') {
        $data.trace_errors += 'Other WPR session already active'
      } else {
        $py = @'
import shutil, subprocess, sys
git = shutil.which("git")
assert git and "git" in git.lower(), "default Git not found"
for _ in range(40):
  p = subprocess.run([git,"--version"], stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   check=True, timeout=20)
  assert p.stdout.startswith(b"git version "), "unexpected Git version output"
print("40 default Git invocations: passed", flush=True)
'@
        $untraced = [Diagnostics.Stopwatch]::StartNew()
        & $data.actual_python -c $py
        if ($LASTEXITCODE -ne 0) { throw 'untraced default Git baseline failed' }
        $untraced.Stop()
        $data['control_untraced_before_ms'] = $untraced.Elapsed.TotalMilliseconds
        $start = (& wpr.exe -start GeneralProfile.Light 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) {
          $data.trace_errors += "WPR start failed: $start"
        } else {
          $ownsTrace = $true
          $data.wpr_started = $true
          $timer = [Diagnostics.Stopwatch]::StartNew()
          # Native Python subprocess baseline. No source-built Git or reduced feature flags.

          & $data.actual_python -c $py
          if ($LASTEXITCODE -ne 0) {
            throw 'default native Git invocations failed'
          }
          $timer.Stop()
          $data.git_invocations = 40
          $data.elapsed_wall_ms = $timer.Elapsed.TotalMilliseconds
          $saved = (& wpr.exe -stop $trace 'Git 809 default Windows CI Git process lifetime' -skipPdbGen 2>&1 | Out-String)
          $ownsTrace = $false
          if ($LASTEXITCODE -ne 0) {
            $data.trace_errors += "WPR stop failed: $saved"
          } elseif (Test-Path $trace) {
            $afterTimer = [Diagnostics.Stopwatch]::StartNew()
            & $data.actual_python -c $py
            if ($LASTEXITCODE -ne 0) { throw 'untraced Git after-control failed' }
            $afterTimer.Stop()
            $data['control_untraced_after_ms'] = $afterTimer.Elapsed.TotalMilliseconds
            & tracerpt.exe $trace -o $dump -of CSV -y -summary $summary | Out-Null
            $data.tracerpt_exit_code = $LASTEXITCODE
            # xperf's native process action includes process, thread, image
            # timing reports. Keep only bounded Git rows, never raw ETW.
            $xperfTool = Get-Command xperf.exe -ErrorAction SilentlyContinue
            if ($xperfTool) {
              $processReport = Join-Path $env:RUNNER_TEMP 'git809-process-event-report.txt'
              & $xperfTool.Source -i $trace -o $processReport -a process -image -thread | Out-Null
              $data.event_summary['xperf_process_rc'] = $LASTEXITCODE
              if (Test-Path $processReport) {
                $lines = @(Get-Content $processReport | Where-Object { $_ -match '(?i)git\.exe' } | Select-Object -First 25)
                $data.event_summary['xperf_git_process_rows'] = @(
                  $lines | ForEach-Object {
                    # Redact path segments, and keep only process labels/numbers.
                    $_ -replace '[A-Za-z]:\\[^,\s]+', '[REDACTED_PATH]'
                  }
                )
                # xperf's Process records contain start/end counters in microseconds;
                # each Image row identifies the same process instance by PID.
                # Report aggregate windows only; never persist system-wide records.
                $instances = @{}
                foreach ($line in Get-Content $processReport) {
                  $event = [regex]::Match($line,
                    '^\s*(\d+)\s*,\s*(\d+)\s*,\s*(Process|Image)\s*,.*?git\.exe\s*\(\s*(\d+)\s*\)',
                    [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
                  if (-not $event.Success) { continue }
                  $startUs = [long]$event.Groups[1].Value
                  $endUs = [long]$event.Groups[2].Value
                  $kind = $event.Groups[3].Value
                  $pid = $event.Groups[4].Value
                  if (-not $instances.ContainsKey($pid)) {
                    $instances[$pid] = @{ start = $null; finish = $null; images = [System.Collections.Generic.List[long]]::new() }
                  }
                  if ($kind -eq 'Process') {
                    $instances[$pid].start = $startUs
                    $instances[$pid].finish = $endUs
                  } else {
                    $instances[$pid].images.Add($startUs)
                  }
                }
                $life = [System.Collections.Generic.List[double]]::new()
                $firstImage = [System.Collections.Generic.List[double]]::new()
                $lastImageToExit = [System.Collections.Generic.List[double]]::new()
                $invalid = 0
                foreach ($entry in $instances.Values) {
                  if ($null -eq $entry.start -or $null -eq $entry.finish -or
                      $entry.finish -le $entry.start) { $invalid++; continue }
                  $life.Add(($entry.finish - $entry.start) / 1000.0)
                  if ($entry.images.Count -gt 0) {
                    $sortedImages = @($entry.images | Sort-Object)
                    if ($sortedImages[0] -lt $entry.start -or
                        $sortedImages[-1] -gt $entry.finish) { $invalid++; continue }
                    $firstImage.Add(($sortedImages[0] - $entry.start) / 1000.0)
                    $lastImageToExit.Add(($entry.finish - $sortedImages[-1]) / 1000.0)
                  }
                }
                function MedianMs($numbers) {
                  if ($numbers.Count -eq 0) { return $null }
                  $ordered = @($numbers | Sort-Object)
                  $middle = [int][math]::Floor($ordered.Count / 2)
                  if (($ordered.Count % 2) -eq 1) { return [math]::Round($ordered[$middle],3) }
                  return [math]::Round(($ordered[$middle - 1] + $ordered[$middle]) / 2.0,3)
                }
                $data.event_summary['xperf_timeline'] = [ordered]@{
                  units = 'milliseconds (xperf report counters in microseconds)'
                  process_instances = $instances.Count
                  valid_lifetimes = $life.Count
                  instances_with_images = $firstImage.Count
                  invalid_intervals = $invalid
                  median_process_lifetime_ms = MedianMs $life
                  median_create_to_first_image_ms = MedianMs $firstImage
                  median_last_image_to_exit_ms = MedianMs $lastImageToExit
                  caveat = 'Image events are observational markers, not completion of DLL initialization. These windows cannot by themselves attribute CPU, scheduling, security scanning, or CRT cost.'
                }
                Remove-Item $processReport -Force -ErrorAction SilentlyContinue
              }
            }
            if (Test-Path $summary) {
              $content = Get-Content $summary -ErrorAction SilentlyContinue
              # Report broad event types only, not paths, environment or system traces.
              foreach ($kind in @('Process','Thread','Image','Disk','File','CSwitch','ReadyThread')) {
                $data.event_summary[$kind] = @($content | Where-Object { $_ -match $kind }).Count
              }
            }
            if (Test-Path $dump) {
              $count = 0
              $mentions = 0
              Get-Content $dump | ForEach-Object {
                $count++
                if ($_ -match '(?i)git\.exe') { $mentions++ }
              }
              $data.event_summary['csv_total_rows'] = $count
              $data.event_summary['git_named_rows'] = $mentions
              # Identify the precise decoded ETW event schema without exposing
              # raw kernel-trace data or machine-specific paths.
              $csvRows = @(Import-Csv -Path $dump)
              if ($csvRows.Count -gt 0) {
                $data.event_summary['decoded_column_names'] = @($csvRows[0].PSObject.Properties.Name)
                $data.event_summary['git_event_schema_samples'] = @(
                  $csvRows | Where-Object {
                    ([string]$_.'User Data') -match '(?i)git\.exe'
                  } | Select-Object -First 12 | ForEach-Object {
                    $clean = [ordered]@{}
                    foreach ($field in $_.PSObject.Properties) {
                      $key = $field.Name
                      if ($key -match '(?i)time|stamp|provider|event|process|thread|pid|tid|opcod|keyword') {
                        $value = [string]$field.Value
                        # Keep only bounded numeric values or event labels.
                        if ($value.Length -lt 90 -and $value -notmatch '[\\/]') {
                          $clean[$key] = $value
                        }
                      }
                    }
                    $clean
                  }
                )
              }
            }
          }
        }
      }
    }
  }
} catch {
  $data.trace_errors += $_.Exception.Message
} finally {
  if ($ownsTrace) {
    # Only stop recording owned by this experiment.
    try { & wpr.exe -stop $trace 'Git 809 diagnostic cleanup' -skipPdbGen | Out-Null } catch {}
  }
  foreach ($file in @($trace,$dump,$summary)) {
    if (Test-Path $file) { Remove-Item $file -Force -ErrorAction SilentlyContinue }
  }
  $dest = Join-Path $outDir 'default-ci-kernel-trace-status.json'
  $data | ConvertTo-Json -Depth 6 | Set-Content -Path $dest -Encoding UTF8
  $data | ConvertTo-Json -Depth 6
}
