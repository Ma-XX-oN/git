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


if __name__ == "__main__":
    unittest.main()
