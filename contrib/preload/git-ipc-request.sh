# Shared Bash request extension for both --preload-worker transports.
# All text fields are UTF-8; environment uses NUL-terminated Bash declarations.

# Cache native shadows of the standard MSYS path variables in the owner shell.
# Takes no arguments. Export __git_env_NAME for the worker, retaining the raw
# value in unexported __git_env_source_NAME. Unchanged values use only builtins;
# changed nonempty values use this shell installation's cygpath. Return zero
# after refreshing all shadows, or nonzero on conversion/assignment failure.
_git_preload_refresh_paths() {
  local __git_env_name __git_env_source_key __git_env_target_key
  local __git_env_value __git_env_mode
  for __git_env_name in PATH HOME LD_LIBRARY_PATH ORIGINAL_PATH SHELL TMPDIR TMP TEMP; do
    __git_env_source_key=__git_env_source_$__git_env_name
    __git_env_target_key=__git_env_$__git_env_name
    if [[ ! ${!__git_env_name+x} ]]; then
      unset "$__git_env_source_key" "$__git_env_target_key" || return 1
      continue
    fi
    if [[ ${!__git_env_source_key+x} &&
          ${!__git_env_source_key} == "${!__git_env_name}" ]]; then
      continue
    fi
    __git_env_value=${!__git_env_name}
    case $__git_env_name in
      PATH|LD_LIBRARY_PATH|ORIGINAL_PATH) __git_env_mode=-wp ;;
      *) __git_env_mode=-w ;;
    esac
    if [[ -n $__git_env_value ]]; then
      # The sentinel preserves trailing newlines belonging to the value.
      __git_env_value=$(/usr/bin/cygpath "$__git_env_mode" -- "$__git_env_value" && printf .) || return 1
      __git_env_value=${__git_env_value%.}
      __git_env_value=${__git_env_value%$'\n'}
    fi
    printf -v "$__git_env_target_key" '%s' "$__git_env_value" || return 1
    printf -v "$__git_env_source_key" '%s' "${!__git_env_name}" || return 1
    export "$__git_env_target_key" || return 1
    export -n "$__git_env_source_key" || return 1
  done
}

# Emit cwd and the complete ordinary exported-variable snapshot on stdout.
# Argument 1: windows or unix, selecting only the native pwd spelling.
# Return zero on success, one if a builtin cannot emit its field.
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
