#!/usr/bin/env python3
"""Summarize collected profiles and Linux syscall counts per measured command.

Run DIRECTORY --output FILE against profile_preload.py output. Reports lighter
boundary probes, pre-reply CPU/wall time, observed profiler perturbation, and
strace call counts matched to the Bash stage windows. Traced syscall durations
are deliberately not presented as normal command latency.
"""
import argparse
from bisect import bisect_right
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re
import statistics


def nanoseconds(value):
    """Convert decimal seconds text to integer nanoseconds without float loss."""
    return int(Decimal(value) * 1000000000)


def strace_calls(lines):
    """Yield complete syscalls, reconstructing interleaved unfinished/resumed pairs.

    Parameters
    ----------
    lines : iterable of str
        strace -f -ttt -T -yy output.

    Yields
    ------
    tuple
        Native TID, entry timestamp in epoch ns, and reconstructed syscall text.
        Signals, process exits and unmatched incomplete calls are not syscalls.
    """
    pending = {}
    for line in lines:
        match = re.match(r"\s*(\d+)\s+(\d+\.\d+)\s+(.*)", line)
        if not match:
            continue
        tid, stamp, body = int(match[1]), nanoseconds(match[2]), match[3]
        if "<unfinished ...>" in body:
            pending[tid] = (stamp, body.split("<unfinished ...>")[0].rstrip())
            continue
        resumed = re.match(r"<\.\.\. (\w+) resumed>(.*)", body)
        if resumed:
            earlier = pending.pop(tid, None)
            if earlier is None or not earlier[1].startswith(resumed[1] + "("):
                continue
            stamp, prefix = earlier
            body = prefix + resumed[2]
        if re.match(r"\w+\(", body) and re.search(r"<\d+\.\d+>\s*$", body):
            yield tid, stamp, body


def category(body):
    """Classify a client's syscall by its operation and decoded FIFO pathname."""
    name = body.split("(", 1)[0]
    if name == "read" and re.search(r"/slot\.\d+>", body):
        return "reply_read"
    if name == "write" and "/request>" in body:
        return "allocation_request_write"
    if name == "openat" and "/slot." in body:
        return "mailbox_claim" if ".lock\"" in body else "mailbox_open"
    if name == "openat" and "/worker-" in body:
        return "worker_channel_open"
    if name == "read" and "/worker-" in body:
        return "worker_channel_read"
    if name == "write" and "/worker-" in body:
        return "worker_request_write"
    if name in ("dup", "dup2", "dup3", "fcntl", "close"):
        return "descriptor_setup_or_close"
    return name


def syscall_counts(directory):
    """Count client syscalls within each completed Bash command's timing window.

    Parameters
    ----------
    directory : Path
        A syscalls-shell or syscalls-subshell directory from the Linux tracer.

    Returns
    -------
    dict
        Per-command call-count medians/means, reply bytes and reader TIDs.
        Returns an explicit unavailable status when no completed workload exists.
    """
    trace, stages = directory / "trace.txt", directory / "paced/client-stages.txt"
    if not trace.exists() or not stages.exists():
        return {"status": "no completed Linux syscall workload"}
    windows = [(nanoseconds(row[1]), nanoseconds(row[-1]))
               for line in stages.read_text().splitlines() if (row := line.split())]
    starts = [start for start, _ in windows]
    with trace.open(encoding="utf-8") as source:
        calls = list(strace_calls(source))
    grouped = [[] for _ in windows]
    for tid, stamp, body in calls:
        index = bisect_right(starts, stamp) - 1
        if index >= 0 and stamp <= windows[index][1]:
            grouped[index].append((tid, body))
    counts, sizes, readers = [], [], []
    for group in grouped:
        senders = {tid for tid, body in group if body.startswith("write(")
                   and "/request>" in body and '"GET git ' in body}
        if len(senders) != 1:
            raise ValueError("expected exactly one GET sender per measured command")
        caller = senders.pop()
        client = [body for tid, body in group if tid == caller]
        counts.append(Counter(category(body) for body in client))
        reply_reads = [body for body in client if category(body) == "reply_read"]
        sizes.append(sum(int(match[1]) for body in reply_reads
                         if (match := re.search(r"\)\s*=\s*(\d+)", body))))
        readers.append(caller)
    return {"commands": len(windows), "client_tids": sorted(set(readers)),
            "reply_bytes_median": statistics.median(sizes),
            "calls_per_command": {name: {"median": statistics.median(c[name] for c in counts),
                                          "mean": statistics.mean(c[name] for c in counts)}
                                  for name in sorted(set().union(*counts))}}


