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
      _GIT809_MANAGER_LOG=$temp/manager.log
      local data
      IFS= read -r data < "$temp/ready.json" || return 1
      if [[ $data =~ \"pipe_token\"[[:space:]]*:[[:space:]]*\"([a-f0-9]+)\" ]]; then
        _GIT809_MANAGER_TOKEN=${BASH_REMATCH[1]}
      else
        printf 'manager READY missing pipe token\n' >&2
        return 1
      fi
      _GIT809_NEXT_SEQUENCE=0
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

# Bash builtins in each Git IPC command (no per-command native executable).
git_ipc() {
  if [[ -z ${_GIT809_MANAGER_TOKEN:-} || $# -gt 31 ]]; then
    printf 'git_ipc requires at most 31 Git arguments\n' >&2
    return 2
  fi
  local index=$_GIT809_NEXT_SEQUENCE
  local base="\\\\.\\pipe\\git809-real"
  local request="${base}-request-${_GIT809_MANAGER_TOKEN}-${index}"
  local pout="${base}-stdout-${_GIT809_MANAGER_TOKEN}-${index}"
  local perr="${base}-stderr-${_GIT809_MANAGER_TOKEN}-${index}"
  local pstatus="${base}-status-${_GIT809_MANAGER_TOKEN}-${index}"
  local i line status
  _GIT809_NEXT_SEQUENCE=$((index+1))
  for ((i=0;i<200;i++)); do
    if { exec 3>>"$request"; } 2>/dev/null; then break; fi
    sleep .005
  done
  if (( i == 200 )); then
    printf 'Git IPC request endpoint unavailable\n' >&2
    return 70
  fi
  local count=$(( $# + 1 )) header arg
  printf -v header '\\x%02x\\x00\\x00\\x00' "$count"
  printf '%b' "$header" >&3 || return 71
  printf 'git\x00' >&3 || return 71
  for arg in "$@"; do
    printf '%s\x00' "$arg" >&3 || return 71
  done
  exec 3>&-
  exec 4<"$pout" || return 72
  while IFS= read -r line <&4 || [[ -n $line ]]; do
    printf '%s\n' "$line"
  done
  exec 4<&-
  exec 5<"$perr" || return 73
  while IFS= read -r line <&5 || [[ -n $line ]]; do
    printf '%s\n' "$line" >&2
  done
  exec 5<&-
  exec 6<"$pstatus" || return 74
  IFS= read -r status <&6 || return 75
  exec 6<&-
  if [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
    return "${BASH_REMATCH[1]}"
  fi
  printf 'invalid Git IPC status: %s\n' "$status" >&2
  return 76
}
alias git='git_ipc'
return 0
