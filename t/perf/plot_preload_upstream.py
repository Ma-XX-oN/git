#!/usr/bin/env python3
"""Plot migrated preloaded Git against unmodified upstream Git, per machine.

Inputs are upstream.json reports from compare_preload_upstream.py. Each row
uses only samples from one runner and job; machines are never pooled. Error
bars are 95% bootstrap confidence intervals of the median (percentile method,
fixed seed), so they describe the precision of each plotted median.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

RESAMPLES = 2000
"""Bootstrap resamples per confidence interval."""

SEED = 809
"""Fixed bootstrap seed, so regenerated figures and tables are identical."""

SERIES = {
    "upstream_direct": ("Unmodified Git, direct", "#94a3b8", "//"),
    "migrated_preloaded": ("Migrated Git, preloaded", "#0072b2", ""),
}
"""Legend name, fill color and hatch per plotted series."""


def load_report(path):
    """Read one upstream comparison and verify its controls.

    Parameters
    ----------
    path : Path
        upstream.json from compare_preload_upstream.py.

    Returns
    -------
    dict
        Original measurements plus a source digest for graph provenance.
    """
    data = path.read_bytes()
    report = json.loads(data)
    assert not report["tracing"], "comparison tracing must be disabled"
    builds = report["builds"]
    assert builds["upstream"]["flags"] == builds["migrated"]["flags"], "compiler flags differ"
    assert {row["variant"] for row in report["rounds"]} == {"upstream", "migrated"}
    report["source"] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
    return report


def samples(report):
    """Collect the raw samples behind each plotted series.

    Parameters
    ----------
    report : dict
        Validated upstream comparison.

    Returns
    -------
    dict
        Millisecond sample lists keyed by SERIES name: upstream direct calls
        and preloaded calls from every round.
    """
    return {"upstream_direct": [s for row in report["rounds"] if row["variant"] == "upstream"
                                for s in row["direct"]["samples_ms"]],
            "migrated_preloaded": [s for row in report["rounds"]
                                   for s in row["ipc"]["samples_ms"]]}


def bootstrap(values, reference=None):
    """Return the median and its 95% bootstrap confidence interval.

    Parameters
    ----------
    values : list of float
        Raw samples in milliseconds.
    reference : list of float, optional
        When given, the statistic is 100 * median(values) / median(reference),
        with both sample sets resampled independently.

    Returns
    -------
    tuple of float
        (statistic, lower bound, upper bound).
    """
    import numpy

    rng = numpy.random.default_rng(SEED)
    data = numpy.asarray(values)
    medians = numpy.median(rng.choice(data, (RESAMPLES, data.size)), axis=1)
    statistic = float(numpy.median(data))
    if reference is not None:
        base = numpy.asarray(reference)
        medians = 100 * medians / numpy.median(rng.choice(base, (RESAMPLES, base.size)), axis=1)
        statistic = 100 * statistic / float(numpy.median(base))
    low, high = numpy.percentile(medians, (2.5, 97.5))
    return statistic, float(low), float(high)


def summarize(report):
    """Compute plotted medians, intervals and percentages for one machine.

    Parameters
    ----------
    report : dict
        Validated upstream comparison.

    Returns
    -------
    dict
        (median, low, high) per series in milliseconds, plus "percent": the
        migrated preloaded median as a percentage of unmodified direct Git.
    """
    raw = samples(report)
    summary = {name: bootstrap(values) for name, values in raw.items()}
    summary["percent"] = bootstrap(raw["migrated_preloaded"], raw["upstream_direct"])
    return summary


def draw_bars(axis, labels, series, title, unit, reference=None):
    """Draw grouped horizontal bars with confidence-interval error bars.

    Parameters
    ----------
    axis : matplotlib.axes.Axes
        Destination panel.
    labels : list of str
        Machine labels, top to bottom.
    series : list of tuple
        (SERIES key, list of (median, low, high)) per bar group member.
    title, unit : str
        Panel title and x-axis label.
    reference : float, optional
        X position of a dashed reference line.
    """
    height = .8 / len(series)
    top = max([high for _, rows in series for _, _, high in rows] + [reference or 0])
    for index, (key, rows) in enumerate(series):
        name, color, hatch = SERIES[key]
        offset = (index - (len(series) - 1) / 2) * height
        positions = [p + offset for p in range(len(labels))]
        medians = [m for m, _, _ in rows]
        errors = [[m - low for m, low, _ in rows], [high - m for m, _, high in rows]]
        axis.barh(positions, medians, height=height * .95, label=name, color=color,
                  hatch=hatch, edgecolor="#475569", linewidth=0,
                  xerr=errors, error_kw={"ecolor": "#0f172a", "capsize": 4, "elinewidth": 1})
        for position, (median, _, high) in zip(positions, rows):
            axis.text(high + top * .02, position, f"{median:.2f}", va="center", fontsize=9)
    axis.set_yticks(range(len(labels)), labels)
    axis.invert_yaxis()
    axis.set_xlabel(unit)
    axis.set_title(title)
    axis.set_xlim(0, top * 1.2)
    axis.grid(axis="x", alpha=.2)
    axis.set_axisbelow(True)
    if reference is not None:
        axis.axvline(reference, color="#334155", linestyle="--", linewidth=1)


def plot(summaries, labels, output):
    """Write PNG and SVG figures: milliseconds and percent of unmodified Git.

    Parameters
    ----------
    summaries : list of dict
        summarize() results in display order.
    labels : list of str
        Machine labels matching summaries.
    output : Path
        Existing destination directory.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, 2, figsize=(14, max(3.2, len(labels) * 1.1)),
                                layout="constrained")
    draw_bars(axes[0], labels,
              [(key, [s[key] for s in summaries]) for key in SERIES],
              "Command latency", "Median milliseconds (95% CI)")
    draw_bars(axes[1], labels,
              [("migrated_preloaded", [s["percent"] for s in summaries])],
              "Preloaded as % of unmodified Git", "% of unmodified direct Git median (95% CI)",
              100)
    handles, names = axes[0].get_legend_handles_labels()
    figure.legend(handles, names, loc="outside lower center", ncol=2, frameon=False)
    figure.suptitle("Migrated preload vs unmodified Git: each row measured within one job")
    figure.savefig(output / "preload-comparison.png", dpi=180)
    with (output / "preload-comparison.svg").open("w", encoding="utf-8", newline="\n") as svg:
        figure.savefig(svg, format="svg")
    plt.close(figure)


