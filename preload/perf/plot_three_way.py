#!/usr/bin/env python3
"""Plot unmodified Git against the Python- and C++-manager preload builds.

Inputs are three-way.json reports from compare_three_way.py. Each row uses only
samples from one runner and job; machines are never pooled. Error bars are 95%
bootstrap confidence intervals of the median (percentile method, fixed seed).
The left panel shows milliseconds for all three builds; the right panel shows
each preloaded build as a percentage of unmodified Git on the same machine.
"""
import argparse
import hashlib
import json
from pathlib import Path

from plot_upstream import (RESAMPLES, SEED, bootstrap, draw_bars, format_interval,
               interval_decimals)
from svg_output import SvgWriter

STYLES = {
  "upstream_direct": ("Unmodified Git, direct", "#2a78d6", "//"),
  "python_preloaded": ("Preloaded, Python manager (working-version-3)", "#eb6834", ""),
  "cpp_preloaded": ("Preloaded, C++ manager", "#1baf7a", ""),
}
"""Legend name, fill color and hatch per series: categorical slots 1-3, validated
all-pairs (light). Aqua is below 3:1 contrast, so every bar carries a value label
and report.md carries the tables."""

PERCENTS = {"python_percent": "python_preloaded", "cpp_percent": "cpp_preloaded"}
"""Right-panel series and the latency series whose style each one reuses."""

CLIENT_PREFIX = {"Bash": "", "PowerShell": "ps_"}
"""Summary-key prefix of each client; a PowerShell group exists only on Windows."""


def load_report(path):
  """Read one three-way comparison and verify its controls.

  Parameters
  ----------
  path : Path
      three-way.json from compare_three_way.py.

  Returns
  -------
  dict
      Original measurements plus a source digest for graph provenance.
  """
  data = path.read_bytes()
  report = json.loads(data)
  assert not report["tracing"], "comparison tracing must be disabled"
  assert len({build["flags"] for build in report["builds"].values()}) == 1, "compiler flags differ"
  assert {row["variant"] for row in report["rounds"]} == {"python", "cpp"}
  report["source"] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
  return report


def summarize(report):
  """Compute plotted medians, intervals and percentages for one machine.

  Parameters
  ----------
  report : dict
      Validated three-way comparison.

  Returns
  -------
  dict
      (median, low, high) per STYLES and PERCENTS key, plus the same keys with
      the "ps_" prefix when the report has Windows PowerShell rounds. Each
      client's percentages use that client's own unmodified-Git reference.
  """
  rounds = report["rounds"]
  raw = {"upstream_direct": [s for row in rounds for s in row["direct"]["samples_ms"]],
       "python_preloaded": [s for row in rounds if row["variant"] == "python"
                for s in row["ipc"]["samples_ms"]],
       "cpp_preloaded": [s for row in rounds if row["variant"] == "cpp"
               for s in row["ipc"]["samples_ms"]]}
  for variant, key in (("direct", "upstream_direct"), ("python", "python_preloaded"),
             ("cpp", "cpp_preloaded")):
    values = [s for row in report.get("powershell_rounds", []) if row["variant"] == variant
          for s in row["samples_ms"]]
    if values:
      raw["ps_" + key] = values
  summary = {name: bootstrap(values) for name, values in raw.items()}
  for prefix in CLIENT_PREFIX.values():
    for percent, series in PERCENTS.items():
      if prefix + series in raw:
        summary[prefix + percent] = bootstrap(raw[prefix + series], raw[prefix + "upstream_direct"])
  return summary


def groups(summaries, labels):
  """Split machines into plotted groups: one per machine and client.

  Parameters
  ----------
  summaries : list of dict
      summarize() results in display order.
  labels : list of str
      Machine labels matching summaries.

  Returns
  -------
  tuple of list
      (group summaries keyed by STYLES and PERCENTS, group labels). A machine
      with PowerShell results yields a Bash group and a PowerShell group.
  """
  group_summaries, group_labels = [], []
  for summary, label in zip(summaries, labels):
    clients = [client for client, prefix in CLIENT_PREFIX.items()
           if prefix + "upstream_direct" in summary]
    for client in clients:
      prefix = CLIENT_PREFIX[client]
      group_summaries.append({key: summary.get(prefix + key) for key in [*STYLES, *PERCENTS]})
      group_labels.append(f"{label}\n{client}" if len(clients) > 1 else label)
  return group_summaries, group_labels


