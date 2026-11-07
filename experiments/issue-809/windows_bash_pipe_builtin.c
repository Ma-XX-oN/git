/* RWF 809 experimental Windows transport for Git Bash.
 * A loadable Bash builtin, not a child process or data relay.
 * Communicates with one native named-pipe instance per endpoint.
 * Source-built Git worker and allocator speak the common four-channel frame.
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include "builtins.h"
#include "shell.h"

#define FRAME_CAP 4096
#define PIPE_WAIT_MS 5000
#define ERROR_BROKEN_PIPE_VALUE 109

struct channel {
  HANDLE pipe;
  int fd;
  volatile LONG failed;
};
static int output_all(int fd, const char *buf, size_t length)
{
  while (length) {
    ssize_t n = write(fd, buf, length);
    if (n < 0 && errno == EINTR)
      continue;
    if (n <= 0)
      return -1;
    buf += n;
    length -= (size_t)n;
  }
  return 0;
}
static DWORD WINAPI drain(void *arg)
{
  struct channel *c = (struct channel *)arg;
  char data[4096];
  DWORD count;
  for (;;) {
    if (ReadFile(c->pipe, data, sizeof(data), &count, NULL)) {
      if (count && output_all(c->fd, data, count) != 0) {
        c->failed = 1;
        break;
      }
    } else {
      DWORD error = GetLastError();
      if (error != ERROR_BROKEN_PIPE && error != ERROR_PIPE_NOT_CONNECTED)
        c->failed = 1;
      break;
    }
  }
  return 0;
}
static HANDLE connect_pipe(const char *path, DWORD access)
{
  HANDLE pipe;
  DWORD error;
  for (;;) {
    pipe = CreateFileA(path, access, 0, NULL, OPEN_EXISTING, 0, NULL);
    if (pipe != INVALID_HANDLE_VALUE)
      return pipe;
    error = GetLastError();
    if (error != ERROR_PIPE_BUSY && error != ERROR_FILE_NOT_FOUND)
      return INVALID_HANDLE_VALUE;
    if (!WaitNamedPipeA(path, PIPE_WAIT_MS))
      return INVALID_HANDLE_VALUE;
  }
}
static void pipe_name(char *out, size_t cap, unsigned long pid, const char *suffix)
{
  snprintf(out, cap, "\\\\.\\pipe\\%lu-%s", pid, suffix);
}
int git809pipe_builtin(WORD_LIST *list)
{
  const char *owner = getenv("_GIT809_OWNER_WINPID");
  char path[128], frame[FRAME_CAP], reply[128], buffer[64];
  const char *names[] = {"stdout", "stderr", "exit-code", "stdin"};
  HANDLE allocator = INVALID_HANDLE_VALUE;
  HANDLE pipe[4] = {INVALID_HANDLE_VALUE, INVALID_HANDLE_VALUE,
                    INVALID_HANDLE_VALUE, INVALID_HANDLE_VALUE};
  HANDLE threads[2] = {NULL, NULL};
  struct channel output[2];
  WORD_LIST *arg;
  DWORD n;
  size_t length = 4, pos = 0;
  unsigned long pid, owner_pid;
  char *end;
  unsigned count = 1;
  int i, rc = 255;
  const char *stage = "validate";
  memset(frame, 0, 4);
  if (!owner || !*owner)
    goto cleanup;
  owner_pid = strtoul(owner, &end, 10);
  if (!owner_pid || *end || owner_pid > 0xffffffffUL)
    goto cleanup;
  for (arg = list; arg; arg = arg->next)
    if (++count > 32)
      goto cleanup;
  for (arg = list; arg; arg = arg->next) {
    if (!arg->word || !arg->word->word)
      goto cleanup;
    if (strlen(arg->word->word) + length + 1 > FRAME_CAP)
      goto cleanup;
    length += strlen(arg->word->word) + 1;
  }
  if (length + sizeof("git") > FRAME_CAP)
    goto cleanup;
  frame[0] = (char)count;
  memcpy(frame + 4, "git", 4);
  pos = 8;
  for (arg = list; arg; arg = arg->next) {
    size_t size = strlen(arg->word->word) + 1;
    memcpy(frame + pos, arg->word->word, size);
    pos += size;
  }
  snprintf(path, sizeof(path), "\\\\.\\pipe\\git809-allocate-%lu", owner_pid);
  stage = "allocate-connect";
  allocator = connect_pipe(path, GENERIC_READ | GENERIC_WRITE);
  if (allocator == INVALID_HANDLE_VALUE)
    goto cleanup;
  stage = "allocate-request";
  if (!WriteFile(allocator, "GET\n", 4, &n, NULL) || n != 4)
    goto cleanup;
  stage = "allocate-reply";
  length = 0;
  while (length < sizeof(reply)-1) {
    if (!ReadFile(allocator, reply + length, 1, &n, NULL) || n != 1)
      goto cleanup;
    if (reply[length++] == '\n')
      break;
  }
  reply[length] = '\0';
  if (!length || reply[length-1] != '\n')
    goto cleanup;
  reply[length-1] = '\0';
  pid = strtoul(reply, &end, 10);
  if (!pid || *end || pid > 0xffffffffUL)
    goto cleanup;
  CloseHandle(allocator);
  allocator = INVALID_HANDLE_VALUE;
  stage = "worker-connect";
  /* Match worker's fixed one-instance ConnectNamedPipe sequence. */
  for (i = 0; i < 4; i++) {
    pipe_name(path, sizeof(path), pid, names[i]);
    pipe[i] = connect_pipe(path, i == 3 ? GENERIC_WRITE : GENERIC_READ);
    if (pipe[i] == INVALID_HANDLE_VALUE)
      goto cleanup;
  }
  stage = "worker-request";
  if (!WriteFile(pipe[3], frame, (DWORD)pos, &n, NULL) || n != pos)
    goto cleanup;
  CloseHandle(pipe[3]);
  pipe[3] = INVALID_HANDLE_VALUE;
  stage = "output-drain";
  for (i = 0; i < 2; i++) {
    output[i].pipe = pipe[i];
    output[i].fd = i == 0 ? STDOUT_FILENO : STDERR_FILENO;
    output[i].failed = 0;
    threads[i] = CreateThread(NULL, 0, drain, &output[i], 0, NULL);
    if (!threads[i])
      goto cleanup;
  }
  if (WaitForMultipleObjects(2, threads, TRUE, INFINITE) != WAIT_OBJECT_0)
    goto cleanup;
  if (output[0].failed || output[1].failed)
    goto cleanup;
  for (i = 0; i < 2; i++) {
    CloseHandle(threads[i]);
    threads[i] = NULL;
    CloseHandle(pipe[i]);
    pipe[i] = INVALID_HANDLE_VALUE;
  }
  stage = "exit-response";
  length = 0;
  while (length < sizeof(buffer)-1) {
    if (!ReadFile(pipe[2], buffer + length, 1, &n, NULL) || n != 1)
      goto cleanup;
    if (buffer[length++] == '\n')
      break;
  }
  buffer[length] = '\0';
  {
    char extra;
    if (!length || buffer[length-1] != '\n') {
      rc = 255;
    } else {
      buffer[length-1] = '\0';
      if (sscanf(buffer, "EXIT %d%c", &rc, &extra) != 1 ||
          rc < 0 || rc > 255)
        rc = 255;
    }
  }
cleanup:
  if (rc == 255)
    fprintf(stderr, "GIT809_NATIVE_ERROR stage=%s win32_error=%lu\n",
            stage, (unsigned long)GetLastError());
  /* Never free a channel context while its thread is still reading. */
  for (i = 0; i < 2; i++)
    if (threads[i]) {
      CancelSynchronousIo(threads[i]);
      if (pipe[i] != INVALID_HANDLE_VALUE)
        CancelIoEx(pipe[i], NULL);
      WaitForSingleObject(threads[i], INFINITE);
      CloseHandle(threads[i]);
      threads[i] = NULL;
    }
  for (i = 0; i < 4; i++)
    if (pipe[i] != INVALID_HANDLE_VALUE)
      CloseHandle(pipe[i]);
  if (allocator != INVALID_HANDLE_VALUE)
    CloseHandle(allocator);
  return rc;
}
char *git809pipe_doc[] = {
  "Issue 809 experimental native Windows pipe transport for preinitialized Git workers.",
  (char *)NULL
};
struct builtin git809pipe_struct = {
  "git809pipe", git809pipe_builtin, BUILTIN_ENABLED, git809pipe_doc,
  "git809pipe [git arguments]", 0
};
