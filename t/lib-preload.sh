# Bash adapter for selected serial, text-output Git regression cases.
# Test setup runs normally; subsequent shell-level git calls use real workers.
# The preload library allocates its descriptors with {var} redirections, so
# test-lib's reserved descriptors are untouched. This adapter measures
# correctness, not command latency.

test -n "${BASH_VERSION:-}" || error 'preload regression adapter requires Bash'

# Start a real manager and preload the tested Git.
# No arguments. Return zero after READY; a startup failure aborts the test script.
_git_test_preload_start() {
  local client
  export PRELOAD_MANAGER=${GIT_TEST_PRELOAD_MANAGER:-${PRELOAD_MANAGER:-preload-manager}}
  source "$TEST_DIRECTORY/../preload/client/manager.sh" ||
    error 'preload regression manager failed to start'
  preload "$GIT_TEST_PRELOAD_GIT" ||
    error 'preload regression workers failed to start'
  # Keep the generated git function under a private name; the adapter's git()
  # below replaces it to record each call.
  client=$(declare -f git) || error 'preload client function missing'
  eval "_git_test_preload_client${client#git}"
  export -f _git_test_preload_client
  _GIT_TEST_PRELOAD_MANAGER_LOG=$PRELOAD_DIR/manager.log
}

# Stop the test-owned manager and preserve its allocation evidence.
# No arguments. Return zero after shutdown and evidence copy, nonzero on error.
_git_test_preload_stop() {
  _preload_stop || return 1
  cp "$_GIT_TEST_PRELOAD_MANAGER_LOG" "$GIT_TEST_PRELOAD_LOG.manager.log"
}

_git_test_preload_start
test_atexit '_git_test_preload_stop'

# Run the selected cases' Git commands through the actual Bash client.
# Arguments are ordinary Git argv. Forward text output and command exit status.
# The subshell gives each call its own $BASHPID record; there is no direct-Git
# fallback.
git() (
  printf '%s\n' "$BASHPID" >>"$GIT_TEST_PRELOAD_LOG"
  _git_test_preload_client "$@"
)
