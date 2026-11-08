# Shared Bash request extension for both --preload transports.
# All text fields are UTF-8; environment is an exact byte-counted snapshot.
_git809_write_context() {
  local platform=$1
  if [[ $platform == windows ]]; then
    # Investigation-only switches; default keeps both features enabled.
    if [[ ${GIT809_ABLATION_CWD:-1} == 1 ]]; then
      printf 'cwd-win\0' || return 1
      pwd -W || return 1
      printf '\0' || return 1
    fi
    if [[ ${GIT809_ABLATION_ENV:-1} == 1 ]]; then
      printf 'env-export\0' || return 1
      export -p || return 1
      printf '\0' || return 1
    fi
    return 0
  fi
  local cwd=$PWD
  local name i size header
  local -a names=() values=()

  if [[ $platform == windows ]]; then
    cwd=$(pwd -W) || return 1
  fi
  printf 'cwd\0%s\0' "$cwd" || return 1

  # Snapshot exports BEFORE changing the locale for byte counts.
  while IFS= read -r name; do
    names+=("$name")
    values+=("${!name}")
  done < <(compgen -e)

  # Bash string lengths count bytes under the C locale.
  local LC_ALL=C
  size=1
  for ((i=0; i<${#names[@]}; i++)); do
    size=$((size + ${#names[i]} + ${#values[i]} + 2))
  done
  if (( ${#names[@]} == 0 )); then
    size=2
  fi
  if (( size > 120000 )); then
    printf 'GIT-IPC ERROR: environment snapshot too large\n' >&2
    return 1
  fi
  printf -v header '\\x%02x\\x%02x\\x%02x\\x%02x' \
    "$(( size & 255 ))" "$(( (size >> 8) & 255 ))" \
    "$(( (size >> 16) & 255 ))" "$(( (size >> 24) & 255 ))"
  printf 'env\0%b' "$header" || return 1
  for ((i=0; i<${#names[@]}; i++)); do
    printf '%s=%s\0' "${names[i]}" "${values[i]}" || return 1
  done
  if (( ${#names[@]} == 0 )); then
    printf '\0\0' || return 1
  else
    printf '\0' || return 1
  fi
}
