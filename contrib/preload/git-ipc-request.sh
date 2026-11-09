# Shared Bash request extension for both --preload-worker transports.
# All text fields are UTF-8; environment is an exact byte-counted snapshot.
_git_preload_write_context() {
  local platform=$1
  if [[ $platform == windows ]]; then
    # Investigation-only switches; default keeps both features enabled.
    if [[ ${GIT_PRELOAD_ABLATION_CWD:-1} == 1 ]]; then
      printf 'cwd\0' || return 1
      pwd -W || return 1
      printf '\0' || return 1
    fi
    if [[ ${GIT_PRELOAD_ABLATION_ENV:-1} == 1 ]]; then
      printf 'env-export\0' || return 1
      export -p || return 1
      printf '\0' || return 1
    fi
    return 0
  fi
  # Unix and Windows share the validated native export-declaration decoder.
  # Both outputs are Bash builtins; neither opens an MSYS subprocess.
  printf 'cwd\0' || return 1
  pwd -P || return 1
  printf '\0env-export\0' || return 1
  export -p || return 1
  printf '\0' || return 1
}
