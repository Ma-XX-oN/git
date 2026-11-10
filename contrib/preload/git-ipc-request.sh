# Shared Bash session lifecycle and cwd request fields; all text is UTF-8.

# Stop an owned manager and join cleanup before its allocation names are reused.
# Arguments: child manager PID (0 closes inherited bindings without stopping
# their owner), open request descriptor and open reply descriptor, each above 2.
# Takes ownership of both descriptors. Return zero after cleanup, nonzero if
# STOP cannot be delivered to a live manager or its cleanup exits unsuccessfully.
_git_preload_stop_manager() {
  local manager=$1 request_fd=$2 reply_fd=$3
  if (( manager )) && kill -0 "$manager" 2>/dev/null; then
    if ! printf 'STOP\n' >&"$request_fd"; then
      printf 'GIT-IPC ERROR: manager shutdown request failed\n' >&2
      return 1
    fi
    if ! wait "$manager"; then
      exec {request_fd}>&- {reply_fd}<&-
      printf 'GIT-IPC ERROR: manager cleanup failed\n' >&2
      return 1
    fi
  fi
  exec {request_fd}>&- {reply_fd}<&-
  return 0
}

# Emit only cwd; workers inherit the environment captured at manager startup.
# Argument 1: windows or unix, selecting only the native pwd spelling.
# Return zero on success, one on write failure, or two for an invalid platform.
_git_preload_write_context() {
  local platform=$1
  if [[ $platform == windows ]]; then
    # Retain the cwd investigative switch until migration acceptance.
    if [[ ${GIT_PRELOAD_ABLATION_CWD:-1} == 1 ]]; then
      printf 'cwd\0' || return 1
      pwd -W || return 1
      printf '\0' || return 1
    fi
    return 0
  fi
  [[ $platform == unix ]] || return 2
  printf 'cwd\0' || return 1
  pwd -P || return 1
  printf '\0' || return 1
}
