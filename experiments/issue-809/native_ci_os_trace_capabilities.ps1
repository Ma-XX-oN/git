$ErrorActionPreference = "Stop"
# Git #809: discover default GitHub Windows CI runner native startup trace capabilities.
# Only read system configuration. Does not modify settings or processes.
$root = Join-Path $PWD "git809-native-os-trace"
New-Item -Path $root -ItemType Directory -Force | Out-Null
$git = (Get-Command git.exe -ErrorAction Stop).Source
$py = (Get-Command python.exe -ErrorAction Stop).Source
$record = [ordered]@{
  sha = $env:GITHUB_SHA
  runner_os = $env:RUNNER_OS
  runner_image = $env:ImageOS
  runner_image_version = $env:ImageVersion
  git_executable = $git
  python_executable = $py
  git_version = (& $git --version)
  env_path = $env:PATH
  tool_availability = @{}
  wpr_profile_details = ""
  exception = $null
}
foreach ($tool in @("wpr.exe","tracerpt.exe","xperf.exe","logman.exe","Get-WinEvent")) {
  $c = Get-Command $tool -ErrorAction SilentlyContinue
  $record.tool_availability[$tool] = if ($c) { $c.Source } else { $null }
}
try {
  $wpr = Get-Command wpr.exe -ErrorAction SilentlyContinue
  if ($wpr) {
    $record.wpr_profile_details = ((& wpr.exe -profiles 2>&1 | Out-String) -split "`n" | Select-Object -First 80) -join "`n"
  }
}
catch { $record.exception = $_.Exception.Message }
$record | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $root "native-ci-os-trace-capabilities.json") -Encoding UTF8
Write-Output "NATIVE_CI_CAPABILITIES"
$record | ConvertTo-Json -Depth 5
