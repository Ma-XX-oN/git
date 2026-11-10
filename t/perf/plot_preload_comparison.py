#!/usr/bin/env python3
"""Plot matched preload medians in milliseconds and percent of each local baseline."""
import argparse
import hashlib
import json
from pathlib import Path


def load_comparison(path):
    """Read a measured comparison and verify its controls.

    Parameters
    ----------
    path : Path
        Raw matched.json report from compare_preload_versions.py.

    Returns
    -------
    dict
        Original measurements with a source digest for graph provenance.
    """
    data = path.read_bytes()
    report = json.loads(data)
    assert report["full_context"] and not report["tracing"], "comparison controls differ"
    variants = report["variants"]
    assert variants["baseline"]["flags"] == variants["candidate"]["flags"], "compiler flags differ"
    assert all(value > 0 for value in report["medians_ms"].values()), "invalid timing"
    report["source"] = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}
    return report


def plot_comparisons(reports, labels, output):
    """Create absolute and baseline-normalized plots without pooling machines.

    Parameters
    ----------
    reports : list of dict
        Validated matched comparisons with medians_ms and original raw samples.
    labels : list of str
        One machine/stage label per comparison, in display order.
    output : Path
        Existing directory receiving PNG and SVG figures.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axes = plt.subplots(1, 2, figsize=(12, max(3.2, len(reports) * .9)),
                                layout="constrained")
    baseline = [report["medians_ms"]["baseline"] for report in reports]
    candidate = [report["medians_ms"]["candidate"] for report in reports]
    percentages = [100 * new / old for new, old in zip(candidate, baseline)]
    positions = list(range(len(reports)))
    for axis, values, title, unit in (
            (axes[0], (baseline, candidate), "Absolute command latency", "Median milliseconds"),
            (axes[1], ([100] * len(reports), percentages), "Within-machine change", "% of matched baseline")):
        for offset, series, name, color in ((-.17, values[0], "Baseline", "#64748b"),
                                            (.17, values[1], "Migrated", "#0072b2")):
            bars = axis.barh([p + offset for p in positions], series, height=.3,
                             label=name, color=color)
            axis.bar_label(bars, fmt="%.2f", padding=3, fontsize=9)
        axis.set_yticks(positions, labels)
        axis.invert_yaxis()
        axis.set_xlabel(unit)
        axis.set_title(title)
        axis.set_xlim(0, max(*values[0], *values[1]) * 1.22)
        axis.grid(axis="x", alpha=.2)
        axis.set_axisbelow(True)
    axes[1].axvline(100, color="#334155", linestyle="--", linewidth=1)
    handles, names = axes[0].get_legend_handles_labels()
    figure.legend(handles, names, loc="outside lower center", ncol=2, frameon=False)
    figure.suptitle("Preload migration: each pair measured on the same host/job")
    figure.savefig(output / "preload-comparison.png", dpi=180)
    with (output / "preload-comparison.svg").open("w", encoding="utf-8", newline="\n") as svg:
        figure.savefig(svg, format="svg")
    plt.close(figure)


def write_report(reports, labels, output):
    """Preserve graph inputs, machine metadata and signed changes.

    Parameters
    ----------
    reports : list of dict
        Validated measurements, retaining raw samples and build provenance.
    labels : list of str
        Machine/stage labels matching reports.
    output : Path
        Existing destination directory for JSON and Markdown summaries.
    """
    lines = ["# Matched preload performance", "",
             "Each row compares revisions on one host/job. Lower latency is better.", "",
             "| Machine / stage | Baseline ms | Migrated ms | Change ms | Baseline % | Change % |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    evidence = []
    for label, report in zip(labels, reports):
        before, after = (report["medians_ms"][name] for name in ("baseline", "candidate"))
        percent = 100 * after / before
        lines.append(f"| {label} | {before:.4f} | {after:.4f} | {after-before:+.4f} | "
                     f"{percent:.2f}% | {percent-100:+.2f}% |")
        evidence.append({"label": label, "candidate_percent_of_baseline": percent,
                         "change_percent": percent - 100, "measurement": report})
    lines += ["", "![Absolute and normalized medians](preload-comparison.png)", "",
              "Full machine metadata, compiler flags, source digests and raw samples are in",
              "[graph-evidence.json](graph-evidence.json). Missing historical hardware fields",
              "remain unspecified. No timings are averaged across machines.", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")
    (output / "graph-evidence.json").write_text(
        json.dumps(evidence, indent=2) + "\n", encoding="utf-8", newline="\n")


def main():
    """Parse comparison paths and produce exportable figures; requires matplotlib."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, type=Path)
    parser.add_argument("--label", action="append")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    reports = [load_comparison(path) for path in args.input]
    labels = args.label or [f"{r['platform']} / {r['host']}" for r in reports]
    if len(labels) != len(reports):
        parser.error("supply one label per input")
    args.output.mkdir(parents=True, exist_ok=True)
    plot_comparisons(reports, labels, args.output)
    write_report(reports, labels, args.output)


if __name__ == "__main__":
    main()
