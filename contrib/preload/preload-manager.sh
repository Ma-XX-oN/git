# Generic preload manager library for Bash (Git Bash, Linux, macOS).
#
# Usage:  source preload-manager.sh
#         preload git             # or: preload /path/to/git
#         git status              # served by a preloaded `git --preload-worker`
#
# Sourcing starts (or reuses) the session's manager and defines preload().
# preload <command> starts 3 workers of <command> --preload-worker and defines
# an exported function named after the command; `command <name>` still runs
# the executable directly. Set PRELOAD_PYTHON to the Python used for the
# manager (native Windows Python on Git Bash). Requires Bash 4.1 or newer.
# See benchmark-results/.../generalizing.md for the specification.

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  printf '%s must be sourced, not executed: source %s\n' "$0" "$0" >&2
  exit 2
fi
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 1) )); then
  printf 'preload-manager.sh requires Bash 4.1 or newer\n' >&2
  return 2
fi
case ${OSTYPE:-} in
  msys*|cygwin*) _PRELOAD_PLATFORM=windows ;;
  linux*|darwin*) _PRELOAD_PLATFORM=unix ;;
  *) printf 'preload: unsupported OSTYPE=%s\n' "${OSTYPE:-unset}" >&2; return 2 ;;
esac
# Exported with the helper functions so child Bash processes can use them.
export _PRELOAD_PLATFORM
_PRELOAD_LIB_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P) || return 1
declare -gA _PRELOAD_COMMANDS 2>/dev/null || declare -A _PRELOAD_COMMANDS

# Busy-wait without a child process; /dev/zero's NUL bytes are discarded, so
# read ends only at its timeout. Argument: seconds. Returns zero.
_preload_sleep() {
  local _
  IFS= read -r -N 1 -t "$1" _ < /dev/zero
  return 0
}

# Open a Windows named pipe read/write into a new descriptor.
# Arguments: variable name receiving the descriptor, pipe path, attempts.
# Returns zero on success, one after all attempts failed.
_preload_open() {
  local fd attempt
  for (( attempt = 0; attempt < $3; attempt++ )); do
    if { exec {fd}<>"$2"; } 2>/dev/null; then
      printf -v "$1" '%s' "$fd"
      return 0
    fi
    _preload_sleep .005
  done
  return 1
}

# Open a worker channel and send its one-byte call marker.
# Arguments: variable name, pipe path, marker digit, attempts.
# Returns zero on success, one on failure (the descriptor is closed).
_preload_open_marked() {
  _preload_open "$1" "$2" "$4" || return 1
  if ! printf '%s' "$3" >&"${!1}"; then
    eval "exec ${!1}>&-"
    return 1
  fi
}

# Write the cwd context field and end tag of a worker request.
# Returns zero on success, nonzero on write failure.
_preload_write_context() {
  printf 'cwd\0' || return 1
  if [[ $_PRELOAD_PLATFORM == windows ]]; then pwd -W; else pwd -P; fi || return 1
  printf '\0end\0'
}

