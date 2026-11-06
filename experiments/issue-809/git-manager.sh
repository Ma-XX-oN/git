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
  IFS= read -r owner_winpid < "/proc/$/winpid" || return 1
  if [[ ! $owner_winpid =~ ^[1-9][0-9]*$ ]]; then
    printf 'cannot resolve owning shell native Windows PID\n' >&2
    return 1
  fi
  local script_path script_dir temp native_ready
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 1
  script_path=$(cygpath -w "$script_dir/windows_shell_owned_pool.py") || return 1
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
return "$_git809_source_rc"
