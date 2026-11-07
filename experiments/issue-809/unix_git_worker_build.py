#!/usr/bin/env python3
"""Temporary Unix-only source hook for preinitialized Git #809 workers.

Edits only the CI checkout's git.c and builds the same Git source as the
unmodified direct benchmark. This is not a production Git command.
"""
from pathlib import Path
import subprocess

HOOK = r'''
#ifdef GIT809_UNIX_WORKER
#include <setjmp.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

static jmp_buf git809_unix_jump;
static volatile int git809_unix_active;
static volatile int git809_unix_exit_status;

static void git809_unix_exit(int status)
{
  if (git809_unix_active) {
    git809_unix_exit_status = status;
    longjmp(git809_unix_jump, 1);
  }
  exit(status);
}

int cmd_main(int argc, const char **argv);

static int git809_unix_worker(const char *directory)
{
  const char *names[] = {"stdout", "stderr", "exit-code", "stdin"};
  char paths[4][1024];
  char frame[4096], result[64];
  const char *args[33] = {0};
  int fd[4] = {-1, -1, -1, -1};
  int i, count, pos, rc = 120;
  size_t used = 0;
  ssize_t n;

  if (strlen(directory) > 960)
    return 121;
  for (i = 0; i < 4; i++)
    if (snprintf(paths[i], sizeof(paths[i]), "%s/%s",
                 directory, names[i]) >= sizeof(paths[i]))
      return 121;

  printf("READY %ld\n", (long)getpid());
  fflush(stdout);

  /* Match the Bash client's descriptor-open order exactly. */
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
  while (used < sizeof(frame)) {
    n = read(fd[3], frame + used, sizeof(frame) - used);
    if (n < 0) {
      if (errno == EINTR)
        continue;
      rc = 124;
      goto cleanup;
    }
    if (n == 0)
      break;
    used += (size_t)n;
  }
  close(fd[3]);
  fd[3] = -1;
  if (used < 4) {
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
  if ((size_t)pos != used || strcmp(args[0], "git")) {
    rc = 129;
    goto cleanup;
  }
  args[count] = NULL;

  if (dup2(fd[0], STDOUT_FILENO) < 0 ||
      dup2(fd[1], STDERR_FILENO) < 0) {
    rc = 130;
    goto cleanup;
  }
  close(fd[0]);
  fd[0] = -1;
  close(fd[1]);
  fd[1] = -1;

  git809_unix_active = 1;
  if (!setjmp(git809_unix_jump))
    rc = cmd_main(count, args);
  else
    rc = git809_unix_exit_status;
  git809_unix_active = 0;

  fflush(stdout);
  fflush(stderr);
  close(STDOUT_FILENO);
  close(STDERR_FILENO);
cleanup:
  n = snprintf(result, sizeof(result), "EXIT %d\n", rc);
  if (fd[2] >= 0) {
    if (n > 0 && (size_t)n < sizeof(result))
      (void)write(fd[2], result, (size_t)n);
    close(fd[2]);
  }
  for (i = 0; i < 4; i++)
    if (i != 2 && fd[i] >= 0)
      close(fd[i]);
  return rc;
}

/* Git's builtin dispatcher exits directly; capture that status once. */
#define exit(status) git809_unix_exit(status)
#endif
'''

def build(root: Path):
    source_path = root / "git.c"
    source = source_path.read_text(encoding="utf-8")
    anchor = "#define RUN_SETUP\t\t(1<<0)"
    if source.count(anchor) != 1:
        raise RuntimeError("Git builtin-dispatch anchor changed")
    source = source.replace(anchor, HOOK + "\n" + anchor, 1)
    entry = "int cmd_main(int argc, const char **argv)\n{"
    if source.count(entry) != 1:
        raise RuntimeError("Git cmd_main entry changed")
    source = source.replace(entry,
        entry + '\n#ifdef GIT809_UNIX_WORKER\n'
        '  if (argc == 3 && !strcmp(argv[1], "--git809-unix-worker"))\n'
        '    return git809_unix_worker(argv[2]);\n'
        '#endif', 1)
    source_path.write_text(source, encoding="utf-8")
    cmd = ["make", "-j4", "NO_RUST=YesPlease", "NO_CURL=YesPlease",
           "NO_GETTEXT=YesPlease", "CFLAGS_APPEND=-DGIT809_UNIX_WORKER", "git"]
    subprocess.run(cmd, cwd=root, check=True, timeout=1800,
                   stdout=subprocess.DEVNULL)
    exe = (root / "git").resolve(strict=True)
    return exe, cmd
