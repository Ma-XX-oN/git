"""Run selected existing Git assertions through real preload workers.

The explicit selections cover serial text-output commands with a fixed startup
environment. Other cases are excluded, not rerouted to direct Git. The adapter
uses a Bash subshell to protect test-lib descriptors; use the separate paired
benchmark for latency measurements.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from acceptance import shell_path
from test_shell_output import stop_owned_tree

SUITES = (("t0002-gitfile.sh", "1-4,11", "gitfile errors, repository discovery and cwd"),
      ("t0014-alias.sh", "1-3", "internal/external aliases and failure status"),
      ("t1300-config.sh", "1-14", "configuration text, whitespace and quoting"))


def run_suite(script, selection, environment, output, root):
  """Run unchanged assertions and verify every adapted call allocated a worker.

  Parameters
  ----------
  script : str
      Existing Git regression script basename under t/.
  selection : str
      Explicit --run case ranges compatible with the Bash client contract.
  environment : dict
      Child environment; copied before adding suite-specific evidence paths.
  output : Path
      Existing evidence directory receiving logs and worker-call records.
  root : Path
      Source tree containing the unmodified regression assertions.

  Returns
  -------
  dict
      Selection and actual allocation count; failure raises after cleanup.
  """
  env = environment.copy()
  calls = output / (Path(script).stem + ".calls")
  calls.write_text("", encoding="utf-8")
  env["GIT_TEST_PRELOAD_LOG"] = shell_path(calls)
  # -v keeps each failing assertion's actual output in the preserved stdout.
  command = [shutil.which("bash"), "--noprofile", "--norc", script,
         "--run=" + selection, "-v"]
  process = subprocess.Popen(command, cwd=root / "t", env=env,
                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 start_new_session=os.name != "nt")
  try:
    out, err = process.communicate(timeout=600 if os.name == "nt" else 120)
  except subprocess.TimeoutExpired as error:
    (output / (script + ".stdout")).write_bytes(error.stdout or b"")
    (output / (script + ".stderr")).write_bytes(error.stderr or b"")
    stop_owned_tree(process)
    raise
  (output / (script + ".stdout")).write_bytes(out)
  (output / (script + ".stderr")).write_bytes(err)
  assert process.returncode == 0, (script, process.returncode, err, out)
  count = len(calls.read_text(encoding="utf-8").splitlines())
  manager = Path(str(calls) + ".manager.log").read_text(encoding="utf-8")
  allocations = sum(line.startswith("ALLOCATED ") for line in manager.splitlines())
  assert count > 0 and count == allocations, (script, count, allocations)
  print("PRELOAD_EXISTING_REGRESSION_PASS", script, count, flush=True)
  return {"script": script, "selection": selection, "worker_calls": count}


def main():
  """Run the selected suites and preserve explicit coverage and call evidence.

  Returns
  -------
  None
      Prints success only after existing assertions and allocation counts pass.
  """
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", type=Path, required=True)
  parser.add_argument("--output", type=Path, required=True)
  args = parser.parse_args()
  output = args.output.resolve()
  output.mkdir(parents=True, exist_ok=True)
  root = Path(__file__).resolve().parents[2]
  env = os.environ.copy()
  env["GIT_BUILD_DIR"] = shell_path(args.git.resolve().parent)
  env["GIT_TEST_PRELOAD"] = "1"
  env["GIT_TEST_PRELOAD_GIT"] = str(args.git.resolve())
  env["GIT_TEST_PRELOAD_MANAGER"] = os.environ.get("PRELOAD_MANAGER", str(args.git.resolve().parent /
    ("preload-manager.exe" if os.name == "nt" else "preload-manager")))
  rows = []
  for script, selection, scope in SUITES:
    row = run_suite(script, selection, env, output, root)
    row["scope"] = scope
    rows.append(row)
  (output / "coverage.json").write_text(json.dumps(rows, indent=2) + "\n",
                     encoding="utf-8", newline="\n")
  print("PRELOAD_EXISTING_REGRESSIONS_PASS", flush=True)


if __name__ == "__main__":
  main()
