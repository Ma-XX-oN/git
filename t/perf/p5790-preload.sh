#!/bin/sh

test_description='serial direct versus preloaded Git latency'
. ./perf-lib.sh

test_perf 'preload paired direct and preloaded latency' '
	"${PRELOAD_PYTHON:-python3}" "$TEST_DIRECTORY/perf/preload_pool_benchmark.py"
'

test_done
