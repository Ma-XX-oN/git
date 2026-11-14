#!/usr/bin/env python3
"""RWF 809: validate same-capture four-configuration ETW records and medians."""
import csv
import json
import math
import re
import statistics
import sys
from pathlib import Path

STAGES = (
    "01-uninstrumented",
    "02-fileio",
    "03-fileio-cpu-stacks-scheduling",
    "04-fileio-cpu-disk-minifilters",
)
MODES = ("win32_all", "nt_all", "win32_metadata", "nt_metadata")
FIELDS = ("wall_ms", "open_ms", "read_ms", "close_ms", "metadata_ms")
EXPECTED = {(mode, thread, rnd) for mode in MODES
            for thread in (1, 4) for rnd in (999, 0, 1, 2, 3, 4)}

def analyze(root: Path, out: Path):
    assert (root / "identity.json").exists(), "Missing exact source and binary identity"
    identity = json.loads((root / "identity.json").read_text(encoding="utf-8-sig"))
    assert identity.get("source_sha") == "d18a959ecc7ec551ae0b83d68b7155965edee4f4", identity
    summary = []
    for stage in STAGES:
        content = (root / f"{stage}-measurements.txt").read_text(encoding="utf-8-sig")
        assert content.count("RWF809_ETW_PASS ") == 1, (stage, "terminal validation")
        records = []
        for line in content.splitlines():
            if not line.startswith("RWF809_NT_CSV "):
                continue
            fields = next(csv.reader([line[len("RWF809_NT_CSV "):]]))
            assert len(fields) == 12, (stage, fields)
            mode, shape, size, rnd, thread = fields[:5]
            assert mode in MODES
            assert (int(shape), int(size), int(fields[10]), int(fields[11])) == (0, 871, 8000, 6968000)
            assert int(thread) in (1, 4) and int(rnd) in (999, 0, 1, 2, 3, 4)
            values = [float(x) for x in fields[5:10]]
            assert all(math.isfinite(v) and v >= 0 for v in values), fields
            records.append((mode, int(thread), int(rnd), values))
        assert len(records) == 48, (stage, len(records))
        keys = [(mode, thread, rnd) for mode, thread, rnd, _ in records]
        assert len(set(keys)) == 48 and set(keys) == EXPECTED, (stage, "duplicates/missing")
        if stage != STAGES[0]:
            trace = root / f"{stage}.etl"
            assert trace.exists() and trace.stat().st_size > 4096, (stage, "missing ETL")
            status = (root / f"{stage}-status.txt").read_text(encoding="utf-8-sig")
            loss = re.findall(r"Events Lost\s*:\s*(\d+)", status, re.I)
            assert loss and all(int(x) == 0 for x in loss), (stage, "events lost", loss)
            assert (root / f"{stage}-events-summary.txt").stat().st_size > 0, (stage, "no provider summary")
        for thread in (1, 4):
            for mode in MODES:
                group = [durations for m, t, rnd, durations in records
                         if m == mode and t == thread and rnd != 999]
                assert len(group) == 5, (stage, mode, thread)
                result = {"stage": stage, "mode": mode, "threads": thread, "measured_rounds": 5}
                for idx, field in enumerate(FIELDS):
                    sorted_values = sorted(row[idx] for row in group)
                    result[field.replace("_ms", "_median_ms")] = statistics.median(sorted_values)
                    if field == "wall_ms":
                        result["wall_min_ms"] = sorted_values[0]
                        result["wall_max_ms"] = sorted_values[-1]
                        result["wall_empirical_p95_nearest_rank_ms"] = sorted_values[-1]
                summary.append(result)
    out.write_text(json.dumps({
        "source_identity": identity,
        "origin_run": 43611008082,
        "stages": list(STAGES),
        "record_count": 4*48,
        "summary": summary,
        "note": "P95 with five observations equals maximum; tracerpt counts are not kernel stack CPU attribution"
    }, indent=2), encoding="utf-8")
    print("RWF809_ETW_ANALYSIS_PASS 192 records, 32 mode/thread/stage summaries, 3 complete ETL stages")
    for x in summary:
        if x["threads"] == 1 and x["mode"] == "win32_all":
            print(x["stage"], x["wall_median_ms"], "ms")
    return 0

if __name__ == "__main__":
    assert len(sys.argv) == 3, "usage: analyze.py ROOT OUTPUT_JSON"
    raise SystemExit(analyze(Path(sys.argv[1]), Path(sys.argv[2])))
