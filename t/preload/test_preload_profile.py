#!/usr/bin/env python3
"""Check diagnostic span nesting, exception cleanup and self-time accounting."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "perf"))
from profile_preload_manager import Profiler
from profile_preload import summarize_profile
from analyze_preload_profile import critical_reply, strace_calls, category, scheduler_intervals, scheduler_row


class ProfileTests(unittest.TestCase):
    """Keep profiler accounting independent of Unix transport availability."""

    def test_nested_calls_preserve_values_and_parentage(self):
        """Only active roots record nested calls, with correct parent indices."""
        profiler = Profiler()
        child = profiler.wrap("child", lambda: 42)
        self.assertEqual(child(), 42)
        self.assertEqual(profiler.records, [])
        root = profiler.wrap("root", child, lambda args: True)
        self.assertEqual(root(), 42)
        events = profiler.records[0]["events"]
        self.assertEqual([event["parent"] for event in events], [None, 0])
        self.assertTrue(all(event["end_ns"] >= event["start_ns"] for event in events))
        self.assertTrue(all(event["cpu_ns"] >= 0 for event in events))

    def test_exception_clears_thread_context(self):
        """A failing operation records its duration without contaminating later calls."""
        profiler = Profiler()

        def fail():
            """Raise a diagnostic fixture error."""
            raise ValueError("fixture")

        root = profiler.wrap("root", fail, lambda args: True)
        with self.assertRaisesRegex(ValueError, "fixture"):
            root()
        self.assertIsNone(profiler.local.record)
        self.assertEqual(len(profiler.records), 1)

    def test_threads_have_independent_roots(self):
        """Simultaneous operations retain their own native thread identities."""
        profiler = Profiler()
        barrier = threading.Barrier(2)
        root = profiler.wrap("root", lambda: barrier.wait(5), lambda args: True)
        threads = [threading.Thread(target=root) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len({r["tid"] for r in profiler.records}), 2)
        self.assertTrue(all(r["events"][0]["parent"] is None for r in profiler.records))

    def test_self_time_subtracts_direct_children_once(self):
        """Nested inclusive spans must not double-count critical-path self time."""
        raw = {"calibration": {}, "records": [{"kind": "Server.handle", "events": [
            {"name": "root", "parent": None, "start_ns": 0, "end_ns": 10000, "cpu_ns": 4000},
            {"name": "child", "parent": 0, "start_ns": 1000, "end_ns": 7000, "cpu_ns": 1000}]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            result = summarize_profile(path, warmup=0)
        root = result["operations"]["root"]
        self.assertEqual(root["wall_us"]["median"], 10)
        self.assertEqual(root["self_wall_us"]["median"], 4)
        self.assertEqual(root["self_cpu_us"]["median"], 3)

    def test_strace_reassembles_interleaved_calls(self):
        """Read counts include completed calls split across another thread's event."""
        lines = [
            '10 1.000001 read(0</tmp/slot.0> <unfinished ...>\n',
            '11 1.000002 write(6</tmp/slot.0>, "x", 1) = 1 <0.000001>\n',
            '10 1.000003 <... read resumed>, "x", 1) = 1 <0.000002>\n',
        ]
        calls = list(strace_calls(lines))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0:2], (10, 1000001000))
        self.assertEqual(category(calls[1][2]), "reply_read")
        self.assertEqual(category('openat(0, "/tmp/slot.0.lock", O_EXCL) = 3 <0.1>'),
                         "mailbox_claim")

    def test_critical_reply_excludes_post_reply_refill(self):
        """Service CPU/wall ends at the write return rather than callback completion."""
        record = {"kind": "Server.handle", "events": [
            {"name": "Server.handle", "start_ns": 0, "end_ns": 10000,
             "start_cpu_ns": 0, "cpu_ns": 7000},
            {"name": "os.write", "start_ns": 2000, "end_ns": 3000,
             "start_cpu_ns": 1500, "cpu_ns": 500, "bytes": 42}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "manager-profile.json").write_text(
                json.dumps({"records": [record] * 21}), encoding="utf-8")
            result = critical_reply(path)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["wall_us"]["median"], 3)
        self.assertEqual(result["cpu_us"]["median"], 2)

    def test_scheduler_separates_blocked_from_runnable(self):
        """Wakeup marks runnable delay rather than the entire off-CPU interval."""
        lines = [
            "bash 1 [000] 1.000001000: sched:sched_switch: bash:1 [120] S ==> git:2 [120]",
            "git 2 [000] 1.000003000: sched:sched_wakeup: bash:1 [120] CPU:000",
            "git 2 [000] 1.000005000: sched:sched_switch: git:2 [120] R ==> bash:1 [120]",
        ]
        spans = scheduler_intervals(lines, {1})
        row = scheduler_row(spans[1], 1000000000, 1000006000)
        self.assertEqual(row, {"wall_us": 6, "on_cpu_us": 2, "runnable_us": 2, "blocked_us": 2})


if __name__ == "__main__":
    unittest.main()
