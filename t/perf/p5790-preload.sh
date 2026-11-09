#!/bin/sh

test_description='serial direct versus preloaded Git latency'
. ./perf-lib.sh

test_perf 'preload paired latency and worker pool sizes' '
	"${GIT_PRELOAD_MANAGER_PYTHON:-python3}" "$TEST_DIRECTORY/perf/preload_pool_benchmark.py"
'

test_done
