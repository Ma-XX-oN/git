# Source in Bash on Linux/macOS after setting GIT809_WORKER_GIT.
# Experimental single-use preinitialized Git worker; text output only.
_git809_unix_source() {
  local requested=${1:-3}
  if [[ ! $requested =~ ^[1-9][0-9]*$ ]] || (( requested > 32 )); then
    printf 'GIT-IPC ERROR: invalid pool size\n' >&2
    return 255
  fi
  if [[ -n ${_GIT809_UNIX_MANAGER_PID:-} ]] &&
     kill -0 "$_GIT809_UNIX_MANAGER_PID" 2>/dev/null &&
     [[ -f ${_GIT809_UNIX_DIRECTORY:-}/ready.json ]]; then
    return 0
  fi
  [[ -n ${GIT809_WORKER_GIT:-} ]] || {
    printf 'GIT-IPC ERROR: GIT809_WORKER_GIT not set\n' >&2
    return 255
  }
  local script_dir directory py
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 255
  directory=$(mktemp -d) || return 255
  py=${GIT809_MANAGER_PYTHON:-python3}
  "$py" "$script_dir/unix_git_ipc_pool.py" --git "$GIT809_WORKER_GIT" \
    --workers "$requested" --directory "$directory" --owner-pid "$BASHPID" \
    >"$directory/manager.log" 2>&1 </dev/null &
  local launched=$! i
  for ((i=0;i<200;i++)); do
    if [[ -f $directory/ready.json ]]; then
      # One persistent reader per owning Bash shell. Reopening it for each
      # command can observe EOF from the prior reply writer before the
      # server finishes closing that connection.
      exec {_GIT809_UNIX_REPLY_FD}< "$directory/allocate-reply" || {
        printf 'GIT-IPC ERROR: Unix allocation reply channel open failed\n' >&2
        return 255
      }
      if [[ ${GIT809_PERSISTENT_REQ:-} == 1 ]]; then
        exec {_GIT809_UNIX_REQ_FD}> "$directory/allocate-req" || {
          printf 'GIT-IPC ERROR: persistent allocation request channel open failed\n' >&2
          return 255
        }
      fi
      _GIT809_UNIX_DIRECTORY=$directory
      _GIT809_UNIX_MANAGER_PID=$launched
      _GIT809_UNIX_MANAGER_LOG=$directory/manager.log
      return 0
    fi
    if ! kill -0 "$launched" 2>/dev/null; then
      printf 'GIT-IPC ERROR: Unix manager exited before READY\n' >&2
      cat "$directory/manager.log" >&2
      return 255
    fi
    sleep .025
  done
  printf 'GIT-IPC ERROR: Unix manager READY timeout\n' >&2
  return 255
}
_git809_unix_source "$@"
_git809_unix_source_rc=$?
unset -f _git809_unix_source
if (( _git809_unix_source_rc != 0 )); then
  return "$_git809_unix_source_rc"
fi

