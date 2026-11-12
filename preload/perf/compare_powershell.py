#!/usr/bin/env python3
"""Compare warmed Windows PowerShell and Bash clients using the same worker binary."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

from compare_versions import machine_metadata
from stage_timing import shell_path

ROOT = Path(__file__).resolve().parents[2]
BASH = r'''
source "$1" || exit 10
trap '_preload_stop' EXIT
preload "$2" || exit 11
expected=$("$2" --version) || exit 12
actual=$(git --version) || exit 13
[[ $actual == "$expected" ]] || exit 14
for (( i = -20; i < $3; i++ )); do
  "$2" --version >/dev/null || exit 15
  begin=$EPOCHREALTIME
  git --version >/dev/null || exit 16
  end=$EPOCHREALTIME
  (( i < 0 )) || printf '%s %s\n' "$begin" "$end"
done
'''


def powershell(git, output, mode, count, discard=False):
    """Run a separate PowerShell test process and read its report.

    Parameters
    ----------
    git : Path
        Built worker executable; runtime DLLs must be on PATH.
    output : Path
        New artifact directory for this process.
    mode : str
        Test, Preload or Direct, passed to the PowerShell runner.
    count : int
        Measured calls excluding twenty warm-up calls.
    discard : bool, optional
        Drain output directly to Stream.Null and return status only.

    Returns
    -------
    dict
        Correctness checks or millisecond samples from the runner.
    """
    command = [shutil.which("pwsh"), "-NoProfile", "-File",
               str(ROOT / "preload/tests/test_powershell.ps1"),
               "-Git", str(git), "-Python", sys.executable,
               "-Output", str(output), "-Mode", mode, "-Count", str(count)]
    if discard:
        command += ["-DiscardOutput"]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=180)
    output.mkdir(parents=True, exist_ok=True)
    (output / "runner.stdout").write_text(completed.stdout, encoding="utf-8")
    (output / "runner.stderr").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(completed.stderr)
    assert not (output / "session/ready.json").exists(), "manager did not stop"
    return json.loads((output / "report.json").read_text(encoding="utf-8-sig"))


def bash(git, output, count):
    """Time Bash's existing wrapper against the same executable.

    Parameters
    ----------
    git : Path
        Built worker executable.
    output : Path
        New artifact directory for shell output and manager logs.
    count : int
        Measured calls excluding twenty warm-up calls.

    Returns
    -------
    list of float
        Per-call milliseconds from Bash's EPOCHREALTIME timestamps.
    """
    output.mkdir(parents=True)
    env = os.environ.copy()
    env["PRELOAD_PYTHON"] = sys.executable
    env["TMPDIR"] = shell_path(output)
    completed = subprocess.run(
        [shutil.which("bash"), "--noprofile", "--norc", "-c", BASH, "comparison",
         shell_path(ROOT / "preload/client/manager.sh"), shell_path(git), str(count)],
        env=env, capture_output=True, text=True, timeout=180)
    (output / "runner.stdout").write_text(completed.stdout, encoding="utf-8")
    (output / "runner.stderr").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(completed.stderr)
    from decimal import Decimal
    samples = [float((Decimal(end) - Decimal(begin)) * 1000)
               for begin, end in (line.split() for line in completed.stdout.splitlines())]
    assert len(samples) == count, len(samples)
    return samples


def summarize(samples):
    """Return median and p90 milliseconds for a nonempty sample sequence."""
    values = sorted(samples)
    return {"count": len(values), "median_ms": statistics.median(values),
            "p90_ms": values[int(len(values) * .9) - 1]}


def main():
    """Validate the prototype, run serial ABBA blocks and preserve all samples."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--git", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--powershell-output", choices=("capture", "discard"), default="discard")
    args = parser.parse_args()
    if os.name != "nt" or args.count < 1 or args.rounds < 1:
        parser.error("requires Windows and positive count/rounds")
    git = args.git.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    checks = powershell(git, output / "correctness", "Test", args.count)
    direct = powershell(git, output / "direct", "Direct", args.count)
    rows = []
    for block in range(args.rounds):
        for position, client in enumerate(("powershell", "bash", "bash", "powershell")):
            directory = output / f"{block}-{position}-{client}"
            samples = (powershell(git, directory, "Preload", args.count,
                                  discard=args.powershell_output == "discard")["SamplesMs"]
                       if client == "powershell" else bash(git, directory, args.count))
            row = {"block": block, "client": client, "samples_ms": samples,
                   **summarize(samples)}
            rows.append(row)
            print(json.dumps({key: value for key, value in row.items() if key != "samples_ms"}), flush=True)
    summaries = {client: summarize([value for row in rows if row["client"] == client
                                    for value in row["samples_ms"]])
                 for client in ("powershell", "bash")}
    ps_median = summaries["powershell"]["median_ms"]
    bash_median = summaries["bash"]["median_ms"]
    report = {
        "machine": machine_metadata(), "powershell": checks["PowerShell"],
        "correctness": checks["Passed"], "git": str(git),
        "binary_sha256": hashlib.sha256(git.read_bytes()).hexdigest(),
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("preload/client/manager.ps1", "preload/src/client/pipe.cs", "preload/client/manager.sh",
                                       "preload/manager/manager_windows.py", "preload/manager/pool.py",
                                       "preload/tests/test_powershell.ps1", "preload/perf/compare_powershell.py")},
        "workload": "--version; serial paced calls; one direct call between samples; 20 warmups per run",
        "powershell_output": args.powershell_output,
        "limitations": "Capture mode buffers and decodes UTF-8; discard mode drains directly to Stream.Null and returns status only. Bash redirects stdout to /dev/null. No command overrides, stdin or pipeline/redirection equivalence. Startup and one-time shared C# connector compilation are excluded.",
        "summary": summaries, "powershell_percent_of_bash": 100 * ps_median / bash_median,
        "direct_powershell": {**summarize(direct["SamplesMs"]), "samples_ms": direct["SamplesMs"]},
        "powershell_preloaded_percent_of_direct": 100 * ps_median / statistics.median(direct["SamplesMs"]),
        "runs": rows,
    }
    (output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summaries, "powershell_percent_of_bash": report["powershell_percent_of_bash"],
                      "direct": report["direct_powershell"]["median_ms"]}), flush=True)


if __name__ == "__main__":
    main()
