# Source on Git Bash, Linux, or macOS; Bash identifies the transport at startup.
case ${OSTYPE:-} in
  linux*|darwin*)
    source "$(dirname -- "${BASH_SOURCE[0]}")/git-manager-unix.sh" "$@"
    return $?
    ;;
  msys*|cygwin*) ;;
  *)
    printf 'Git IPC does not support OSTYPE=%s\n' "${OSTYPE:-unset}" >&2
    return 2
    ;;
esac

source "$(dirname -- "${BASH_SOURCE[0]}")/git-ipc-request.sh" || return 1
# Source into Git Bash: source /path/to/git-manager.sh [worker-count]
# IPC client and manager launcher for the native Git --preload-worker worker.
# Requires GIT_PRELOAD_WORKER_GIT (native Git executable with --preload-worker support)
# and GIT_PRELOAD_MANAGER_PYTHON (native Windows Python 3).
# Busy-wait without a child process. Usage: sleep seconds (one numeric value).
# Bash discards /dev/zero's NUL bytes, so read ends only at its timeout.
sleep() {
  local delay_data status
  if (( $# != 1 )); then
    printf 'sleep requires one duration in seconds\n' >&2
    return 2
  fi
  if IFS= read -r -N 1 -t "$1" delay_data < /dev/zero; then
    return 0
  else
    status=$?
  fi
  # SIGALRM is the expected timeout; preserve errors and other signals.
  if (( status == 142 )); then return 0; fi
  return "$status"
}

# Argument 1: initial worker count (default 3, range 1 through 32).
# Retain manager identity and discovery paths in the sourcing owner shell.
# Return zero after READY; nonzero denotes configuration or startup failure.
_git_preload_source_manager() {
  local requested=${1:-3}
  if [[ ! $requested =~ ^[1-9][0-9]*$ ]] ||
     (( ${#requested} > 2 || requested > 32 )); then
    printf 'worker count must be an integer from 1 to 32\n' >&2
    return 2
  fi
  if [[ -z ${GIT_PRELOAD_WORKER_GIT:-} || -z ${GIT_PRELOAD_MANAGER_PYTHON:-} ]]; then
    printf 'set GIT_PRELOAD_WORKER_GIT and GIT_PRELOAD_MANAGER_PYTHON before sourcing\n' >&2
    return 2
  fi
  local owner_pid
  IFS= read -r owner_pid < "/proc/$BASHPID/winpid" || return 1
  if [[ ! $owner_pid =~ ^[1-9][0-9]*$ ]]; then
    printf 'cannot resolve owning shell native Windows PID\n' >&2
    return 1
  fi
  if [[ -n ${_GIT_PRELOAD_MANAGER_BASH_PID:-} ]]; then
    local previous=0
    if [[ ${_GIT_PRELOAD_OWNER_PID:-} == "$owner_pid" ]]; then
      previous=$_GIT_PRELOAD_MANAGER_BASH_PID
    fi
    if [[ ${_GIT_PRELOAD_ALLOC_CONNECTED:-} == 1 ]]; then
      _git_preload_stop_manager "$previous" 8 9 || return 1
    elif (( previous )) && kill -0 "$previous" 2>/dev/null; then
      printf 'GIT-IPC ERROR: live manager has no shutdown channel\n' >&2
      return 1
    fi
  fi
  unset _GIT_PRELOAD_ALLOC_CONNECTED _GIT_PRELOAD_MANAGER_BASH_PID
  unset _GIT_PRELOAD_MANAGER_READY _GIT_PRELOAD_MANAGER_LOG _GIT_PRELOAD_REQUEST_SEQ
  local script_path script_dir temp native_directory
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 1
  script_path=$(/usr/bin/cygpath -w "$script_dir/windows_git_ipc_pool.py") || return 1
  temp=$(mktemp -d "${TMPDIR:-${TEMP:-${TMP:-/tmp}}}/git-preload-$owner_pid-XXXXXXXX") || return 1
  native_directory=$(/usr/bin/cygpath -w "$temp") || return 1
  "$GIT_PRELOAD_MANAGER_PYTHON" "$script_path" --git "$GIT_PRELOAD_WORKER_GIT" \
    --workers "$requested" --owner-pid "$owner_pid" --directory "$native_directory" \
    >"$temp/manager.log" 2>&1 </dev/null &
  local launched=$!
  local i
  for ((i=0;i<200;i++)); do
    if [[ -f $temp/ready.json ]]; then
      _GIT_PRELOAD_MANAGER_BASH_PID=$launched
      _GIT_PRELOAD_MANAGER_READY=$temp/ready.json
      _GIT_PRELOAD_MANAGER_WORKERS=$requested
      _GIT_PRELOAD_OWNER_PID=$owner_pid
      _GIT_PRELOAD_MANAGER_LOG=$temp/manager.log
      return 0
    fi
    if ! kill -0 "$launched" 2>/dev/null; then
      printf 'Git manager failed to start; see %s\n' "$temp/manager.log" >&2
      return 1
    fi
    sleep 0.05
  done
  printf 'Git manager did not reach READY; see %s\n' "$temp/manager.log" >&2
  return 1
}
_git_preload_source_manager "$@"
_git_preload_source_rc=$?
unset -f _git_preload_source_manager
if (( _git_preload_source_rc != 0 )); then
  return "$_git_preload_source_rc"
fi

# Close selected descriptor numbers, or all reserved IPC descriptors in the
# current git-ipc call when no arguments are supplied. Repeated closes are safe.
# Arguments: optional descriptor numbers from 3 through 7; zero arguments closes all.
# Preserve allocator descriptors 8 and 9. Return zero after cleanup, or 2 for
# an invalid descriptor argument; unopened and already closed descriptors are safe.
close_fhs() {
  local fd
  if (( $# == 0 )); then
    exec 3>&- 4>&- 5>&- 6>&- 7>&-
    return
  fi
  for fd in "$@"; do
    case $fd in
      3|4|5|6|7) exec {fd}>&- ;;
      *) return 2 ;;
    esac
  done
}

# Open any Windows IPC channel and consume its one-byte call marker here.
# Keep the established descriptor numbers and opening order unchanged.
# Arguments: descriptor number, validated native pipe path, decimal marker,
# and maximum attempts (default 1). Failed attempts use a builtin sleep retry delay.
# On success the caller owns the descriptor and the marker has been written.
# Return zero on success, nonzero after invalid arguments or exhausted attempts.
_git_preload_open_pipe() {
  local fd=$1 path=$2 marker=$3 retries=${4:-1} attempt
  [[ $fd == [3-9] && $marker == [0-9] &&
     $retries == [1-9]*([0-9]) ]] || return 2
  for ((attempt=0; attempt<retries; attempt++)); do
    case $fd in
      3) { exec 3<>"$path"; } 2>/dev/null ;;
      4) { exec 4<>"$path"; } 2>/dev/null ;;
      5) { exec 5<>"$path"; } 2>/dev/null ;;
      6) { exec 6<>"$path"; } 2>/dev/null ;;
      7) { exec 7<>"$path"; } 2>/dev/null ;;
      8) { exec 8<>"$path"; } 2>/dev/null ;;
      9) { exec 9<>"$path"; } 2>/dev/null ;;
    esac
    if (( $? == 0 )); then
      if printf '%s' "$marker" >&"$fd"; then
        return 0
      fi
      if [[ $fd == 8 ]]; then exec 8>&-;
      elif [[ $fd == 9 ]]; then exec 9>&-;
      else close_fhs "$fd"; fi
      return 1
    fi
    if (( attempt + 1 < retries )); then sleep .005; fi
  done
  return 1
}

