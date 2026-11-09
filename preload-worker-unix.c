#include "git-compat-util.h"
#ifndef GIT_WINDOWS_NATIVE
#include "common-init.h"
#include "preload-context.h"
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <pthread.h>
#include <signal.h>
#include <setjmp.h>
#include <sys/stat.h>
#include <unistd.h>

/*
 * A --preload worker executes one command and exits. Its private channel
 * directory is created by the owning manager, not by the Git worker.
 */
static jmp_buf preload_exit_jump;
static volatile int preload_exit_active;
static volatile int preload_exit_status;

static NORETURN void git_preload_worker_exit(int status)
{
  if (preload_exit_active) {
    preload_exit_status = status;
    longjmp(preload_exit_jump, 1);
  }
  exit(status);
}

/*
 * The allocation manager relinquishes command ownership to git-ipc. Check
 * the originating Bash PID independently every 10 seconds. Do not use
 * getppid(): the worker's immediate parent is the allocation manager.
 */
static void git_preload_remove_fifos(const char *directory)
{
  static const char *const names[5] = {
    "stdout", "stderr", "exit-code", "stdin", "param-env-cwd"
  };
  char path[1024];
  int i;

  for (i = 0; i < 5; i++) {
    int length = snprintf(path, sizeof(path), "%s/%s", directory,
                          names[i]);
    if (length >= 0 && (size_t)length < sizeof(path))
      unlink(path);
  }
  rmdir(directory);
}

struct preload_owner_watch {
  pid_t pid;
  char directory[1];
};

static void *git_preload_watch_owner(void *context)
{
  struct preload_owner_watch *watch = context;
  pid_t owner_pid = watch->pid;

  for (;;) {
    unsigned int left = 10;

    while (left)
      left = sleep(left);
    if (kill(owner_pid, 0) < 0 && errno == ESRCH) {
      git_preload_remove_fifos(watch->directory);
      _exit(137);
    }
  }
}

static int git_preload_start_owner_watch(const char *directory)
{
  const char *raw = getenv("GIT809_PRELOAD_OWNER_PID");
  unsigned long value = 0;
  pthread_t watcher;
  struct preload_owner_watch *watch;
  size_t i, n;

  if (!raw || !*raw)
    return -1;
  for (i = 0; raw[i]; i++) {
    unsigned char ch = raw[i];

    if (ch < '0' || ch > '9' ||
        value > (LONG_MAX - (ch - '0')) / 10)
      return -1;
    value = value * 10 + ch - '0';
  }
  if (!value || value > INT_MAX ||
      (kill((pid_t)value, 0) < 0 && errno == ESRCH))
    return -1;
  n = strlen(directory);
  watch = malloc(sizeof(*watch) + n);
  if (!watch)
    return -1;
  watch->pid = (pid_t)value;
  memcpy(watch->directory, directory, n + 1);
  if (pthread_create(&watcher, NULL, git_preload_watch_owner, watch)) {
    free(watch);
    return -1;
  }
  pthread_detach(watcher);
  return 0;
}

int git_preload_unix_worker(const char *directory)
{
  static const char *const suffixes[5] = {
    "stdout", "stderr", "exit-code", "stdin", "param-env-cwd"
  };
  char paths[5][1024];
  char *frame = NULL;
  char result[64];
  const size_t frame_capacity = 131072;
  const char *args[33] = { NULL };
  int fd[5] = { -1, -1, -1, -1, -1 };
  int i, count, pos, rc = 120;
  size_t used = 0;
  ssize_t n;

  if (git_preload_start_owner_watch(directory))
    return 136;
  if (!directory || strlen(directory) > 960)
    return 121;
  frame = malloc(frame_capacity);
  if (!frame)
    return 121;
  for (i = 0; i < 5; i++) {
    int length = snprintf(paths[i], sizeof(paths[i]), "%s/%s",
                          directory, suffixes[i]);
    if (length < 0 || (size_t)length >= sizeof(paths[i])) {
      rc = 121;
      goto cleanup;
    }
  }

  printf("READY %ld\n", (long)getpid());
  fflush(stdout);

  /* Match the four-channel open order used by git-ipc. */
  for (i = 0; i < 3; i++) {
    fd[i] = open(paths[i], O_WRONLY);
    if (fd[i] < 0) {
      rc = 122;
      goto cleanup;
    }
  }
  fd[3] = open(paths[3], O_RDONLY);
  if (fd[3] < 0) {
    rc = 123;
    goto cleanup;
  }
  fd[4] = open(paths[4], O_RDONLY);
  if (fd[4] < 0) {
    rc = 123;
    goto cleanup;
  }

  while (used < frame_capacity) {
    n = read(fd[4], frame + used, frame_capacity - used);
    if (n < 0) {
      if (errno == EINTR)
        continue;
      rc = 124;
      goto cleanup;
    }
    if (!n)
      break;
    used += (size_t)n;
  }
  close(fd[4]); fd[4] = -1;
  if (used < 4 || used == frame_capacity) {
    rc = 125;
    goto cleanup;
  }
  count = (unsigned char)frame[0] |
    ((unsigned char)frame[1] << 8) |
    ((unsigned char)frame[2] << 16) |
    ((unsigned char)frame[3] << 24);
  if (count < 1 || count > 32) {
    rc = 126;
    goto cleanup;
  }
  pos = 4;
  for (i = 0; i < count; i++) {
    int start = pos;
    if ((size_t)pos >= used) {
      rc = 127;
      goto cleanup;
    }
    while ((size_t)pos < used && frame[pos])
      pos++;
    if ((size_t)pos == used) {
      rc = 128;
      goto cleanup;
    }
    args[i] = frame + start;
    pos++;
  }
  if (strcmp(args[0], "git") ||
      preload_apply_context(frame + pos, used - (size_t)pos)) {
    rc = 129;
    goto cleanup;
  }
  args[count] = NULL;

  /*
   * The request writer has reached EOF, but its readable endpoint remains
   * valid. Redirect it to Git's stdin instead of closing descriptor zero.
   */
  if (dup2(fd[3], STDIN_FILENO) < 0 ||
      dup2(fd[0], STDOUT_FILENO) < 0 ||
      dup2(fd[1], STDERR_FILENO) < 0) {
    rc = 130;
    goto cleanup;
  }
  close(fd[3]); fd[3] = -1;
  close(fd[0]); fd[0] = -1;
  close(fd[1]); fd[1] = -1;

  /* Defer Git initialization until after the command arrives. */
  preload_exit_active = 1;
  set_preload_exit_callback(git_preload_worker_exit);
  if (!setjmp(preload_exit_jump)) {
    init_git(args);
    rc = cmd_main(count, args);
    set_preload_exit_callback(NULL);
    rc = common_exit(__FILE__, __LINE__, rc);
  } else {
    rc = preload_exit_status;
  }
  set_preload_exit_callback(NULL);
  preload_exit_active = 0;

  fflush(stdout);
  fflush(stderr);
  close(STDOUT_FILENO);
  close(STDERR_FILENO);
  close(STDIN_FILENO);

cleanup:
  n = snprintf(result, sizeof(result), "EXIT %d\n", rc);
  if (fd[2] >= 0) {
    if (n > 0 && (size_t)n < sizeof(result))
      (void)write(fd[2], result, (size_t)n);
    close(fd[2]);
    fd[2] = -1;
  }
  for (i = 0; i < 5; i++)
    if (fd[i] >= 0)
      close(fd[i]);
  git_preload_remove_fifos(directory);
  free(frame);
  return rc;
}
#endif