# Linux/macOS: exchange one request and reply through a mailbox.
# Arguments: request command (GET, PRELOAD or STOP), then for GET/PRELOAD the
# command name, then for PRELOAD the executable path. The claimed mailbox
# number is inserted after the name: `GET git <n>`, `PRELOAD git <n> <path>`,
# `STOP <n>`.
# A mailbox is claimed by creating slot.<n>.lock with noclobber (an atomic
# exclusive create) holding $BASHPID. The client then blocks in read on the
# mailbox FIFO slot.<n>; the manager's reply wakes it immediately. FREE <n>
# releases the mailbox. If the manager dies, its ends of the mailbox close
# and read returns end-of-file instead of hanging.
# The request writer and a never-read request reader stay open for the
# session (reopened when PRELOAD_DIR changes, e.g. after a restart or in a
# child Bash that inherited only the variables). The idle reader guarantees
# the request FIFO always has a reader, so a write after the manager has
# exited cannot raise SIGPIPE and kill this shell.
# Sets _PRELOAD_REPLY. Returns zero once a reply has been read.
_preload_unix_exchange() {
  local slot i fd line rc noclobber=0
  _PRELOAD_REPLY=
  if [[ ${_PRELOAD_FD_DIR:-} != "$PRELOAD_DIR" ]]; then
    local ready
    IFS= read -r ready <"$PRELOAD_DIR/ready.json" 2>/dev/null || return 1
    [[ $ready =~ \"slots\":\ *([0-9]+) ]] || return 1
    _PRELOAD_SLOTS=${BASH_REMATCH[1]}
    [[ $ready =~ \"manager_pid\":\ *([0-9]+) ]] || return 1
    _PRELOAD_MANAGER_PID=${BASH_REMATCH[1]}
    # The manager holds a writer, so opening this reader does not block.
    exec {_PRELOAD_REQ_GUARD}<"$PRELOAD_DIR/request" || return 1
    exec {_PRELOAD_REQ_FD}>"$PRELOAD_DIR/request" || return 1
    _PRELOAD_FD_DIR=$PRELOAD_DIR
  fi
  # Claim a free mailbox, starting at a PID-derived index to avoid collisions.
  [[ $- == *C* ]] && noclobber=1
  set -C
  while :; do
    [[ -f $PRELOAD_DIR/ready.json ]] || { (( noclobber )) || set +C; return 1; }
    for (( i = 0; i < _PRELOAD_SLOTS; i++ )); do
      slot=$(( (BASHPID + i) % _PRELOAD_SLOTS ))
      { printf '%s' "$BASHPID" >"$PRELOAD_DIR/slot.$slot.lock"; } 2>/dev/null && break 2
    done
    _preload_sleep .001  # Every mailbox is busy; wait for a FREE.
  done
  (( noclobber )) || set +C
  # Opening a FIFO for reading blocks until it has a writer; the manager holds
  # the only one. A manager that crashed (leaving ready.json) would make the
  # open hang, so check it is alive first. Once open, its death gives EOF.
  if ! kill -0 "$_PRELOAD_MANAGER_PID" 2>/dev/null ||
     ! exec {fd}<"$PRELOAD_DIR/slot.$slot"; then
    printf 'FREE %s\n' "$slot" >&"$_PRELOAD_REQ_FD"
    return 1
  fi
  case $1 in
    STOP) line="STOP $slot" ;;
    *) line="$1 $2 $slot${3:+ $3}" ;;
  esac
  printf '%s\n' "$line" >&"$_PRELOAD_REQ_FD" && IFS= read -r _PRELOAD_REPLY <&"$fd"
  rc=$?
  exec {fd}<&-
  printf 'FREE %s\n' "$slot" >&"$_PRELOAD_REQ_FD"
  return "$rc"
}

# Send one request to the manager and store its reply line.
# Arguments: request command, then its words (GET <name>, PRELOAD <name>
# <path>, or STOP). Sets _PRELOAD_REPLY. Returns zero if a reply arrived.
_preload_request() {
  local req
  _PRELOAD_REPLY=
  if [[ $_PRELOAD_PLATFORM == windows ]]; then
    _preload_open req "$PRELOAD_WIN_DIR\\request" 40 || return 1
    printf '%s\n' "$*" >&"$req" && IFS= read -r _PRELOAD_REPLY <&"$req"
    local rc=$?
    exec {req}>&-
    return "$rc"
  fi
  _preload_unix_exchange "$@"
}

# Template for generated Windows command functions; __PRELOAD_NAME__ is
# replaced by the command name. Arguments: the command's arguments (at most
# 31). Returns the command's exit status, or 255 for a preload failure.
_preload_template_windows() {
  local req wdir rc marker count header chunk status out_open=1 err_open=1
  local fd_in fd_out fd_err fd_exit fd_ctl
  if (( $# > 31 )); then
    printf '__PRELOAD_NAME__: preload supports at most 31 arguments\n' >&2
    return 255
  fi
  if ! _preload_open req "$PRELOAD_WIN_DIR\\request" 40; then
    printf 'PRELOAD ERROR: cannot connect to the preload manager\n' >&2
    return 255
  fi
  printf 'GET __PRELOAD_NAME__\n' >&"$req" && IFS= read -r wdir <&"$req"
  rc=$?
  exec {req}>&-
  if (( rc )) || [[ $wdir != *'\worker-'* ]]; then
    printf 'PRELOAD ERROR: %s\n' "${wdir:-no reply from the preload manager}" >&2
    return 255
  fi
  marker=$(( _PRELOAD_SEQ = (${_PRELOAD_SEQ:-0} + 1) % 10 ))
  # Open in the order the worker accepts connections.
  if ! _preload_open_marked fd_out "$wdir\\stdout" "$marker" 40 ||
     ! _preload_open_marked fd_err "$wdir\\stderr" "$marker" 40 ||
     ! _preload_open_marked fd_exit "$wdir\\exit-code" "$marker" 40 ||
     ! _preload_open_marked fd_in "$wdir\\stdin" "$marker" 40 ||
     ! _preload_open_marked fd_ctl "$wdir\\params-cwd" "$marker" 40; then
    for rc in $fd_out $fd_err $fd_exit $fd_in $fd_ctl; do eval "exec $rc>&-"; done
    printf 'PRELOAD ERROR: cannot connect to worker %s\n' "$wdir" >&2
    return 255
  fi
  count=$(( $# + 1 ))
  printf -v header '\\x%02x\\x00\\x00\\x00' "$count"
  if ! { printf '%b%s\0' "$header" '__PRELOAD_NAME__' &&
         { (( $# == 0 )) || printf '%s\0' "$@"; } &&
         _preload_write_context; } >&"$fd_ctl"; then
    exec {fd_out}<&- {fd_err}<&- {fd_exit}<&- {fd_in}>&- {fd_ctl}>&-
    printf 'PRELOAD ERROR: worker request write failed\n' >&2
    return 255
  fi
  # Keep stdin and params-cwd open until output EOF so closing them cannot
  # discard a marker before the worker reads it. Alternate between stdout
  # and stderr so neither stream blocks the other.
  while (( out_open || err_open )); do
    if (( out_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u "$fd_out" chunk
      rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk"
      if (( rc == 1 )); then out_open=0; exec {fd_out}<&-
      elif (( rc != 0 && rc <= 128 )); then
        exec {fd_out}<&- {fd_err}<&- {fd_exit}<&- {fd_in}>&- {fd_ctl}>&-
        printf 'PRELOAD ERROR: stdout read failed\n' >&2; return 255
      fi
    fi
    if (( err_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u "$fd_err" chunk
      rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk" >&2
      if (( rc == 1 )); then err_open=0; exec {fd_err}<&-
      elif (( rc != 0 && rc <= 128 )); then
        exec {fd_out}<&- {fd_err}<&- {fd_exit}<&- {fd_in}>&- {fd_ctl}>&-
        printf 'PRELOAD ERROR: stderr read failed\n' >&2; return 255
      fi
    fi
  done
  exec {fd_in}>&- {fd_ctl}>&-
  IFS= read -r status <&"$fd_exit"
  rc=$?
  exec {fd_exit}<&-
  if (( rc == 0 )) && [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
    return "${BASH_REMATCH[1]}"
  fi
  printf 'PRELOAD ERROR: invalid worker exit status: %s\n' "$status" >&2
  return 255
}

# Template for generated Linux/macOS command functions; see the Windows one.
_preload_template_unix() {
  local wdir rc count header chunk status out_open=1 err_open=1
  local fd_in fd_out fd_err fd_exit fd_ctl
  if (( $# > 31 )); then
    printf '__PRELOAD_NAME__: preload supports at most 31 arguments\n' >&2
    return 255
  fi
  _preload_unix_exchange GET __PRELOAD_NAME__
  wdir=$_PRELOAD_REPLY
  if [[ $wdir != "$PRELOAD_DIR/worker-"* ]]; then
    printf 'PRELOAD ERROR: %s\n' "${wdir:-the preload manager stopped}" >&2
    return 255
  fi
  if ! exec {fd_out}<"$wdir/stdout"; then
    printf 'PRELOAD ERROR: stdout open failed\n' >&2; return 255
  fi
  exec {fd_err}<"$wdir/stderr" {fd_exit}<"$wdir/exit-code" \
       {fd_in}>"$wdir/stdin" {fd_ctl}>"$wdir/params-cwd" || {
    exec {fd_out}<&-
    printf 'PRELOAD ERROR: worker channel open failed\n' >&2; return 255
  }
  # stdin is connected but unused until a multitee builtin exists.
  exec {fd_in}>&-
  count=$(( $# + 1 ))
  printf -v header '\\x%02x\\x00\\x00\\x00' "$count"
  if ! { printf '%b%s\0' "$header" '__PRELOAD_NAME__' &&
         { (( $# == 0 )) || printf '%s\0' "$@"; } &&
         _preload_write_context; } >&"$fd_ctl"; then
    exec {fd_out}<&- {fd_err}<&- {fd_exit}<&- {fd_ctl}>&-
    printf 'PRELOAD ERROR: worker request write failed\n' >&2
    return 255
  fi
  while (( out_open || err_open )); do
    if (( out_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u "$fd_out" chunk
      rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk"
      if (( rc == 1 )); then out_open=0; exec {fd_out}<&-
      elif (( rc != 0 && rc <= 128 )); then
        exec {fd_out}<&- {fd_err}<&- {fd_exit}<&- {fd_ctl}>&-
        printf 'PRELOAD ERROR: stdout read failed\n' >&2; return 255
      fi
    fi
    if (( err_open )); then
      chunk=
      IFS= read -r -d '' -t 0.100 -u "$fd_err" chunk
      rc=$?
      [[ -z $chunk ]] || printf '%s' "$chunk" >&2
      if (( rc == 1 )); then err_open=0; exec {fd_err}<&-
      elif (( rc != 0 && rc <= 128 )); then
        exec {fd_out}<&- {fd_err}<&- {fd_exit}<&- {fd_ctl}>&-
        printf 'PRELOAD ERROR: stderr read failed\n' >&2; return 255
      fi
    fi
  done
  exec {fd_ctl}>&-
  IFS= read -r status <&"$fd_exit"
  rc=$?
  exec {fd_exit}<&-
  if (( rc == 0 )) && [[ $status =~ ^EXIT[[:space:]]([0-9]+)$ ]]; then
    return "${BASH_REMATCH[1]}"
  fi
  printf 'PRELOAD ERROR: invalid worker exit status: %s\n' "$status" >&2
  return 255
}

# Define and export the command function from the platform template.
# Argument: validated command name.
_preload_define() {
  local body
  body=$(declare -f "_preload_template_$_PRELOAD_PLATFORM")
  body=${body#*$'\n'}
  eval "$1() ${body//__PRELOAD_NAME__/$1}"
  export -f "$1"
}

# Preload an executable that supports --preload-worker.
# Argument: command name (looked up on PATH) or path.
# Returns zero after its workers are READY and the function is defined.
preload() {
  if (( $# != 1 )); then
    printf 'usage: preload <command|path>\n' >&2
    return 2
  fi
  local path name kind native
  if [[ $_PRELOAD_PLATFORM == windows && ( $1 == *\\* || $1 == [A-Za-z]:* ) ]]; then
    path=$(cygpath -u -- "$1") || return 1  # Native Windows path.
  elif [[ $1 == */* ]]; then
    path=$1
  else
    path=$(type -P -- "$1") || path=
  fi
  if [[ -z $path || ! -f $path || ! -x $path ]]; then
    printf 'preload: %s: executable not found\n' "$1" >&2
    return 1
  fi
  [[ $path == /* ]] || path=$PWD/$path
  name=${path##*/}
  [[ $_PRELOAD_PLATFORM == unix ]] || name=${name%.[eE][xX][eE]}
  kind=$(type -t -- "$name")
  if [[ ! $name =~ ^[A-Za-z_.][A-Za-z0-9_.-]*$ || $kind == builtin || $kind == keyword ]]; then
    printf 'preload: %s: not usable as a function name\n' "$name" >&2
    return 1
  fi
  if [[ $_PRELOAD_PLATFORM == windows ]]; then
    native=$(cygpath -w -- "$path") || return 1
  else
    native=$path
  fi
  _preload_request PRELOAD "$name" "$native"
  if [[ $_PRELOAD_REPLY != OK ]]; then
    printf 'preload: %s\n' "${_PRELOAD_REPLY#ERROR }" >&2
    return 1
  fi
  _preload_define "$name"
  _PRELOAD_COMMANDS[$name]=$path
}

# Stop this shell's manager and wait up to 10 seconds for it to exit.
# Returns zero once it has exited, one if it is still running.
_preload_stop() {
  local i
  if [[ -n ${_PRELOAD_MANAGER_JOB:-} ]] && kill -0 "$_PRELOAD_MANAGER_JOB" 2>/dev/null; then
    _preload_request STOP
    for (( i = 0; i < 400; i++ )); do
      kill -0 "$_PRELOAD_MANAGER_JOB" 2>/dev/null || break
      _preload_sleep .025
    done
    if kill -0 "$_PRELOAD_MANAGER_JOB" 2>/dev/null; then
      printf 'preload: manager %s did not stop\n' "$_PRELOAD_MANAGER_JOB" >&2
      return 1
    fi
  fi
  unset _PRELOAD_MANAGER_JOB
  return 0
}

# Start this shell's session, or reuse a live one inherited from a parent.
# Returns zero once the manager is READY.
_preload_start_session() {
  local owner base dir py i script
  if [[ $_PRELOAD_PLATFORM == windows ]]; then
    IFS= read -r owner <"/proc/$BASHPID/winpid" || return 1
  else
    owner=$BASHPID
  fi
  # A separate child process (bash -c, a script) inherits PRELOAD_* but not
  # the unexported _PRELOAD_MANAGER_JOB: reuse its parent's live session. A
  # subshell sourcing the library has both and gets a session of its own.
  if [[ -n ${PRELOAD_PID:-} && $PRELOAD_PID != "$owner" &&
        -z ${_PRELOAD_MANAGER_JOB:-} && -f ${PRELOAD_DIR:-/nonexistent}/ready.json ]]; then
    return 0
  fi
  if [[ ${PRELOAD_PID:-} != "$owner" ]]; then
    unset _PRELOAD_MANAGER_JOB  # A subshell must not stop its parent's manager.
  fi
  if [[ ${PRELOAD_PID:-} == "$owner" ]]; then
    _preload_stop || return 1  # Re-sourcing restarts with the current environment.
  fi
  # Descriptors cached by _preload_unix_exchange belong to the old session.
  if [[ -n ${_PRELOAD_FD_DIR:-} ]]; then
    exec {_PRELOAD_REQ_FD}>&- {_PRELOAD_REQ_GUARD}<&-
    unset _PRELOAD_FD_DIR
  fi
  case $OSTYPE in
    linux*) base=${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}} ;;
    *) base=${TMPDIR:-/tmp} ;;
  esac
  dir=${base%/}/preload-$owner
  if [[ -e $dir || -L $dir ]]; then
    if [[ -L $dir || ! -d $dir || ! -O $dir ]]; then
      printf 'preload: %s exists and is not owned by you\n' "$dir" >&2
      return 1
    fi
    rm -rf -- "$dir" || return 1
  fi
  # On Windows the per-user temp directory's ACL already makes this private,
  # and MSYS cannot apply POSIX modes there.
  if [[ $_PRELOAD_PLATFORM == windows ]]; then
    mkdir -- "$dir" || return 1
  else
    mkdir -m 700 -- "$dir" || return 1
  fi
  export PRELOAD_PID=$owner PRELOAD_DIR=$dir
  if [[ $_PRELOAD_PLATFORM == windows ]]; then
    export PRELOAD_WIN_DIR="\\\\.\\pipe\\preload-$owner"
    py=${PRELOAD_PYTHON:-python}
    script=$(cygpath -w -- "$_PRELOAD_LIB_DIR/preload_manager_windows.py") || return 1
    "$py" "$script" --owner-pid "$owner" --directory "$(cygpath -w -- "$dir")" \
      --pipe-dir "$PRELOAD_WIN_DIR" >"$dir/manager.log" 2>&1 </dev/null &
  else
    unset PRELOAD_WIN_DIR
    py=${PRELOAD_PYTHON:-python3}
    "$py" "$_PRELOAD_LIB_DIR/preload_manager_unix.py" --owner-pid "$owner" \
      --directory "$dir" >"$dir/manager.log" 2>&1 </dev/null &
  fi
  _PRELOAD_MANAGER_JOB=$!
  # Not a job of this shell: a bare `wait` (e.g. after `git a & git b &`)
  # must not wait for the manager.
  disown "$_PRELOAD_MANAGER_JOB"
  for (( i = 0; i < 400; i++ )); do
    [[ -f $dir/ready.json ]] && return 0
    if ! kill -0 "$_PRELOAD_MANAGER_JOB" 2>/dev/null; then
      printf 'preload: manager failed to start; see %s\n' "$dir/manager.log" >&2
      return 1
    fi
    _preload_sleep .025
  done
  printf 'preload: manager did not become ready; see %s\n' "$dir/manager.log" >&2
  return 1
}

export -f _preload_sleep _preload_open _preload_open_marked _preload_write_context \
  _preload_request _preload_unix_exchange
if ! _preload_start_session; then
  return 1
fi
# Restarting a session (re-sourcing) re-registers this shell's commands.
for _preload_name in "${!_PRELOAD_COMMANDS[@]}"; do
  preload "${_PRELOAD_COMMANDS[$_preload_name]}" || unset "_PRELOAD_COMMANDS[$_preload_name]"
done
unset _preload_name
return 0