# Keep the two allocation channels connected for the owning shell's lifetime.
# Connect the owning session's persistent request/reply descriptors 8 and 9.
# Takes no arguments; uses the captured native owner PID. Return zero after
# connection or when already connected; return nonzero on connection failure.
_git_preload_connect_allocator() {
  if [[ ${_GIT_PRELOAD_ALLOC_CONNECTED:-} == 1 ]]; then return 0; fi
  local request reply
  printf -v request '%s%s' '\\.\pipe\git-preload-allocate-req-' "$_GIT_PRELOAD_OWNER_PID"
  printf -v reply '%s%s' '\\.\pipe\git-preload-allocate-reply-' "$_GIT_PRELOAD_OWNER_PID"
  if ! _git_preload_open_pipe 8 "$request" 0 400 ||
     ! _git_preload_open_pipe 9 "$reply" 0 400; then
    exec 8>&- 9>&-
    return 1
  fi
  _GIT_PRELOAD_ALLOC_CONNECTED=1
}
_git_preload_connect_allocator || {
  printf 'GIT-IPC ERROR: persistent allocator connection failed\n' >&2
  return 1
}

# Request a worker PID, then talk directly to its five worker channels.
# The Python manager only allocates IDs; it never forwards Git output.
# Run one serial Git command using the persistent allocator.
# Arguments: ordinary Git arguments, at most 31; each is encoded without evaluation.
# Write text stdout/stderr to the caller; stdin is an EOF-producing channel.
# Return the command status, or 255 for a transport/protocol failure.
git-ipc() {
  local fd_worker_stdin=3 fd_worker_stdout=4 fd_worker_stderr=5
  local fd_worker_exit=6 fd_worker_control=7
  if [[ ${_GIT_PRELOAD_ALLOC_CONNECTED:-} != 1 || $# -gt 31 ]]; then
    printf 'git-ipc requires a connected allocator and at most 31 arguments\n' >&2
    printf 'GIT-IPC ERROR: invalid invocation\n' >&2; return 255
  fi
  _GIT_PRELOAD_REQUEST_SEQ=$(( ${_GIT_PRELOAD_REQUEST_SEQ:-0} + 1 ))
  local marker=$(( _GIT_PRELOAD_REQUEST_SEQ % 10 ))
  local pid count header status
  if ! printf 'GET\n' >&8; then
    close_fhs
    printf 'GIT-IPC ERROR: allocator GET write failed\n' >&2; return 255
  fi
  IFS= read -r pid <&9 || {
    close_fhs
    printf 'GIT-IPC ERROR: allocator ID read failed\n' >&2; return 255
  }
  if [[ $pid == WAIT ]]; then
    printf 'Git worker pool temporarily depleted\n' >&2
    printf 'GIT-IPC ERROR: no worker ready\n' >&2; return 255
  fi
  if [[ ! $pid =~ ^[1-9][0-9]*$ ]]; then
    printf 'Invalid Git worker PID response: %s\n' "$pid" >&2
    printf 'GIT-IPC ERROR: invalid worker PID response\n' >&2; return 255
  fi

  local stem; printf -v stem '%s%s' '\\.\pipe\git-preload-' "$pid"
  local pstdin="${stem}-stdin"
  local pout="${stem}-stdout"
  local perr="${stem}-stderr"
  local pexit="${stem}-exit-code"
  local pcontrol="${stem}-param-env-cwd"

  # Open in the same order that the native worker accepts connections.
  _git_preload_open_pipe "$fd_worker_stdout" "$pout" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: stdout pipe open failed\n' >&2; return 255; }
  _git_preload_open_pipe "$fd_worker_stderr" "$perr" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: stderr pipe open failed\n' >&2; return 255; }
  _git_preload_open_pipe "$fd_worker_exit" "$pexit" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: exit-status pipe open failed\n' >&2; return 255; }
  _git_preload_open_pipe "$fd_worker_stdin" "$pstdin" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: stdin pipe open failed\n' >&2; return 255; }
  _git_preload_open_pipe "$fd_worker_control" "$pcontrol" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: control pipe open failed\n' >&2; return 255; }
  # No relay process: stdin is unavailable pending process-free handle
  # transfer. Close the worker channel after Windows has consumed the digit.
  # The worker supplies EOF from NUL. Keep this handle through output EOF
  # so closing it cannot discard the marker before the worker reads it.


  count=$(( $# + 1 ))
  printf -v header '\\x%02x\\x00\\x00\\x00' "$count"
  {
    printf '%b%s\0' "$header" git || { close_fhs; printf 'GIT-IPC ERROR: worker request write failed\n' >&2; return 255; }
    if (( $# )); then
      printf '%s\0' "$@" || { close_fhs; printf 'GIT-IPC ERROR: worker request write failed\n' >&2; return 255; }
    fi
    _git_preload_write_context windows || {
      close_fhs
      printf 'GIT-IPC ERROR: context frame write failed\n' >&2
      return 255
    }
    printf 'end\0' || {
      close_fhs
      printf 'GIT-IPC ERROR: request end tag write failed\n' >&2
      return 255
    }
  } >&"$fd_worker_control" || {
    close_fhs
    printf 'GIT-IPC ERROR: worker request write failed\n' >&2
    return 255
  }
  # Keep both writers open until output EOF: neither connection marker
  # nor request bytes may be discarded by a close before server consumption.

  # Collect text with alternating 100 ms reads, without waiting for a newline.
  # Git Bash misclassifies these pipes, so its reads wait for EOF instead;
  # stderr is delivered last and large unread stderr can block the command.
  # See Documentation/technical/preload.adoc for the client limits.
  # -d '' avoids waiting for newline; EOF marks the channel complete.
  # Timeouts >128 can still provide partial data, and do not close channels.
  local out_open=1 err_open=1 chunk read_rc
  while (( out_open || err_open )); do
    if (( out_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u "$fd_worker_stdout" chunk
      read_rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk"
      if (( read_rc == 1 )); then
        out_open=0
        close_fhs "$fd_worker_stdout"
      elif (( read_rc != 0 && read_rc <= 128 )); then
        close_fhs
        printf 'GIT-IPC ERROR: output pipe read failed\n' >&2; return 255
      fi
    fi
    if (( err_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u "$fd_worker_stderr" chunk
      read_rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk" >&2
      if (( read_rc == 1 )); then
        err_open=0
        close_fhs "$fd_worker_stderr"
      elif (( read_rc != 0 && read_rc <= 128 )); then
        close_fhs
        printf 'GIT-IPC ERROR: output pipe read failed\n' >&2; return 255
      fi
    fi
  done
  close_fhs "$fd_worker_stdin" "$fd_worker_control"
  IFS= read -r status <&"$fd_worker_exit" || { close_fhs; printf 'GIT-IPC ERROR: exit-status read failed\n' >&2; return 255; }
  close_fhs
  if [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
    return "${BASH_REMATCH[1]}"
  fi
  printf 'Invalid Git exit-code response: %s\n' "$status" >&2
  printf 'GIT-IPC ERROR: malformed exit status\n' >&2; return 255
}
alias git='git-ipc'
return 0
