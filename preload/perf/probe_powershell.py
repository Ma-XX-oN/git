#!/usr/bin/env python3
"""Test temporary PowerShell variants to explain measured client overhead."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from compare_versions import machine_metadata
from profile_powershell import ROOT, replace_once, summary


def generate(output):
  """Generate baseline, inline-open and synchronous-read diagnostic copies.

  Parameters
  ----------
  output : Path
      Existing private directory receiving generated client/runner files.

  Returns
  -------
  dict
      Variant name to generated runner path. Production sources are unchanged.
  """
  source = (ROOT / "preload/client/manager.ps1").read_text(encoding="utf-8")
  source = replace_once(source, "Join-Path $PSScriptRoot '../manager/manager_windows.py'",
              "'" + str(ROOT / "preload/manager/manager_windows.py").replace("'", "''") + "'")
  source = replace_once(source, "Join-Path $PSScriptRoot '../src/client/pipe.cs'",
              "'" + str(ROOT / "preload/src/client/pipe.cs").replace("'", "''") + "'")
  # Anchors match the client's 2-space indentation.
  inline = replace_once(source,
    '      $pipe = [Preload.Client.PipeConnector]::Open($script:PreloadSession.PipeName + "\\worker-$workerId\\$channel")\n'
    '      $channels += $pipe',
    '      $pipe = [System.IO.Pipes.NamedPipeClientStream]::new(\n'
    '        \'.\', ($script:PreloadSession.PipeName + "\\worker-$workerId\\$channel"),\n'
    '        [System.IO.Pipes.PipeDirection]::InOut, [System.IO.Pipes.PipeOptions]::Asynchronous)\n'
    '      $channels += $pipe\n'
    '      $pipe.Connect(5000)')
  sync = replace_once(source,
    "    $read = $Pipe.ReadAsync($buffer, $offset, $Count - $offset)\n"
    "    if (-not $read.Wait(10000)) { throw 'Preload reply timed out' }\n"
    "    if ($read.Result -eq 0) { throw 'Preload reply ended prematurely' }\n"
    "    $offset += $read.Result",
    "    $received = $Pipe.Read($buffer, $offset, $Count - $offset)\n"
    "    if ($received -eq 0) { throw 'Preload reply ended prematurely' }\n"
    "    $offset += $received")
  sync = replace_once(sync,
    "      $read = $reader.ReadLineAsync()\n"
    "      if (-not $read.Wait(10000)) { throw 'Worker status timed out' }\n"
    "      $status = $read.Result",
    "      $status = $reader.ReadLine()")
  runners = {}
  for name, text in (("baseline", source), ("inline_opens", inline), ("sync_replies", sync)):
    client = output / (name + ".ps1")
    client.write_text(text, encoding="utf-8", newline="\n")
    runner = (ROOT / "preload/tests/test_powershell.ps1").read_text(encoding="utf-8")
    runner = replace_once(runner, ". (Join-Path $PSScriptRoot '../client/manager.ps1')",
                ". '" + str(client).replace("'", "''") + "'")
    path = output / (name + "-runner.ps1")
    path.write_text(runner, encoding="utf-8", newline="\n")
    runners[name] = path
  return runners


def run(runner, git, output, count, mode):
  """Execute one bounded diagnostic process and preserve its report/logs.

  Parameters
  ----------
  runner : Path
      Generated runner with a diagnostic client.
  git : Path
      Shared built worker executable.
  output : Path
      New per-run output directory.
  count : int
      Measured calls excluding twenty warmups.
  mode : str
      Test for correctness, Preload for discard timings.

  Returns
  -------
  dict
      Correctness results or raw millisecond samples.
  """
  result = subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(runner),
               "-Git", str(git), "-Python", sys.executable, "-Output", str(output),
               "-Mode", mode, "-Count", str(count), "-DiscardOutput"],
              capture_output=True, text=True, timeout=180)
  output.mkdir(parents=True, exist_ok=True)
  (output / "runner.stdout").write_text(result.stdout, encoding="utf-8")
  (output / "runner.stderr").write_text(result.stderr, encoding="utf-8")
  assert result.returncode == 0, result.stderr
  return json.loads((output / "report.json").read_text(encoding="utf-8-sig"))


def main():
  """Validate both variants and run serial ABCCBA comparisons."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", required=True, type=Path)
  parser.add_argument("--output", required=True, type=Path)
  parser.add_argument("--count", type=int, default=200)
  args = parser.parse_args()
  output = args.output.resolve()
  output.mkdir(parents=True, exist_ok=False)
  git = args.git.resolve()
  runners = generate(output)
  checks = {name: run(runners[name], git, output / (name + "-checks"), args.count, "Test")["Passed"]
        for name in ("inline_opens", "sync_replies")}
  rows = []
  for index, name in enumerate(("baseline", "inline_opens", "sync_replies",
                  "sync_replies", "inline_opens", "baseline")):
    result = run(runners[name], git, output / f"{index}-{name}", args.count, "Preload")
    values = [sample * 1000 for sample in result["SamplesMs"]]
    row = {"variant": name, "samples_us": values, "summary": summary(values)}
    rows.append(row)
    print(json.dumps({"variant": name, "summary": row["summary"]}), flush=True)
  summaries = {name: summary([sample for row in rows if row["variant"] == name
                for sample in row["samples_us"]]) for name in runners}
  report = {"machine": machine_metadata(), "correctness": checks, "summary": summaries,
        "binary_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
        "client_sha256": hashlib.sha256((ROOT / "preload/client/manager.ps1").read_bytes()).hexdigest(),
        "generated_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in output.glob("*.ps1")},
        "notes": "Temporary uninstrumented variants of the current client; ABCCBA, discard output, 20 warmups and direct refill calls. Inline-opens keeps asynchronous pipe options and replaces only four shared worker-connector calls. Sync-replies keeps the shared connector and asynchronous pipe handles but uses blocking reads for PID/status; async output drain is unchanged. Sync diagnostic removes per-read deadlines; parent process timeout still applies. Variants are not installed into the library.",
        "runs": rows}
  (output / "probes.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
  main()
