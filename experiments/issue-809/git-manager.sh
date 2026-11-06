# Source into Git Bash: source /path/to/git-manager.sh [worker-count]
# Experimental #809 lifecycle only; command routing/alias is future work.
# Requires GIT809_WORKER_GIT (source-built wmain-patched Git executable)
# and GIT809_MANAGER_PYTHON (native Windows Python 3).
_git809_source_manager() {
  local requested
  requested=$1
  [[ -n $requested ]] || requested=4
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

# Request a worker PID, then talk directly to its four pipe endpoints.
# The Python manager only allocates IDs; it never forwards Git output.
git-ipc() {
  if [[ -z ${_GIT809_OWNER_WINPID:-} || $# -gt 31 ]]; then
    printf 'git-ipc requires a running manager and at most 31 arguments\n' >&2
    return 2
  fi
  local allocator; printf -v allocator '%s%s' '\\.\pipe\git809-allocate-' "$_GIT809_OWNER_WINPID"
  local pid i count header arg status line
  for ((i=0;i<200;i++)); do
    if { exec 3<>"$allocator"; } 2>/dev/null; then break; fi
    sleep .005
  done
  if (( i == 200 )); then
    printf 'Git manager allocation pipe unavailable\n' >&2
    return 70
  fi
  printf 'GET\n' >&3 || { exec 3>&-; return 71; }
  IFS= read -r pid <&3 || { exec 3>&-; return 72; }
  exec 3>&-
  if [[ $pid == WAIT ]]; then
    printf 'Git worker pool temporarily depleted\n' >&2
    return 73
  fi
  if [[ ! $pid =~ ^[1-9][0-9]*$ ]]; then
    printf 'Invalid Git worker PID response: %s\n' "$pid" >&2
    return 74
  fi

  local stem; printf -v stem '%s%s' '\\.\pipe\' "$pid"
  local pstdin="${stem}-stdin"
  local pout="${stem}-stdout"
  local perr="${stem}-stderr"
  local pexit="${stem}-exit-code"

  # Open in the same order that the native worker accepts connections.
  exec 4<"$pout" || return 75
  exec 5<"$perr" || { exec 4<&-; return 76; }
  exec 6<"$pexit" || { exec 4<&- 5<&-; return 77; }
  exec 7>>"$pstdin" || { exec 4<&- 5<&- 6<&-; return 78; }

  count=$(( $# + 1 ))
  printf -v header '\\x%02x\\x00\\x00\\x00' "$count"
  printf '%b' "$header" >&7 || return 79
  printf 'git\x00' >&7 || return 79
  for arg in "$@"; do
    printf '%s\x00' "$arg" >&7 || return 79
  done
  exec 7>&-

  # Experimental second timing mode: stdout accumulates all available
  # text up to a 100 ms deadline without requiring a newline or byte count.
  # Bash read -d '' uses NUL as terminator; the currently supported
  # text-output contract excludes NUL bytes.
  # stderr uses a zero-timeout readiness check and line-delimited reads.
  if [[ ${GIT809_DRAIN_MODE:-} == chunk100 ]]; then
    local out_open=1 err_open=1 chunk read_rc stderr_line
    while (( out_open || err_open )); do
      if (( out_open )); then
        chunk=
        IFS= read -r -d '' -t 0.100 -u 4 chunk
        read_rc=$?
        [[ -z $chunk ]] || printf '%s' "$chunk"
        if (( read_rc == 1 )); then
          out_open=0
          exec 4<&-
        elif (( read_rc != 0 && read_rc <= 128 )); then
          exec 4<&- 5<&- 6<&-
          return 81
        fi
      fi
      if (( err_open )) && IFS= read -r -t 0 -u 5; then
        stderr_line=
        IFS= read -r -t 0.001 -u 5 stderr_line
        read_rc=$?
        if [[ -n $stderr_line ]]; then
          printf '%s' "$stderr_line" >&2
          if (( read_rc == 0 )); then printf '\n' >&2; fi
        fi
        if (( read_rc == 1 )); then
          err_open=0
          exec 5<&-
        elif (( read_rc != 0 && read_rc <= 128 )); then
          exec 4<&- 5<&- 6<&-
          return 81
        fi
      fi
    done
    IFS= read -r status <&6 || { exec 6<&-; return 80; }
    exec 6<&-
    if [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
      return "${BASH_REMATCH[1]}"
    fi
    printf 'Invalid Git exit-code response: %s\n' "$status" >&2
    return 82
  fi
  # Poll stdout and stderr in one Bash process; read exit status after both close.
  # -t 0 checks readiness only. -N reads chunks without waiting for newline;
  # a fractional timeout bounds a partial read when less than 4096 bytes arrive.
  # Bash returns >128 on timeout and still assigns any partial data to chunk.
  local out_open=1 err_open=1 chunk read_rc
  while (( out_open || err_open )); do
    if (( out_open )) && IFS= read -r -t 0 -u 4; then
      chunk=
      IFS= read -r -N 4096 -t 0.001 -u 4 chunk
      read_rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk"
      if (( read_rc == 1 )); then
        out_open=0
        exec 4<&-
      elif (( read_rc != 0 && read_rc <= 128 )); then
        exec 4<&- 5<&- 6<&-
        return 81
      fi
    fi
    if (( err_open )) && IFS= read -r -t 0 -u 5; then
      chunk=
      IFS= read -r -N 4096 -t 0.001 -u 5 chunk
      read_rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk" >&2
      if (( read_rc == 1 )); then
        err_open=0
        exec 5<&-
      elif (( read_rc != 0 && read_rc <= 128 )); then
        exec 4<&- 5<&- 6<&-
        return 81
      fi
    fi
  done
  IFS= read -r status <&6 || { exec 6<&-; return 80; }
  exec 6<&-
  if [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
    return "${BASH_REMATCH[1]}"
  fi
  printf 'Invalid Git exit-code response: %s\n' "$status" >&2
  return 82
}
alias git='git-ipc'
return 0
