# Native Windows transport variant of issue 809.
# Reuses worker allocator and shared Git frame; no per-command child process.
source "$(dirname "${BASH_SOURCE[0]}")/git-manager.sh" "$@" || return $?
[[ -n ${GIT809_NATIVE_BUILTIN_DLL:-} && -f $GIT809_NATIVE_BUILTIN_DLL ]] || {
  printf 'GIT-IPC ERROR: native builtin DLL not configured\n' >&2
  return 255
}
export _GIT809_OWNER_WINPID
enable -f "$GIT809_NATIVE_BUILTIN_DLL" git809pipe || return 255
git-ipc() {
  git809pipe "$@"
}
alias git='git-ipc'
return 0
