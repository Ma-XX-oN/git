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

test_expect_success 'empty context retains inherited environment' '
	>frame &&
	PRELOAD_TEST_VALUE=inherited test-tool preload context frame >actual &&
	test_grep "^value=inherited$" actual
'

test_expect_success 'context applies the working directory' '
	mkdir destination &&
	printf "cwd\\0%s\\n\\0" "$(pwd)/destination" >frame &&
	test-tool preload context frame >actual &&
	test_grep "^cwd=.*/destination$" actual
'

test_expect_success 'context rejects duplicate working directories' '
	printf "cwd\\0%s\\n\\0cwd\\0%s\\n\\0" "$(pwd)" "$(pwd)" >frame &&
	test_must_fail test-tool preload context frame
'

test_expect_success 'context rejects missing path newline' '
	printf "cwd\\0%s\\0" "$(pwd)" >frame &&
	test_must_fail test-tool preload context frame
'

test_expect_success 'context rejects unterminated fields' '
	printf "cwd\\0%s\\n" "$(pwd)" >frame &&
	test_must_fail test-tool preload context frame
'

test_expect_success 'context rejects empty working directory' '
	printf "cwd\\0\\n\\0" >frame &&
	test_must_fail test-tool preload context frame
'

test_expect_success 'context rejects invalid UTF-8' '
	printf "cwd\\0\\377\\n\\0" >frame &&
	test_must_fail test-tool preload context frame
'

test_expect_success 'context rejects unknown fields' '
	printf "unknown\\0value\\0" >frame &&
	test_must_fail test-tool preload context frame
'

test_done