def plot(summaries, labels, output, title):
  """Write PNG and SVG figures: milliseconds and percent of unmodified Git.

  Parameters
  ----------
  summaries : list of dict
      Group summaries keyed by STYLES and PERCENTS, in display order; a None
      value leaves that bar unmeasured.
  labels : list of str
      Group labels matching summaries.
  output : Path
      Existing destination directory.
  title : str
      Figure title.
  """
  import matplotlib
  matplotlib.use("Agg")
  import matplotlib.pyplot as plt

  styles = dict(STYLES, **{key: STYLES[series] for key, series in PERCENTS.items()})
  figure, axes = plt.subplots(1, 2, figsize=(15, max(3.6, len(labels) * 1.5)),
                layout="constrained")
  draw_bars(axes[0], labels, [(key, [s.get(key) for s in summaries]) for key in STYLES],
        "Command latency", "Median milliseconds (95% CI)", styles=styles)
  draw_bars(axes[1], labels, [(key, [s.get(key) for s in summaries]) for key in PERCENTS],
        "Preloaded as % of unmodified Git",
        "% of unmodified direct Git median, same client (95% CI)", 100, styles=styles)
  handles, names = axes[0].get_legend_handles_labels()
  figure.legend(handles, names, loc="outside lower center", ncol=3, frameon=False)
  figure.suptitle(title)
  figure.savefig(output / "preload-three-way.png", dpi=180)
  with (output / "preload-three-way.svg").open("w", encoding="utf-8", newline="\n") as destination:
    svg = SvgWriter(destination)
    figure.savefig(svg, format="svg")
    svg.finish()
  plt.close(figure)


def cell(summary, key, suffix=""):
  """Format one table cell, or a dash when the value was not measured."""
  return format_interval(summary[key], suffix) if summary.get(key) else "—"


def table_row(label, summary, prefix=""):
  """Format one machine's table row for the Bash ("") or PowerShell ("ps_") series."""
  python, cpp = summary.get(prefix + "python_preloaded"), summary.get(prefix + "cpp_preloaded")
  change = (f"{100 * cpp[0] / python[0] - 100:+.{interval_decimals(summary[prefix + 'cpp_percent'])}f}%"
        if python and cpp else "—")
  return (f"| {label} | {cell(summary, prefix + 'upstream_direct')} | "
      f"{cell(summary, prefix + 'python_preloaded')} | {cell(summary, prefix + 'cpp_preloaded')} | "
      f"{cell(summary, prefix + 'python_percent', '%')} | "
      f"{cell(summary, prefix + 'cpp_percent', '%')} | {change} |")


def write_report(reports, summaries, labels, output):
  """Write the Markdown table and the JSON evidence behind the figures.

  Parameters
  ----------
  reports : list of dict
      Validated measurements, retaining raw samples and build provenance.
  summaries : list of dict
      summarize() results matching reports.
  labels : list of str
      Machine labels matching reports.
  output : Path
      Existing destination directory.
  """
  header = ["| Machine | Unmodified Git ms | Python preloaded ms | C++ preloaded ms "
        "| Python % of unmodified | C++ % of unmodified | C++ change vs Python |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
  lines = ["# Unmodified Git vs Python- and C++-manager preload", "",
       "Each row is measured within one job on one machine. Unmodified Git is the",
       "upstream revision this work branches from, built with identical flags and",
       "launched directly; each direct call is interleaved with a preloaded call from",
       "either the working-version-3 build (Python manager) or the current build",
       "(C++ manager), alternating in ABBA rounds. Superscript and subscript values",
       "are the distances to the bounds of the 95% bootstrap confidence interval.", "",
       "## Bash client", "", *header]
  lines += [table_row(label, summary) for label, summary in zip(labels, summaries)]
  windows = [(label, summary) for label, summary in zip(labels, summaries)
        if summary.get("ps_upstream_direct")]
  if windows:
    lines += ["", "## PowerShell client (Windows)", "",
          "Unmodified Git is launched directly from PowerShell; percentages use that",
          "PowerShell reference. Rounds run direct, Python, C++, C++, Python, direct.", "",
          *header, *(table_row(label, summary, "ps_") for label, summary in windows)]
  evidence = []
  for label, report, summary in zip(labels, reports, summaries):
    evidence.append({"label": label, "confidence": "95% bootstrap of the median",
             "resamples": RESAMPLES, "seed": SEED,
             "summary": {key: dict(zip(("median", "low", "high"), value))
                   for key, value in summary.items() if value},
             "measurement": report})
  lines += ["", "![Medians with 95% confidence intervals](preload-three-way.png)", "",
        "Machine metadata, compiler flags, revisions, binary digests and raw samples",
        "are in [graph-evidence.json](graph-evidence.json). No timings are pooled",
        "across machines.", ""]
  (output / "report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
  (output / "graph-evidence.json").write_text(
    json.dumps(evidence, indent=2) + "\n", encoding="utf-8", newline="\n")


def main():
  """Parse report paths and labels; requires matplotlib and numpy."""
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--input", action="append", required=True, type=Path)
  parser.add_argument("--label", action="append")
  parser.add_argument("--output", required=True, type=Path)
  args = parser.parse_args()
  reports = [load_report(path) for path in args.input]
  labels = args.label or [f"{r['platform']} / {r['host']}" for r in reports]
  if len(labels) != len(reports):
    parser.error("supply one label per input")
  summaries = [summarize(report) for report in reports]
  args.output.mkdir(parents=True, exist_ok=True)
  plot(*groups(summaries, labels), args.output,
     "Unmodified Git vs preloaded Git (Python and C++ managers): each group measured within one job")
  write_report(reports, summaries, labels, args.output)


if __name__ == "__main__":
  main()
