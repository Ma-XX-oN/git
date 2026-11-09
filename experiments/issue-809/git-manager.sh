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
# IPC client and manager launcher for the native Git --preload worker.
# Requires GIT809_WORKER_GIT (native Git executable with --preload support)
# and GIT809_MANAGER_PYTHON (native Windows Python 3).
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

_git809_source_manager() {
  local requested
  requested=$1
  [[ -n $requested ]] || requested=3
  if [[ ! $requested =~ ^[1-9][0-9]*$ ]] ||
     (( ${#requested} > 2 || requested > 32 )); then
    printf 'worker count must be an integer from 1 to 32\n' >&2
    return 2
  fi
  if [[ -n ${_GIT809_MANAGER_BASH_PID:-} ]] &&
     kill -0 "$_GIT809_MANAGER_BASH_PID" 2>/dev/null &&
     [[ -f ${_GIT809_MANAGER_READY:-} ]]; then
    # No resize control protocol in this iteration. Sourcing is idempotent.
    if [[ $requested != "${_GIT809_MANAGER_WORKERS:-}" ]]; then
      printf 'manager already running with %s workers; resize is not implemented\n' \
        "$_GIT809_MANAGER_WORKERS" >&2
    fi
    return 0
  fi
  if [[ -n ${_GIT809_MANAGER_BASH_PID:-} ]] &&
     kill -0 "$_GIT809_MANAGER_BASH_PID" 2>/dev/null; then
    printf 'existing manager is alive but not READY; refusing duplicate\n' >&2
    return 1
  fi
  if [[ -z ${GIT809_WORKER_GIT:-} || -z ${GIT809_MANAGER_PYTHON:-} ]]; then
    printf 'set GIT809_WORKER_GIT and GIT809_MANAGER_PYTHON before sourcing\n' >&2
    return 2
  fi
  local owner_winpid
  IFS= read -r owner_winpid < "/proc/$BASHPID/winpid" || return 1
  if [[ ! $owner_winpid =~ ^[1-9][0-9]*$ ]]; then
    printf 'cannot resolve owning shell native Windows PID\n' >&2
    return 1
  fi
  local script_path script_dir temp native_ready
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 1
  script_path=$(cygpath -w "$script_dir/windows_git_ipc_pool.py") || return 1
  temp=$(mktemp -d) || return 1
  native_ready=$(cygpath -w "$temp/ready.json") || return 1
  "$GIT809_MANAGER_PYTHON" "$script_path" --git "$GIT809_WORKER_GIT" \
    --workers "$requested" --owner-winpid "$owner_winpid" --ready-file "$native_ready" \
    >"$temp/manager.log" 2>&1 </dev/null &
  local launched=$!
  local i
  for ((i=0;i<200;i++)); do
    if [[ -f $temp/ready.json ]]; then
      _GIT809_MANAGER_BASH_PID=$launched
      _GIT809_MANAGER_READY=$temp/ready.json
      _GIT809_MANAGER_WORKERS=$requested
      _GIT809_OWNER_WINPID=$owner_winpid
      _GIT809_MANAGER_LOG=$temp/manager.log
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
_git809_source_manager "$@"
_git809_source_rc=$?
unset -f _git809_source_manager
if (( _git809_source_rc != 0 )); then
  return "$_git809_source_rc"
fi

# Close selected descriptor numbers, or all reserved IPC descriptors in the
# current git-ipc call when no arguments are supplied. Repeated closes are safe.
close_fhs() {
  local fd
  if (( $# == 0 )); then
    # The allocator owns FDs 8 and 9 until the shell exits.
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
_git809_open_pipe() {
  local fd=$1 path=$2 marker=$3 retries=${4:-1} attempt
  [[ $fd =~ ^[3-9]$ && $marker =~ ^[0-9]$ &&
     $retries =~ ^[1-9][0-9]*$ ]] || return 2
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
      close_fhs "$fd"
      return 1
    fi
    if (( attempt + 1 < retries )); then sleep .005; fi
  done
  return 1
}

# Establish the allocator request/reply pair once for the owning shell.
_git809_connect_allocator() {
  if [[ ${_GIT809_ALLOC_CONNECTED:-} == 1 ]]; then return 0; fi
  local request reply
  printf -v request '%s%s' '\\.\pipe\git809-allocate-req-' "$_GIT809_OWNER_WINPID"
  printf -v reply '%s%s' '\\.\pipe\git809-allocate-reply-' "$_GIT809_OWNER_WINPID"
  _git809_open_pipe 8 "$request" 0 400 || return 1
  _git809_open_pipe 9 "$reply" 0 400 || return 1
  _GIT809_ALLOC_CONNECTED=1
}
_git809_connect_allocator || {
  printf 'GIT-IPC ERROR: persistent allocation connection failed\n' >&2
  return 1
}

# Request a worker PID, then talk directly to its five worker channels.
# The Python manager only allocates IDs; it never forwards Git output.
git-ipc() {
  local fd_allocate_request=8 fd_allocate_reply=9
  local fd_worker_stdin=3 fd_worker_stdout=4 fd_worker_stderr=5
  local fd_worker_exit=6 fd_worker_control=7
  if [[ ${_GIT809_ALLOC_CONNECTED:-} != 1 || $# -gt 31 ]]; then
    printf 'git-ipc requires a connected manager and at most 31 arguments\n' >&2
    printf 'GIT-IPC ERROR: invalid invocation\n' >&2; return 255
  fi
  [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE start bashpid=%s\n' "$BASHPID" >>"$_GIT809_MANAGER_LOG"
  _GIT809_REQUEST_SEQ=$(( ${_GIT809_REQUEST_SEQ:-0} + 1 ))
  local marker=$(( _GIT809_REQUEST_SEQ % 10 ))
  local pid i count header arg status line
  local _git809_t_start _git809_t_alloc _git809_t_connect _git809_t_send _git809_t_drained
  if [[ -n ${GIT809_TIMING_LOG:-} ]]; then _git809_t_start=$EPOCHREALTIME; fi
  if ! printf 'GET\n' >&"$fd_allocate_request"; then
    close_fhs
    printf 'GIT-IPC ERROR: persistent allocation request failed\n' >&2; return 255
  fi
  IFS= read -r pid <&"$fd_allocate_reply" || {
    close_fhs
    printf 'GIT-IPC ERROR: persistent allocation response failed\n' >&2
    return 255
  }
  [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE allocated pid=%s bashpid=%s\n' "$pid" "$BASHPID" >>"$_GIT809_MANAGER_LOG"
  if [[ $pid == WAIT ]]; then
    printf 'Git worker pool temporarily depleted\n' >&2
    printf 'GIT-IPC ERROR: no worker ready\n' >&2; return 255
  fi
  if [[ ! $pid =~ ^[1-9][0-9]*$ ]]; then
    printf 'Invalid Git worker PID response: %s\n' "$pid" >&2
    printf 'GIT-IPC ERROR: invalid worker PID response\n' >&2; return 255
  fi

  local stem; printf -v stem '%s%s' '\\.\pipe\' "$pid"
  if [[ -n ${GIT809_TIMING_LOG:-} ]]; then _git809_t_alloc=$EPOCHREALTIME; fi
  local pstdin="${stem}-stdin"
  local pout="${stem}-stdout"
  local perr="${stem}-stderr"
  local pexit="${stem}-exit-code"
  local pcontrol="${stem}-param-env-cwd"

  # Open in the same order that the native worker accepts connections.
  [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE open stdout pid=%s\n' "$pid" >>"$_GIT809_MANAGER_LOG"
  _git809_open_pipe "$fd_worker_stdout" "$pout" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: stdout pipe open failed\n' >&2; return 255; }
  [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE open stderr pid=%s\n' "$pid" >>"$_GIT809_MANAGER_LOG"
  _git809_open_pipe "$fd_worker_stderr" "$perr" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: stderr pipe open failed\n' >&2; return 255; }
  [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE open exit pid=%s\n' "$pid" >>"$_GIT809_MANAGER_LOG"
  _git809_open_pipe "$fd_worker_exit" "$pexit" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: exit-status pipe open failed\n' >&2; return 255; }
  [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE open stdin pid=%s\n' "$pid" >>"$_GIT809_MANAGER_LOG"
  _git809_open_pipe "$fd_worker_stdin" "$pstdin" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: stdin pipe open failed\n' >&2; return 255; }
  _git809_open_pipe "$fd_worker_control" "$pcontrol" "$marker" 40 || { close_fhs; printf 'GIT-IPC ERROR: control pipe open failed\n' >&2; return 255; }
  if [[ -n ${GIT809_TIMING_LOG:-} ]]; then _git809_t_connect=$EPOCHREALTIME; fi
  # No relay process: stdin is unavailable pending process-free handle
  # transfer. Close the worker channel after Windows has consumed the digit.
  # The worker supplies EOF from NUL. Keep this handle through output EOF
  # so closing it cannot discard the marker before the worker reads it.


  count=$(( $# + 1 ))
  printf -v header '\\x%02x\\x00\\x00\\x00' "$count"
  printf '%b' "$header" >&"$fd_worker_control" || { close_fhs; printf 'GIT-IPC ERROR: worker request write failed\n' >&2; return 255; }
  printf 'git\x00' >&"$fd_worker_control" || { close_fhs; printf 'GIT-IPC ERROR: worker request write failed\n' >&2; return 255; }
  for arg in "$@"; do
    printf '%s\x00' "$arg" >&"$fd_worker_control" || { close_fhs; printf 'GIT-IPC ERROR: worker request write failed\n' >&2; return 255; }
  done
  _git809_write_context windows >&"$fd_worker_control" || {
    close_fhs
    printf 'GIT-IPC ERROR: context frame write failed\n' >&2
    return 255
  }
  printf 'end\0' >&"$fd_worker_control" || {
    close_fhs
    printf 'GIT-IPC ERROR: request end tag write failed\n' >&2
    return 255
  }
  # Keep both writers open until output EOF: neither connection marker
  # nor request bytes may be discarded by a close before server consumption.
  [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE sent stdin pid=%s\n' "$pid" >>"$_GIT809_MANAGER_LOG"

  if [[ -n ${GIT809_TIMING_LOG:-} ]]; then _git809_t_send=$EPOCHREALTIME; fi
  # Experimental second timing mode: stdout accumulates all available
  # text up to a 100 ms deadline without requiring a newline or byte count.
  # Bash read -d '' uses NUL as terminator; the currently supported
  # text-output contract excludes NUL bytes.
  # Both channels are timed-read directly so EOF is observable even when empty.
  if [[ ${GIT809_DRAIN_MODE:-} == chunk100 ]]; then
    local out_open=1 err_open=1 chunk read_rc stderr_chunk
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
        stderr_chunk=
        IFS= read -r -d '' -t 0.100 -u "$fd_worker_stderr" stderr_chunk
        read_rc=$?
        [[ -z $stderr_chunk ]] || printf '%s' "$stderr_chunk" >&2
        if (( read_rc == 1 )); then
          err_open=0
          close_fhs "$fd_worker_stderr"
        elif (( read_rc != 0 && read_rc <= 128 )); then
          close_fhs
          printf 'GIT-IPC ERROR: output pipe read failed\n' >&2; return 255
        fi
      fi
    done
    [[ ${GIT809_TRACE:-} == 1 ]] && printf 'TRACE drained data pid=%s\n' "$pid" >>"$_GIT809_MANAGER_LOG"
    if [[ -n ${GIT809_TIMING_LOG:-} ]]; then _git809_t_drained=$EPOCHREALTIME; fi
    close_fhs "$fd_worker_stdin" "$fd_worker_control"
    IFS= read -r status <&"$fd_worker_exit" || { close_fhs; printf 'GIT-IPC ERROR: exit-status read failed\n' >&2; return 255; }
    close_fhs
    if [[ -n ${GIT809_TIMING_LOG:-} ]]; then
      printf '%s,%s,%s,%s,%s,%s\n' "$_git809_t_start" "$_git809_t_alloc" "$_git809_t_connect" "$_git809_t_send" "$_git809_t_drained" "$EPOCHREALTIME" >>"$GIT809_TIMING_LOG"
    fi
    if [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
      return "${BASH_REMATCH[1]}"
    fi
    printf 'Invalid Git exit-code response: %s\n' "$status" >&2
    printf 'GIT-IPC ERROR: malformed exit status\n' >&2; return 255
  fi
  # Alternating timed reads (1 ms each), without readiness probes or subshells.
  # -d '' avoids waiting for newline; EOF marks the channel complete.
  # Timeouts >128 can still provide partial data, and do not close channels.
  local out_open=1 err_open=1 chunk read_rc
  while (( out_open || err_open )); do
    if (( out_open )); then
      chunk=
      IFS= read -r -d '' -t 0.001 -u "$fd_worker_stdout" chunk
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
      IFS= read -r -d '' -t 0.001 -u "$fd_worker_stderr" chunk
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