git-ipc() {
  if [[ -z ${_GIT809_UNIX_DIRECTORY:-} || $# -gt 31 ]]; then
    printf 'GIT-IPC ERROR: manager unavailable or too many arguments\n' >&2
    return 255
  fi
  local dir=$_GIT809_UNIX_DIRECTORY worker count header arg chunk rc status
  local profile_start profile_alloc profile_open profile_sent profile_drained profile_exit
  if [[ ${GIT809_PROFILE_PHASES:-} == 1 ]]; then profile_start=$EPOCHREALTIME; fi
  [[ -n ${_GIT809_UNIX_REPLY_FD:-} ]] || {
    printf 'GIT-IPC ERROR: Unix allocation reply channel unavailable\n' >&2
    return 255
  }
  if [[ ${GIT809_PERSISTENT_REQ:-} == 1 ]]; then
    [[ -n ${_GIT809_UNIX_REQ_FD:-} ]] || {
      printf 'GIT-IPC ERROR: persistent allocation descriptor missing\n' >&2
      return 255
    }
    printf 'GET\n' >&"$_GIT809_UNIX_REQ_FD" || {
      printf 'GIT-IPC ERROR: persistent allocation request failed\n' >&2
      return 255
    }
  else
    printf 'GET\n' > "$dir/allocate-req" || {
      printf 'GIT-IPC ERROR: allocation request failed\n' >&2
      return 255
    }
  fi
  IFS= read -r worker <&"$_GIT809_UNIX_REPLY_FD"
  rc=$?
  if (( rc != 0 )); then
    printf 'GIT-IPC ERROR: allocation response failed read_rc=%s partial=%q manager_pid=%s\n' "$rc" "$worker" "$_GIT809_UNIX_MANAGER_PID" >&2
    return 255
  fi
  if [[ $worker == WAIT || $worker == INVALID || ! -d $worker ]]; then
    printf 'GIT-IPC ERROR: unavailable or invalid Git worker\n' >&2
    return 255
  fi

  if [[ ${GIT809_PROFILE_PHASES:-} == 1 ]]; then profile_alloc=$EPOCHREALTIME; fi
  exec 4<"$worker/stdout" || { printf 'GIT-IPC ERROR: stdout open failed\n' >&2; return 255; }
  exec 5<"$worker/stderr" || { exec 4<&-; printf 'GIT-IPC ERROR: stderr open failed\n' >&2; return 255; }
  exec 6<"$worker/exit-code" || { exec 4<&- 5<&-; printf 'GIT-IPC ERROR: exit-code open failed\n' >&2; return 255; }
  exec 7>"$worker/stdin" || { exec 4<&- 5<&- 6<&-; printf 'GIT-IPC ERROR: stdin open failed\n' >&2; return 255; }

  if [[ ${GIT809_PROFILE_PHASES:-} == 1 ]]; then profile_open=$EPOCHREALTIME; fi
  count=$(( $# + 1 ))
  printf -v header '\\x%02x\\x00\\x00\\x00' "$count"
  printf '%b' "$header" >&7 || { printf 'GIT-IPC ERROR: request header write failed\n' >&2; return 255; }
  printf 'git\x00' >&7 || { printf 'GIT-IPC ERROR: command write failed\n' >&2; return 255; }
  for arg in "$@"; do
    printf '%s\x00' "$arg" >&7 || { printf 'GIT-IPC ERROR: argument write failed\n' >&2; return 255; }
  done
  exec 7>&-
  if [[ ${GIT809_PROFILE_PHASES:-} == 1 ]]; then profile_sent=$EPOCHREALTIME; fi

  local stdout_open=1 stderr_open=1
  while (( stdout_open || stderr_open )); do
    if (( stdout_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u 4 chunk
      rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk"
      if (( rc == 1 )); then
        stdout_open=0
        exec 4<&-
      elif (( rc != 0 && rc <= 128 )); then
        printf 'GIT-IPC ERROR: stdout read failed\n' >&2
        exec 4<&- 5<&- 6<&-
        return 255
      fi
    fi
    if (( stderr_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u 5 chunk
      rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk" >&2
      if (( rc == 1 )); then
        stderr_open=0
        exec 5<&-
      elif (( rc != 0 && rc <= 128 )); then
        printf 'GIT-IPC ERROR: stderr read failed\n' >&2
        exec 4<&- 5<&- 6<&-
        return 255
      fi
    fi
  done
  if [[ ${GIT809_PROFILE_PHASES:-} == 1 ]]; then profile_drained=$EPOCHREALTIME; fi
  IFS= read -r status <&6 || {
    exec 6<&-
    printf 'GIT-IPC ERROR: exit-code read failed\n' >&2
    return 255
  }
  exec 6<&-
  if [[ ${GIT809_PROFILE_PHASES:-} == 1 ]]; then
    profile_exit=$EPOCHREALTIME
    printf 'GIT809_PHASE mode=%s start=%s allocated=%s opened=%s sent=%s drained=%s exited=%s\n' \
      "${GIT809_PERSISTENT_REQ:-0}" "$profile_start" "$profile_alloc" "$profile_open" "$profile_sent" "$profile_drained" "$profile_exit" >&2
  fi
  if [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
    return "${BASH_REMATCH[1]}"
  fi
  printf 'GIT-IPC ERROR: invalid worker exit-code response\n' >&2
  return 255
}
alias git='git-ipc'
return 0