def critical_reply(directory):
    """Measure dispatch CPU/wall time up to reply write return, excluding refill.

    Parameters
    ----------
    directory : Path
        Focus-os.write cohort directory containing absolute span CPU timestamps.

    Returns
    -------
    dict
        Diagnostic medians/means in microseconds and mean CPU/wall ratio.
        Queued allocations without a reply on the dispatch thread are excluded.
    """
    data = json.loads((directory / "manager-profile.json").read_text())
    records = [r for r in data["records"] if r["kind"] == "Server.handle"][20:]
    rows = []
    for record in records:
        root = record["events"][0]
        writes = [e for e in record["events"] if e["name"] == "os.write"]
        if not writes:
            continue
        reply = writes[-1]
        wall = reply["end_ns"] - root["start_ns"]
        cpu = reply["start_cpu_ns"] + reply["cpu_ns"] - root["start_cpu_ns"]
        rows.append((wall / 1000, cpu / 1000, (wall - cpu) / 1000, reply["bytes"]))
    return {"calls": len(rows), "reply_bytes_median": statistics.median(row[3] for row in rows),
            "mean_cpu_percent_of_wall":
            100 * sum(row[1] for row in rows) / sum(row[0] for row in rows),
            **{name: {"median": statistics.median(row[index] for row in rows),
                       "mean": statistics.mean(row[index] for row in rows)}
               for index, name in enumerate(("wall_us", "cpu_us", "wall_minus_cpu_us"))}}


def scheduler_intervals(lines, targets):
    """Reconstruct off-CPU and runnable intervals for selected native TIDs.

    Parameters
    ----------
    lines : iterable of str
        perf script output for sched_switch and sched_wakeup using clockid mono.
    targets : set of int
        Native shell and manager thread IDs to track.

    Returns
    -------
    dict
        TID to disjoint off-CPU and runnable interval lists, in monotonic ns.
        Runnable intervals are subsets of off-CPU time; the remainder is blocked.
    """
    result = {tid: {"off": [], "runnable": []} for tid in targets}
    off, runnable = {}, {}
    for line in lines:
        event = re.search(r"(\d+\.\d+): sched:sched_(switch|wakeup): (.*)", line)
        if not event:
            continue
        stamp, kind, body = nanoseconds(event[1]), event[2], event[3]
        if kind == "wakeup":
            match = re.match(r".*:(\d+) \[[^]]+\] CPU:", body)
            if match and (tid := int(match[1])) in off:
                runnable.setdefault(tid, stamp)
            continue
        match = re.match(r".*:(\d+) \[[^]]+\] (\S+) ==> .*:(\d+) \[", body)
        if not match:
            continue
        previous, state, following = int(match[1]), match[2], int(match[3])
        if previous in targets:
            off[previous] = stamp
            if state.startswith("R"):
                runnable[previous] = stamp
            else:
                runnable.pop(previous, None)
        if following in targets:
            if following in off:
                result[following]["off"].append((off.pop(following), stamp))
            if following in runnable:
                result[following]["runnable"].append((runnable.pop(following), stamp))
    return result


def overlap_ns(intervals, start, end):
    """Return overlap of disjoint scheduler intervals with a half-open span, in ns."""
    return sum(max(0, min(right, end) - max(left, start)) for left, right in intervals)


