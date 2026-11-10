# Bash adapter for selected serial, text-output Git regression cases.
# Test setup runs normally; subsequent shell-level git calls use real workers.
# Client descriptors are isolated from test-lib's reserved descriptors in a
# subshell. This adapter measures correctness, not command latency.

test -n "${BASH_VERSION:-}" || error 'preload regression adapter requires Bash'

# Start a real manager and preserve test-lib's descriptors 8 and 9.
# No arguments. Retain private allocator descriptor copies until test cleanup.
# Return zero after READY; a startup failure aborts the test script.
_git_test_preload_start() {
  local saved_request= saved_reply=
  if { : >&8; } 2>/dev/null; then exec {saved_request}>&8; fi
  if { : >&9; } 2>/dev/null; then exec {saved_reply}>&9; fi
  export GIT_PRELOAD_WORKER_GIT=$GIT_TEST_PRELOAD_GIT
  export GIT_PRELOAD_MANAGER_PYTHON=$GIT_TEST_PRELOAD_PYTHON
  source "$TEST_DIRECTORY/../contrib/preload/git-manager.sh" 3 ||
    error 'preload regression manager failed to start'
  unalias git
  case $OSTYPE in
    msys*|cygwin*)
      exec {_GIT_TEST_PRELOAD_REQUEST}>&8 {_GIT_TEST_PRELOAD_REPLY}<&9
      _GIT_TEST_PRELOAD_MANAGER=$_GIT_PRELOAD_MANAGER_BASH_PID
      _GIT_TEST_PRELOAD_MANAGER_LOG=$_GIT_PRELOAD_MANAGER_LOG
      ;;
    *)
      _GIT_TEST_PRELOAD_REQUEST=$_GIT_PRELOAD_UNIX_REQUEST_FD
      _GIT_TEST_PRELOAD_REPLY=$_GIT_PRELOAD_UNIX_REPLY_FD
      _GIT_TEST_PRELOAD_MANAGER=$_GIT_PRELOAD_UNIX_MANAGER_PID
      _GIT_TEST_PRELOAD_MANAGER_LOG=$_GIT_PRELOAD_UNIX_MANAGER_LOG
      ;;
  esac
  if [[ -n $saved_request ]]; then
    exec 8>&"$saved_request" {saved_request}>&-
  else
    exec 8>&-
  fi
  if [[ -n $saved_reply ]]; then
    exec 9>&"$saved_reply" {saved_reply}>&-
  else
    exec 9>&-
  fi
}

# Stop the test-owned manager and preserve its allocation evidence.
# No arguments. Return zero after shutdown and evidence copy, nonzero on error.
_git_test_preload_stop() {
  _git_preload_stop_manager "$_GIT_TEST_PRELOAD_MANAGER" \
    "$_GIT_TEST_PRELOAD_REQUEST" "$_GIT_TEST_PRELOAD_REPLY" || return 1
  cp "$_GIT_TEST_PRELOAD_MANAGER_LOG" "$GIT_TEST_PRELOAD_LOG.manager.log"
}

_git_test_preload_start
test_atexit '_git_test_preload_stop'

# Run the selected cases' Git commands through the actual Bash client.
# Arguments are ordinary Git argv. Forward text output and command exit status.
# The subshell preserves test-lib descriptors; there is no direct-Git fallback.
git() (
  case $OSTYPE in
    msys*|cygwin*)
      exec 8>&"$_GIT_TEST_PRELOAD_REQUEST" 9<&"$_GIT_TEST_PRELOAD_REPLY"
      ;;
  esac
  printf '%s\n' "$BASHPID" >>"$GIT_TEST_PRELOAD_LOG"
  if git-ipc "$@"; then return 0; else return $?; fi
)
