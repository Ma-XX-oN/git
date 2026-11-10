#!/bin/sh

test_description='preload worker startup and context validation'
. ./test-lib.sh

test_expect_success 'the experimental startup spelling is rejected' '
	test_must_fail git --preload 2>err &&
	test_grep "unknown option" err
'

test_expect_success 'worker mode rejects unrelated arguments' '
	test_must_fail git --preload-worker one two 2>err &&
	test_grep "^error: --preload-worker" err
'

test_expect_success 'context applies a complete snapshot' '
	printf "env-export\0declare -x PRELOAD_TEST_VALUE=\"hello world\"\n\0" >frame &&
	test-tool preload context frame >actual &&
	test_grep "^value=hello world$" actual
'

test_expect_success 'context removal does not retain inherited variables' '
	printf "env\0\002\000\000\000\000\000" >frame &&
	PRELOAD_TEST_VALUE=old test-tool preload context frame >actual &&
	test_grep "^value=<unset>$" actual
'

test_expect_success 'empty export snapshot removes inherited variables' '
	printf "env-export\0\0" >frame &&
	PRELOAD_TEST_VALUE=old test-tool preload context frame >actual &&
	test_grep "^value=<unset>$" actual
'

test_expect_success 'small export snapshot accepts an empty value' '
	printf "env-export\0declare -x A=\"\"\n\0" >frame &&
	test-tool preload context frame >actual
'

test_expect_success 'export attributes retain scalar values and exclude arrays' '
	printf "env-export\0declare -rx PRELOAD_TEST_VALUE=\"readonly\"\ndeclare -ix COUNT=\"42\"\ndeclare -ax ARRAY=([0]=\"ignored\")\n\0" >frame &&
	test-tool preload context frame >actual &&
	test_grep "^value=readonly$" actual
'

test_expect_success 'context rejects unterminated fields' '
	printf "env-export\0declare -x PRELOAD_TEST_VALUE=\"x\"" >frame &&
	test_must_fail test-tool preload context frame
'

test_expect_success 'context rejects shell execution syntax' '
	printf "env-export\0echo injected\n\0" >frame &&
	test_must_fail test-tool preload context frame
'

test_done