def scheduler_row(intervals, start, end):
    """Split a monotonic span into running, runnable and blocked microseconds."""
    off = overlap_ns(intervals["off"], start, end)
    runnable = overlap_ns(intervals["runnable"], start, end)
    return {"wall_us": (end - start) / 1000, "on_cpu_us": (end - start - off) / 1000,
            "runnable_us": runnable / 1000, "blocked_us": (off - runnable) / 1000}


def summarize_rows(rows):
    """Return mean and median for each metric in nonempty diagnostic rows."""
    return {key: {"mean": statistics.mean(row[key] for row in rows),
                  "median": statistics.median(row[key] for row in rows)} for key in rows[0]}


def analyze_scheduler(directory):
    """Join actual scheduler states to client stages and the manager's reply span.

    Parameters
    ----------
    directory : Path
        Scheduler cohort containing perf output, focused spans, and Bash stages.

    Returns
    -------
    dict
        State durations for each client stage and dispatch-to-reply. No claim is
        made that every blocked interval is GIL waiting; blocking causes require
        matching syscall evidence. Clock anchors align epoch and monotonic time.
    """
    trace = directory / "sched.txt"
    if not trace.exists():
        return {"status": "no scheduler trace"}
    profile = json.loads((directory / "manager-profile.json").read_text())
    records = [r for r in profile["records"] if r["kind"] == "Server.handle"][20:]
    client = [line.split()[1:] for line in (directory / "paced/client-stages.txt").read_text().splitlines()]
    if len(client) != len(records):
        raise ValueError("scheduler cohort GET/stage counts do not match")
    shell = int((directory / "paced/caller.pid").read_text())
    with trace.open() as source:
        intervals = scheduler_intervals(source, {shell, *(r["tid"] for r in records)})
    rows = {}
    names = ("entry", "claimed", "opened", "sent", "replied", "freed",
             "channels", "request", "output", "status")
    for record, stamps in zip(records, client):
        offset = record["mono_ns"] - record["epoch_ns"]
        points = [nanoseconds(stamp) + offset for stamp in stamps]
        for first, second, start, end in zip(names, names[1:], points, points[1:]):
            rows.setdefault("client " + first + "->" + second, []).append(
                scheduler_row(intervals[shell], start, end))
        rows.setdefault("client total", []).append(scheduler_row(intervals[shell], points[0], points[-1]))
        writes = [event for event in record["events"] if event["name"] == "os.write"]
        if writes:
            rows.setdefault("manager dispatch->reply", []).append(scheduler_row(
                intervals[record["tid"]], record["events"][0]["start_ns"], writes[-1]["end_ns"]))
    return {"calls": len(client), "stages": {name: summarize_rows(values) for name, values in rows.items()}}


def analyze(directory):
    """Produce compact operation, perturbation and syscall evidence for a run."""
    report = json.loads((directory / "profile-summary.json").read_text())
    result = {"machine": report["machine"], "kernel_traces": report["kernel_traces"],
              "scheduler": analyze_scheduler(directory / "scheduler"), "workloads": {}}
    for workload, data in report["workloads"].items():
        stages = data["stage-only"]["stages"]
        focused = {}
        for name, cohort in data.get("focused", {}).items():
            operations = cohort["profile"]["operations"]
            operation = operations.get(name, operations["Server.handle"])
            focused[name] = {"wall_us": operation["wall_us"], "cpu_us": operation["cpu_us"],
                             "calls": operation["calls"], "cohort_total_percent_of_stage_only":
                             100 * cohort["stages"]["stages"]["client total"]["median_us"] /
                             stages["client total"]["median_us"]}
        details = {"stages_us": {k: v["median_us"] for k, v in stages.items()},
                   "focused": focused, "syscalls": syscall_counts(directory / ("syscalls-" + workload))}
        if focused:
            details["critical_reply"] = critical_reply(directory / (workload + "-focus-os.write"))
        result["workloads"][workload] = details
    return result


def main():
    """Analyze one retained profiling directory and write a reproducible JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = analyze(args.directory)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