def interval_decimals(value):
    """Return decimals showing two significant digits of the smaller CI distance.

    Parameters
    ----------
    value : tuple of float
        (median, low, high) from bootstrap().

    Returns
    -------
    int
        Decimal places, from 0 to 4, matching the interval's precision.
    """
    median, low, high = value
    smallest = min(high - median, median - low)
    if smallest <= 0:
        return 4
    return min(4, max(0, 1 - math.floor(math.log10(smallest))))


def format_interval(value, suffix=""):
    """Format a median with asymmetric 95% CI distances for a Markdown cell.

    Parameters
    ----------
    value : tuple of float
        (median, low, high) from bootstrap().
    suffix : str, optional
        Unit appended to the median, such as "%".

    Returns
    -------
    str
        For example "16.94<sup>+0.29</sup><sub>-0.41</sub>", rounded by
        interval_decimals().
    """
    median, low, high = value
    places = interval_decimals(value)
    return (f"{median:.{places}f}{suffix}<sup>+{high - median:.{places}f}</sup>"
            f"<sub>-{median - low:.{places}f}</sub>")


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
    lines = ["# Migrated preload vs unmodified Git", "",
             "Each row is measured within one job on one machine. Unmodified Git is the",
             "upstream revision this work branches from, built with identical flags and",
             "launched directly. Each direct call is interleaved with a preloaded call.",
             "Superscript and subscript values are the distances to the upper and lower",
             "bounds of the 95% bootstrap confidence interval of the median.", "",
             "| Machine | Unmodified Git ms (95% CI) | Migrated preloaded ms (95% CI) "
             "| % of unmodified (95% CI) | Change % |",
             "| --- | ---: | ---: | ---: | ---: |"]
    evidence = []
    for label, report, summary in zip(labels, reports, summaries):
        percent = summary["percent"]
        lines.append(f"| {label} | {format_interval(summary['upstream_direct'])} | "
                     f"{format_interval(summary['migrated_preloaded'])} | "
                     f"{format_interval(percent, '%')} | "
                     f"{percent[0] - 100:+.{interval_decimals(percent)}f}% |")
        evidence.append({"label": label, "confidence": "95% bootstrap of the median",
                         "resamples": RESAMPLES, "seed": SEED,
                         "summary": {key: dict(zip(("median", "low", "high"), value))
                                     for key, value in summary.items()},
                         "measurement": report})
    lines += ["", "![Medians with 95% confidence intervals](preload-comparison.png)", "",
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
    plot(summaries, labels, args.output)
    write_report(reports, summaries, labels, args.output)


if __name__ == "__main__":
    main()
