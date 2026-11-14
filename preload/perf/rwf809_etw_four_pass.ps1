# RWF 809: four Windows ETW recordings around identical native test executable.
param([Parameter(Mandatory=$true)][string]$Exe,
      [Parameter(Mandatory=$true)][string]$Output)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$exePath=(Resolve-Path -LiteralPath $Exe).Path
$Output=[IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Force -Path $Output | Out-Null
if (!(Get-Command wpr.exe -ErrorAction SilentlyContinue)) { throw "WPR unavailable" }
if (!(Get-Command tracerpt.exe -ErrorAction SilentlyContinue)) { throw "tracerpt unavailable" }
Remove-Item Env:RWF809_DOBBY_DLL -ErrorAction SilentlyContinue

function Invoke-Native {
  param([string]$Program,[string[]]$Arguments,[string]$Log)
  $raw=& $Program @Arguments 2>&1 | Out-String
  $status=$LASTEXITCODE
  $raw | Set-Content -Encoding utf8 -LiteralPath $Log
  if ($status -ne 0) { throw "$Program exit=$status args=$($Arguments -join ' ') log=$Log output=$raw" }
  return $raw
}
function Invoke-Workload {
  param([string]$Name)
  $raw=Invoke-Native -Program $exePath -Arguments @('--etw-workload') -Log (Join-Path $Output "$Name-measurements.txt")
  $rows=@([regex]::Matches($raw,'(?m)^RWF809_NT_CSV '))
  $pass=@([regex]::Matches($raw,'(?m)^RWF809_ETW_PASS '))
  if ($rows.Count -ne 48 -or $pass.Count -ne 1) { throw "$Name incomplete samples=$($rows.Count) pass=$($pass.Count)" }
  $lines=@($raw -split "\r?\n" | Where-Object { $_ -like 'RWF809_NT_CSV *' })
  foreach ($line in $lines) {
    $fields=$line.Substring('RWF809_NT_CSV '.Length).Split(',')
    if ($fields.Count -ne 12) { throw "Malformed CSV: $line" }
    if ([int]$fields[1] -ne 0 -or [int]$fields[2] -ne 871 -or
        [int]$fields[10] -ne 8000 -or [int64]$fields[11] -ne 6968000) {
      throw "Mismatched fixture: $line"
    }
  }
  Write-Host "RWF809_ETW_MODE_PASS $Name 48 samples 8k x 871B"
}
@{
  source_sha=$env:GITHUB_SHA
  binary_sha256=(Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
  runner_os=$env:RUNNER_OS
  runner_image=$env:ImageOS
  runner_image_version=$env:ImageVersion
  executable=$exePath
  started_utc=[DateTime]::UtcNow.ToString('o')
} | ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath (Join-Path $Output 'identity.json')
Invoke-Native 'wpr.exe' @('-profiles') (Join-Path $Output 'wpr-profiles.txt') | Out-Null
Invoke-Native 'fltmc.exe' @('filters') (Join-Path $Output 'minifilter-inventory.txt') | Out-Null
Invoke-Native 'fltmc.exe' @('instances') (Join-Path $Output 'minifilter-instances.txt') | Out-Null
$prepare=Invoke-Native $exePath @('--etw-prepare') (Join-Path $Output 'fixture-prepare.txt')
if ($prepare -notmatch 'RWF809_ETW_PREPARE_PASS') { throw 'Fixture preparation incomplete' }
$stages=@(
  @{Name='01-uninstrumented';Profiles=@()},
  @{Name='02-fileio';Profiles=@('FileIO.Light')},
  @{Name='03-fileio-cpu-stacks-scheduling';Profiles=@('FileIO.Verbose','CPU.Verbose')},
  @{Name='04-fileio-cpu-disk-minifilters';Profiles=@('FileIO.Verbose','CPU.Verbose','DiskIO.Verbose','Minifilter.Verbose')}
)
$stages | ConvertTo-Json -Depth 6 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $Output 'four-config-manifest.json')
foreach ($stage in $stages) {
  $name=[string]$stage.Name
  $profiles=@($stage.Profiles)
  Write-Host "RWF809_ETW_PHASE_BEGIN $name"
  if ($profiles.Count -eq 0) { Invoke-Workload $name; continue }
  $args=@()
  foreach ($profile in $profiles) { $args+=@('-start',$profile) }
  $args+='-filemode'
  $active=$false
  $stopped=$false
  $etl=Join-Path $Output "$name.etl"
  try {
    Invoke-Native 'wpr.exe' $args (Join-Path $Output "$name-start.txt") | Out-Null
    $active=$true
    Invoke-Workload $name
    $status=Invoke-Native 'wpr.exe' @('-status','collectors') (Join-Path $Output "$name-status.txt")
    $losses=@([regex]::Matches($status,'(?im)Events Lost\s*:\s*(\d+)'))
    if ($losses.Count -lt 1) { throw "$name trace loss counters unavailable" }
    foreach ($match in $losses) {
      if ([int64]$match.Groups[1].Value -ne 0) { throw "$name lost events $($match.Groups[1].Value)" }
    }
    Invoke-Native 'wpr.exe' @('-stop',$etl) (Join-Path $Output "$name-stop.txt") | Out-Null
    $stopped=$true
  } finally {
    if ($active -and !$stopped) {
      try { Invoke-Native 'wpr.exe' @('-cancel') (Join-Path $Output "$name-cancel.txt") | Out-Null }
      catch { "WPR cancel failure: $_" | Set-Content -LiteralPath (Join-Path $Output "$name-cancel.txt") }
    }
  }
  if (!(Test-Path -LiteralPath $etl)) { throw "$name ETL not created" }
  $file=Get-Item -LiteralPath $etl
  if ($file.Length -le 4096) {
    throw "$name invalid ETL size $($file.Length) bytes"
  }
  $summary=Join-Path $Output "$name-events-summary.txt"
  Invoke-Native 'tracerpt.exe' @($etl,'-o','NUL','-of','CSV','-summary',$summary,'-y') (Join-Path $Output "$name-tracerpt.txt") | Out-Null
  if (!(Test-Path -LiteralPath $summary) -or (Get-Item -LiteralPath $summary).Length -eq 0) {
    throw "$name missing event summary"
  }
  "name=$name profiles=$($profiles -join '+') etl_bytes=$($file.Length) etl_sha256=$((Get-FileHash -LiteralPath $etl -Algorithm SHA256).Hash.ToLowerInvariant())" |
    Set-Content -Encoding utf8 -LiteralPath (Join-Path $Output "$name-trace-identity.txt")
  Write-Host "RWF809_ETW_TRACE_PASS $name $($file.Length) bytes zero reported lost events"
}
Write-Host 'RWF809_ETW_FOUR_PASS'
