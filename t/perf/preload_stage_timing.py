#!/usr/bin/env python3
"""Break preloaded `git --version` latency into client and manager stages.

Sources contrib/preload/preload-manager.sh with PRELOAD_TIMING set, so each
call appends its stage timestamps ($EPOCHREALTIME) to a file and the manager
logs TIMING lines for each GET. Runs serial calls after a warm-up, pairs the
i-th client record with the i-th manager record, and reports the median and
90th percentile of every stage in microseconds.
"""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys

SHELL = r'''
set -u
source "$1" || exit 10
preload "$2" || exit 11
for (( i = 0; i < 20; i++ )); do git --version >/dev/null || exit 12; done
: >"$PRELOAD_TIMING"
cp "$PRELOAD_DIR/manager.log" "$4/manager-warmup.log" || exit 13
for (( i = 0; i < $3; i++ )); do
  # paced: a direct run between calls lets the pool refill, as in
  # compare_preload_versions.py; burst: back to back, stressing the pool.
  [[ $5 == burst ]] || "$2" --version >/dev/null || exit 16
  git --version >/dev/null || exit 14
done
cp "$PRELOAD_DIR/manager.log" "$4/manager.log" || exit 15
'''

# Client stamp names, in order, for each platform's record type.
CLIENT = {
  "U": ["entry", "claimed", "opened", "sent", "replied", "freed",
        "channels", "request", "output", "status"],
  "W": ["entry", "connected", "sent", "replied", "channels", "request",
        "output", "status"],
}


def shell_path(path):
  """Return a path usable by the native Bash interpreter."""
  if os.name == "nt":
    return subprocess.check_output(["cygpath", "-u", str(path)], text=True).strip()
  return str(path)


def summarize(values):
  """Return median and 90th percentile of microsecond samples."""
  ordered = sorted(values)
  return {"median_us": statistics.median(ordered),
          "p90_us": ordered[int(len(ordered) * .9) - 1], "n": len(ordered)}


def manager_records(path, skip):
  """Parse TIMING GET lines after the first `skip` (the warm-up calls).

  Returns
  -------
  list of dict
      Field name to Decimal wall-clock seconds.
  """
  records = []
  for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("TIMING GET "):
      records.append({key: Decimal(value) for key, value in
                      (field.split("=") for field in line.split()[2:])})
  return records[skip:]


def main():
  """Run the calls and print one PRELOAD_STAGE line per stage."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--git", required=True, type=Path)
  parser.add_argument("--count", type=int, default=500)
  parser.add_argument("--mode", choices=("paced", "burst"), default="paced",
                      help="paced: a direct run between preloaded calls keeps the "
                           "pool full; burst: back-to-back calls")
  parser.add_argument("--output", type=Path, default=Path("preload-stage-timing"))
  args = parser.parse_args()
  output = (args.output / args.mode).resolve()
  output.mkdir(parents=True, exist_ok=True)
  root = Path(__file__).resolve().parents[2]
  timing = output / "client-stages.txt"
  env = {key: value for key, value in os.environ.items()
         if not key.startswith(("GIT_TRACE", "PRELOAD_"))}
  env["PRELOAD_PYTHON"] = sys.executable
  env["PRELOAD_TIMING"] = shell_path(timing)
  subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-c", SHELL, "stages",
                  shell_path(root / "contrib/preload/preload-manager.sh"),
                  shell_path(args.git.resolve()), str(args.count), shell_path(output),
                  args.mode],
                 env=env, check=True, timeout=600)
  client = [line.split() for line in timing.read_text(encoding="utf-8").splitlines()]
  assert len(client) == args.count, len(client)
  kind = client[0][0]
  names = CLIENT[kind]
  warmup = len(manager_records(output / "manager-warmup.log", 0))
  manager = manager_records(output / "manager.log", warmup)
  assert len(manager) == args.count, (len(manager), warmup)
  stages = {}
  for record, server in zip(client, manager):
    stamps = dict(zip(names, (Decimal(value) for value in record[1:])))
    for first, second in zip(names, names[1:]):
      stages.setdefault(f"client {first}->{second}", []).append(
        float((stamps[second] - stamps[first]) * 1000000))
    stages.setdefault("client total", []).append(
      float((stamps["status"] - stamps["entry"]) * 1000000))
    server_names = list(server)
    for first, second in zip(server_names, server_names[1:]):
      stages.setdefault(f"manager {first}->{second}", []).append(
        float((server[second] - server[first]) * 1000000))
    stages.setdefault("transit request (client sent->manager received)", []).append(
      float((server["received"] - stamps["sent"]) * 1000000))
    stages.setdefault("transit reply (manager delivered->client replied)", []).append(
      float((stamps["replied"] - server["delivered"]) * 1000000))
  report = {"platform": sys.platform, "mode": args.mode, "calls": args.count,
            "client_record": kind,
            "stages": {name: summarize(values) for name, values in stages.items()}}
  (output / "stages.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
  for name, summary in report["stages"].items():
    print(f"PRELOAD_STAGE {sys.platform} {args.mode} {name}: median={summary['median_us']:.0f}us "
          f"p90={summary['p90_us']:.0f}us", flush=True)


if __name__ == "__main__":
  main()
