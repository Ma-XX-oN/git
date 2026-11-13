#!/usr/bin/env python3
"""Plot unmodified Git against the tip's Python and C++ managers, per machine.

Inputs are tip-managers.json reports from compare_tip_managers.py. Each group
uses samples from one runner and job; machines are never pooled. A Bash group
shows unmodified, Python-manager and C++-manager bars. A Windows PowerShell
group adds the generated git function: unmodified, Python and C++ through
Invoke-Preload, then Python and C++ through the function. Percentages use the
same client's unmodified reference. Error bars are 95% bootstrap confidence
intervals of the median (percentile method, fixed seed).
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
  "python_preloaded": ("Preloaded, Python manager", "#eb6834", ""),
  "cpp_preloaded": ("Preloaded, C++ manager", "#1baf7a", ""),
  "python_function": ("Preloaded, Python manager, git function (PowerShell)", "#eb6834", ".."),
  "cpp_function": ("Preloaded, C++ manager, git function (PowerShell)", "#1baf7a", ".."),
}
"""Legend name, fill color and hatch per series. Color follows the manager
(categorical slots 1-3, validated all-pairs, light); the dotted texture marks
the git function. Aqua is below 3:1 contrast, so every bar carries a value label
and report.md carries the tables."""

PERCENTS = {"python_percent": "python_preloaded", "cpp_percent": "cpp_preloaded",
      "python_function_percent": "python_function",
      "cpp_function_percent": "cpp_function"}
"""Right-panel series and the latency series whose style each one reuses."""

POWERSHELL_SERIES = {"direct": "upstream_direct", "python_invoke": "python_preloaded",
           "cpp_invoke": "cpp_preloaded", "python_function": "python_function",
           "cpp_function": "cpp_function"}
"""PowerShell round variant to plotted series."""


def load_report(path):
  """Read one tip-managers comparison and verify its controls.

  Parameters
  ----------
  path : Path
      tip-managers.json from compare_tip_managers.py.

  Returns
  -------
  dict
      Original measurements plus a source digest for graph provenance.
  """
  data = path.read_bytes()
  report = json.loads(data)
  assert not report["tracing"], "comparison tracing must be disabled"
  assert report["builds"]["upstream"]["flags"] == report["builds"]["tip"]["flags"]
  assert {row["variant"] for row in report["rounds"]} == {"python", "cpp"}
  report["source"] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
  return report


def groups(report, label):
  """Return (label, summary) per plotted group: Bash, and PowerShell on Windows.

  Parameters
  ----------
  report : dict
      Validated comparison.
  label : str
      Machine label.

  Returns
  -------
  list of tuple
      (group label, summary keyed by STYLES and PERCENTS; False where a series
      does not apply to the group).
  """
  rounds = report["rounds"]
  raw = {"upstream_direct": [s for row in rounds for s in row["direct"]["samples_ms"]],
       "python_preloaded": [s for row in rounds if row["variant"] == "python"
                for s in row["ipc"]["samples_ms"]],
       "cpp_preloaded": [s for row in rounds if row["variant"] == "cpp"
               for s in row["ipc"]["samples_ms"]]}
  powershell = {}
  for row in report.get("powershell_rounds", []):
    powershell.setdefault(POWERSHELL_SERIES[row["variant"]], []).extend(row["samples_ms"])
  result = []
  for client, samples in (("Bash", raw), ("PowerShell", powershell)):
    if not samples:
      continue
    summary = {key: bootstrap(samples[key]) if key in samples else False for key in STYLES}
    for percent, series in PERCENTS.items():
      summary[percent] = (bootstrap(samples[series], samples["upstream_direct"])
                if series in samples else False)
    result.append((f"{label}\n{client}" if powershell else label, summary))
  return result


def plot(summaries, labels, output):
  """Write PNG and SVG figures: milliseconds and percent of unmodified Git.

  Parameters
  ----------
  summaries : list of dict
      Group summaries keyed by STYLES and PERCENTS, in display order.
  labels : list of str
      Group labels matching summaries.
  output : Path
      Existing destination directory.
  """
  import matplotlib
  matplotlib.use("Agg")
  import matplotlib.pyplot as plt

  styles = dict(STYLES, **{key: STYLES[series] for key, series in PERCENTS.items()})
  widest = max(sum(s[key] is not False for key in STYLES) for s in summaries)
  figure, axes = plt.subplots(1, 2, figsize=(15, max(3.6, len(labels) * (.75 + .32 * widest))),
                layout="constrained")
  draw_bars(axes[0], labels, [(key, [s[key] for s in summaries]) for key in STYLES],
        "Command latency", "Median milliseconds (95% CI)", styles=styles)
  draw_bars(axes[1], labels, [(key, [s[key] for s in summaries]) for key in PERCENTS],
        "Preloaded as % of unmodified Git",
        "% of unmodified direct Git median, same client (95% CI)", 100, styles=styles)
  handles, names = axes[0].get_legend_handles_labels()
  figure.legend(handles, names, loc="outside lower center", ncol=3, frameon=False)
  figure.suptitle("Unmodified Git vs the tip's Python and C++ managers: "
          "each group measured within one job")
  figure.savefig(output / "preload-tip-managers.png", dpi=180)
  with (output / "preload-tip-managers.svg").open("w", encoding="utf-8", newline="\n") as destination:
    svg = SvgWriter(destination)
    figure.savefig(svg, format="svg")
    svg.finish()
  plt.close(figure)


def cell(summary, key, suffix=""):
  """Format one table cell, or a dash where the series does not apply."""
  return format_interval(summary[key], suffix) if summary.get(key) else "—"


def write_report(reports, groups_by_report, output):
  """Write Markdown tables and the JSON evidence behind the figures.

  Parameters
  ----------
  reports : list of dict
      Validated measurements, retaining raw samples and build provenance.
  groups_by_report : list of list
      groups() results matching reports.
  output : Path
      Existing destination directory.
  """
  lines = ["# Unmodified Git vs the tip's Python and C++ managers", "",
       "Each group is measured within one job on one machine. Unmodified Git is the",
       "upstream revision this work branches from, built with identical flags and",
       "launched directly. Both preloaded variants are the same tip build (worker and",
       "clients), with the C++ manager or the Python manager. PowerShell groups also",
       "time the generated git function. Percentages use the same client's",
       "unmodified reference. Superscript and subscript values are the distances to",
       "the bounds of the 95% bootstrap confidence interval of the median.", "",
       "| Group | Unmodified ms | Python ms | C++ ms | Python fn ms | C++ fn ms "
       "| Python % | C++ % | Python fn % | C++ fn % |",
       "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
  evidence = []
  for report, report_groups in zip(reports, groups_by_report):
    for label, summary in report_groups:
      lines.append("| " + label.replace("\n", ", ") + " | " + " | ".join(
        [cell(summary, key) for key in STYLES] +
        [cell(summary, key, "%") for key in PERCENTS]) + " |")
    evidence.append({"groups": [label for label, _ in report_groups],
             "confidence": "95% bootstrap of the median", "resamples": RESAMPLES,
             "seed": SEED,
             "summaries": [{key: dict(zip(("median", "low", "high"), value))
                     for key, value in summary.items() if value}
                    for _, summary in report_groups],
             "measurement": report})
  lines += ["", "![Medians with 95% confidence intervals](preload-tip-managers.png)", "",
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
  groups_by_report = [groups(report, label) for report, label in zip(reports, labels)]
  flat = [group for report_groups in groups_by_report for group in report_groups]
  args.output.mkdir(parents=True, exist_ok=True)
  plot([summary for _, summary in flat], [label for label, _ in flat], args.output)
  write_report(reports, groups_by_report, args.output)


if __name__ == "__main__":
  main()
