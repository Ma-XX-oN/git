#!/usr/bin/env python3
"""Profile PowerShell discard calls and Bash using temporary instrumented clients."""

import argparse
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

from compare_versions import machine_metadata
from stage_timing import CLIENT, manager_records, shell_path

ROOT = Path(__file__).resolve().parents[2]
NAMES = ["entry", "prepared", "connected", "sent", "replied", "validated",
     "worker_parsed", "channels", "reads_started", "output", "status", "cleanup"]


def replace_once(text, before, after):
  """Replace one verified instrumentation anchor; fail on source drift."""
  assert text.count(before) == 1, before
  return text.replace(before, after, 1)


def instrument(output):
  """Generate temporary client and runner copies from the real sources.

  Parameters
  ----------
  output : Path
      Existing private artifact directory receiving the generated sources.

  Returns
  -------
  Path
      Generated runner that saves raw stage timestamps with its samples.
  """
  source = (ROOT / "preload/client/manager.ps1").read_text(encoding="utf-8")
  source = replace_once(source, "Join-Path $PSScriptRoot '../src/client/pipe.cs'",
              "'" + str(ROOT / "preload/src/client/pipe.cs").replace("'", "''") + "'")
  source += "\n$script:PreloadProfile = [System.Collections.Generic.List[object]]::new()\n"
  # Anchors match the client's 2-space indentation.
  anchors = [
    "  if (-not $script:PreloadSession) { throw 'No active preload session' }",
    "  try {\n    $request = [Preload.Client.PipeConnector]::Open",
    "    $line = $script:PreloadEncoding.GetBytes(\"GET $name 0`n\")",
    "    $frame = $script:PreloadEncoding.GetString((Read-PreloadBytes $request 8))",
    "    if ($frame -notmatch '^[0-9a-f]{8}$' -or $frame -eq '00000000') {",
    "    # $request stays open for the call",
    "    foreach ($channel in @('stdin', 'stdout', 'stderr', 'exit-code')) {",
    "    $stdout = $channels[1].CopyToAsync($output)",
    "    if (-not [System.Threading.Tasks.Task]::WaitAll(",
    "    $channels[0].Dispose()",
    "    if ($DiscardOutput) { return $global:LASTEXITCODE }",
    "    $errors.Dispose()\n  }\n}",
  ]
  for index, anchor in enumerate(anchors):
    stamp = f"$profileStamps[{index}] = [System.Diagnostics.Stopwatch]::GetTimestamp()"
    if index == 0:
      inserted = "  $profileStamps = [long[]]::new(12)\n  " + stamp + "\n" + anchor
    elif index == 11:
      inserted = "    $errors.Dispose()\n    " + stamp + "\n    $script:PreloadProfile.Add($profileStamps)\n  }\n}"
    else:
      indent = "  " if index == 1 else "    "
      inserted = indent + stamp + "\n" + anchor
    source = replace_once(source, anchor, inserted)
  client = output / "profile-client.ps1"
  client.write_text(source, encoding="utf-8", newline="\n")
  runner = (ROOT / "preload/tests/test_powershell.ps1").read_text(encoding="utf-8")
  runner = replace_once(runner, ". (Join-Path $PSScriptRoot '../client/manager.ps1')",
              ". '" + str(client).replace("'", "''") + "'")
  runner = replace_once(runner, "  $report | ConvertTo-Json -Depth 6",
              "  $report['ProfileTicks'] = $script:PreloadProfile.GetRange(20, $Count).ToArray()\n"
              "  $report['Frequency'] = [System.Diagnostics.Stopwatch]::Frequency\n"
              "  $report | ConvertTo-Json -Depth 6")
  path = output / "profile-runner.ps1"
  path.write_text(runner, encoding="utf-8", newline="\n")
  return path


def summary(values):
  """Summarize nonempty microsecond samples with mean, median and p90."""
  values = sorted(values)
  return {"n": len(values), "mean_us": statistics.mean(values),
      "median_us": statistics.median(values), "p90_us": values[int(len(values) * .9) - 1]}


