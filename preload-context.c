#include "git-compat-util.h"
#include "preload-context.h"
#include "utf8.h"

#ifdef GIT_WINDOWS_NATIVE
#include <windows.h>
#endif

static int valid_environment_block(const char *buf, size_t len)
{
  size_t offset = 0;

  if (len < 2 || buf[len - 1] || buf[len - 2])
    return -1;
  if (len == 2)
    return 0;
  while (offset < len - 1 && buf[offset]) {
    const char *entry = buf + offset;
    const char *end = memchr(entry, '\0', len - offset);
    const char *equal;
    if (!end || !is_utf8(entry))
      return -1;
    equal = strchr(entry, '=');
    if (!equal || equal == entry || equal >= end)
      return -1;
    offset = (size_t)(end - buf) + 1;
  }
  return offset == len - 1 ? 0 : -1;
}

static int clear_worker_environment(void)
{
#ifdef GIT_WINDOWS_NATIVE
  wchar_t *block = GetEnvironmentStringsW();
  const wchar_t *entry;
  int result = 0;
  if (!block)
    return -1;
  for (entry = block; *entry; entry += wcslen(entry) + 1) {
    const wchar_t *equals = wcschr(entry + (*entry == L'='), L'=');
    wchar_t *key;
    size_t len;
    if (!equals)
      continue;
    /* Windows' =C: drive-directory pseudo-variables are not exported
     * by Bash; preserve them rather than treating them as ordinary keys. */
    if (*entry == L'=')
      continue;
    len = (size_t)(equals - entry);
    key = malloc((len + 1) * sizeof(*key));
    if (!key) {
      result = -1;
      break;
    }
    wmemcpy(key, entry, len);
    key[len] = L'\0';
    if (!SetEnvironmentVariableW(key, NULL))
      result = -1;
    free(key);
    if (result)
      break;
  }
  FreeEnvironmentStringsW(block);
  return result;
#else
  extern char **environ;
  while (environ && environ[0]) {
    const char *equal = strchr(environ[0], '=');
    char *key;
    size_t len;
    if (!equal || equal == environ[0])
      return -1;
    len = (size_t)(equal - environ[0]);
    key = malloc(len + 1);
    if (!key)
      return -1;
    memcpy(key, environ[0], len);
    key[len] = '\0';
    if (unsetenv(key)) {
      free(key);
      return -1;
    }
    free(key);
  }
  return 0;
#endif
}

static int apply_environment_block(const char *buf, size_t len)
{
  size_t offset = 0;
  if (valid_environment_block(buf, len))
    return -1;
  if (clear_worker_environment())
    return -1;
  while (offset < len - 1 && buf[offset]) {
    const char *entry = buf + offset;
    const char *equal = strchr(entry, '=');
    char *key;
    size_t name_len = (size_t)(equal - entry);
    key = malloc(name_len + 1);
    if (!key)
      return -1;
    memcpy(key, entry, name_len);
    key[name_len] = '\0';
    if (setenv(key, equal + 1, 1)) {
      free(key);
      return -1;
    }
    free(key);
    offset += strlen(entry) + 1;
  }
  return 0;
}

int preload_apply_context(const char *buf, size_t len)
{
  size_t offset = 0;
  int seen_cwd = 0, seen_env = 0;

  while (offset < len) {
    const char *tag = buf + offset;
    const char *end = memchr(tag, '\0', len - offset);
    if (!end)
      return -1;
    offset = (size_t)(end - buf) + 1;
    if (!strcmp(tag, "cwd") && !seen_cwd) {
      const char *cwd;
      end = memchr(buf + offset, '\0', len - offset);
      if (!end)
        return -1;
      cwd = buf + offset;
      if (!*cwd || !is_utf8(cwd) || chdir(cwd))
        return -1;
      seen_cwd = 1;
      offset = (size_t)(end - buf) + 1;
    } else if (!strcmp(tag, "env") && !seen_env) {
      unsigned long size;
      if (len - offset < 4)
        return -1;
      size = (unsigned char)buf[offset] |
        ((unsigned long)(unsigned char)buf[offset + 1] << 8) |
        ((unsigned long)(unsigned char)buf[offset + 2] << 16) |
        ((unsigned long)(unsigned char)buf[offset + 3] << 24);
      offset += 4;
      if (size > len - offset ||
          apply_environment_block(buf + offset, size))
        return -1;
      offset += size;
      seen_env = 1;
    } else {
      return -1;
    }
  }
  return 0;
}