def main():
  """Run serial ABBA profiles, preserving raw clocks and source copies."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", required=True, type=Path)
  parser.add_argument("--output", required=True, type=Path)
  parser.add_argument("--count", type=int, default=200)
  args = parser.parse_args()
  output = args.output.resolve()
  output.mkdir(parents=True, exist_ok=False)
  runner = instrument(output)
  git = args.git.resolve()
  records = []
  for index, client in enumerate(("powershell", "bash", "bash", "powershell")):
    directory = output / f"{index}-{client}"
    env = os.environ.copy()
    env["PRELOAD_PYTHON"] = sys.executable
    if client == "powershell":
      env["PRELOAD_TIMING"] = str(directory / "timing")
      command = [shutil.which("pwsh"), "-NoProfile", "-File", str(runner),
             "-Git", str(git), "-Manager", os.environ.get("PRELOAD_MANAGER", "preload-manager"), "-Output", str(directory),
             "-Mode", "Preload", "-Count", str(args.count), "-DiscardOutput"]
    else:
      command = [sys.executable, str(ROOT / "preload/perf/stage_timing.py"),
             "--git", str(git), "--count", str(args.count), "--output", str(directory)]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=240)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "runner.stdout").write_text(result.stdout, encoding="utf-8")
    (directory / "runner.stderr").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    if client == "powershell":
      raw = json.loads((directory / "report.json").read_text(encoding="utf-8-sig"))
      stamps = [{name: tick * 1e6 / raw["Frequency"] for name, tick in zip(NAMES, row)}
            for row in raw["ProfileTicks"]]
      manager = manager_records(directory / "session/manager.log", 20)
      groups = [("request_setup_and_send", "entry", "sent"),
            ("allocation_reply", "sent", "replied"),
            ("worker_channels", "replied", "channels"),
            ("output", "channels", "output"),
            ("status_cleanup", "output", "cleanup")]
      detail = {(first + "->" + second): [row[second] - row[first] for row in stamps]
            for first, second in zip(NAMES, NAMES[1:])}
      detail["outside_body"] = [ms * 1000 - (row["cleanup"] - row["entry"])
                    for ms, row in zip(raw["SamplesMs"], stamps)]
      end = "cleanup"
    else:
      directory = directory / "paced"
      raw = [line.split() for line in (directory / "client-stages.txt").read_text().splitlines()]
      stamps = [{name: float(Decimal(value) * 1000000)
             for name, value in zip(CLIENT[row[0]], row[1:])} for row in raw]
      skip = len(manager_records(directory / "manager-warmup.log", 0))
      manager = manager_records(directory / "manager.log", skip)
      groups = [("request_setup_and_send", "entry", "sent"),
            ("allocation_reply", "sent", "replied"),
            ("worker_channels", "replied", "channels"),
            ("output", "channels", "output"),
            ("status_cleanup", "output", "status")]
      detail = {}
      end = "status"
    assert len(stamps) == len(manager) == args.count
    stages = {name: [row[second] - row[first] for row in stamps] for name, first, second in groups}
    stages["total"] = [row[end] - row["entry"] for row in stamps]
    stages["manager_allocate"] = [float((row["allocated"] - row["received"]) * 1000000) for row in manager]
    stages["manager_publish"] = [float((row["delivered"] - row["allocated"]) * 1000000) for row in manager]
    record = {"client": client, "stages": stages, "detail": detail}
    records.append(record)
    print(json.dumps({"client": client, "stages": {key: summary(value) for key, value in stages.items()}}), flush=True)
  combined = {}
  for client in ("powershell", "bash"):
    rows = [row for row in records if row["client"] == client]
    combined[client] = {kind: {key: summary([sample for row in rows for sample in row[kind][key]])
                  for key in rows[0][kind]} for kind in ("stages", "detail")}
  report = {"machine": machine_metadata(), "binary_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
        "clock": "Stopwatch ticks for PowerShell; EPOCHREALTIME for Bash; relative durations only",
        "notes": "Instrumented discard calls, 20 warmups and one direct refill call between samples. Stage medians do not add; means do. PowerShell total excludes function binding/return and profiler record append; outside_body measures these plus harness checks. No per-call profile file I/O in PowerShell.",
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                for name in ("preload/client/manager.ps1", "preload/src/client/pipe.cs", "preload/client/manager.sh",
                       "preload/tests/test_powershell.ps1", "preload/perf/profile_powershell.py")},
        "summary": combined, "runs": records}
  (output / "profile.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
  main()
