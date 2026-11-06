#!/usr/bin/env python3
"""RWF #809: launch one *actual* source-built Git worker waiting in wmain.

The source modification is applied ONLY in the temporary CI checkout.
"""
from __future__ import annotations
import os
from pathlib import Path
import subprocess

HOOK = r'''
#ifdef GIT809_WORKER_PROBE
static int git809_worker_dispatch(const char *token)
{
  char name[180];
  HANDLE pipe;
  char data[4096];
  DWORD used=0, n=0;
  int count, pos, i, exit_code;
  const char *args[33]={0};
  if (strlen(token)>35 || strchr(token,'\\') || strchr(token,'/'))
    return 89;
  if (snprintf(name,sizeof(name),"\\\\.\\pipe\\git809-worker-%s",token)
      >=sizeof(name))
    return 89;
  pipe=CreateNamedPipeA(name, PIPE_ACCESS_INBOUND,
      PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,
      1,4096,4096,5000,NULL);
  if(pipe==INVALID_HANDLE_VALUE)
    return 90;
  if(!ConnectNamedPipe(pipe,NULL) &&
     GetLastError()!=ERROR_PIPE_CONNECTED) {
    CloseHandle(pipe);return 91;
  }
  while(used<sizeof(data)) {
    if(!ReadFile(pipe,data+used,sizeof(data)-used,&n,NULL)) {
      if(GetLastError()!=ERROR_BROKEN_PIPE) {
        CloseHandle(pipe);return 92;
      }
      break;
    }
    if(!n) break;
    used+=n;
  }
  CloseHandle(pipe);
  if(used<4) return 93;
  count=(unsigned char)data[0]|
    ((unsigned char)data[1]<<8)|
    ((unsigned char)data[2]<<16)|
    ((unsigned char)data[3]<<24);
  if(count<1 || count>32) return 94;
  pos=4;
  for(i=0;i<count;i++) {
    int start=pos;
    if(pos>=used) return 95;
    while(pos<used && data[pos]) pos++;
    if(pos==used) return 96;
    if(pos>start && !MultiByteToWideChar(CP_UTF8,
        MB_ERR_INVALID_CHARS,data+start,pos-start,NULL,0))
      return 97;
    args[i]=data+start;
    pos++;
  }
  if(pos!=used || strcmp(args[0],"git")) return 98;
  args[count]=NULL;
  exit_code=main(count,args);
  return exit_code;
}
#endif
'''
DIRECT_HOOK = r'''
#ifdef GIT809_WORKER_PROBE
#include <setjmp.h>
static jmp_buf git809_direct_jump;
static volatile int git809_direct_active;
static volatile int git809_direct_exit_status;

/*
 * The standard Git builtin dispatcher calls exit(ret) from git.c.
 * The temporary benchmark build intercepts that dispatcher exit only, so
 * the one-shot worker can flush its native pipes and deliver the status.
 */
void git809_worker_exit(int status)
{
  if (git809_direct_active) {
    git809_direct_exit_status = status;
    longjmp(git809_direct_jump, 1);
  }
  exit(status);
}
/*
 * Experimental one-shot preinitialized Git worker.  The PID identifies four
 * two instances per channel; the Python manager never forwards command bytes.
 */
static int git809_direct_connect(HANDLE h)
{
  return ConnectNamedPipe(h, NULL) || GetLastError() == ERROR_PIPE_CONNECTED;
}

static int git809_direct_worker(void)
{
  char names[4][96];
  const char *suffixes[4] = {"stdin", "stdout", "stderr", "exit-code"};
  HANDLE pipes[4][2];
  char frame[4096], result[64];
  DWORD used = 0, n;
  int i, j, count, pos, rc = 120, written, outfd, errfd;
  const char *args[33] = {0};
  DWORD pid = GetCurrentProcessId();

  for (i = 0; i < 4; i++)
    for (j = 0; j < 2; j++)
      pipes[i][j] = INVALID_HANDLE_VALUE;
  for (i = 0; i < 4; i++) {
    if (snprintf(names[i], sizeof(names[i]), "\\\\.\\pipe\\%lu-%s",
                 (unsigned long)pid, suffixes[i]) >= sizeof(names[i]))
      goto cleanup;
    for (j = 0; j < 2; j++) {
      pipes[i][j] = CreateNamedPipeA(names[i],
        i == 0 ? PIPE_ACCESS_INBOUND : PIPE_ACCESS_OUTBOUND,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        2, 65536, 65536, 5000, NULL);
      if (pipes[i][j] == INVALID_HANDLE_VALUE) {
        rc = 121;
        goto cleanup;
      }
    }
  }
  /* READY is an initialization handshake, not Git command output. */
  printf("READY %lu\n", (unsigned long)pid);
  fflush(stdout);
  /*
   * Git Bash's initial open consumes a pipe instance without carrying data.
   * Keep a second instance ready for each channel's actual redirection.
   * Retain the original channel-open order.
   */
  {
    const int order[4] = {1, 2, 3, 0};
    for (i = 0; i < 4; i++) {
      int channel = order[i];
      if (!git809_direct_connect(pipes[channel][0]) ||
          !git809_direct_connect(pipes[channel][1])) {
        rc = 122;
        goto cleanup;
      }
      DisconnectNamedPipe(pipes[channel][0]);
      CloseHandle(pipes[channel][0]);
      pipes[channel][0] = INVALID_HANDLE_VALUE;
    }
  }
  while (used < sizeof(frame)) {
    if (!ReadFile(pipes[0][1], frame + used, sizeof(frame) - used, &n, NULL)) {
      if (GetLastError() != ERROR_BROKEN_PIPE) {
        rc = 123;
        goto cleanup;
      }
      break;
    }
    if (!n)
      break;
    used += n;
  }
  if (used < 4) {
    rc = 124;
    goto cleanup;
  }
  count = (unsigned char)frame[0] | ((unsigned char)frame[1] << 8) |
          ((unsigned char)frame[2] << 16) | ((unsigned char)frame[3] << 24);
  if (count < 1 || count > 32) {
    rc = 125;
    goto cleanup;
  }
  pos = 4;
  for (i = 0; i < count; i++) {
    int start = pos;
    if (pos >= used) {
      rc = 126;
      goto cleanup;
    }
    while (pos < used && frame[pos])
      pos++;
    if (pos == used || (pos > start &&
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                             frame + start, pos - start, NULL, 0))) {
      rc = 127;
      goto cleanup;
    }
    args[i] = frame + start;
    pos++;
  }
  if (pos != used || strcmp(args[0], "git")) {
    rc = 128;
    goto cleanup;
  }
  args[count] = NULL;
  FlushFileBuffers(pipes[0][1]);
  DisconnectNamedPipe(pipes[0][1]);
  CloseHandle(pipes[0][1]);
  pipes[0][1] = INVALID_HANDLE_VALUE;

  /* Transfer output handles to CRT descriptors used by Git's main(). */
  outfd = _open_osfhandle((intptr_t)pipes[1][1], _O_BINARY);
  if (outfd < 0) {
    rc = 129;
    goto cleanup;
  }
  pipes[1][1] = INVALID_HANDLE_VALUE;
  errfd = _open_osfhandle((intptr_t)pipes[2][1], _O_BINARY);
  if (errfd < 0) {
    _close(outfd);
    rc = 130;
    goto cleanup;
  }
  pipes[2][1] = INVALID_HANDLE_VALUE;
  if (_dup2(outfd, _fileno(stdout)) < 0 ||
      _dup2(errfd, _fileno(stderr)) < 0) {
    _close(outfd);
    _close(errfd);
    rc = 131;
    goto cleanup;
  }
  _close(outfd);
  _close(errfd);
  git809_direct_active = 1;
  if (!setjmp(git809_direct_jump))
    rc = main(count, args);
  else
    rc = git809_direct_exit_status;
  git809_direct_active = 0;
  fflush(stdout);
  fflush(stderr);
  /*
   * Windows waits until readers consume all pipe bytes. Closing each
   * descriptor then provides EOF. The client drains both concurrently.
   */
  FlushFileBuffers((HANDLE)_get_osfhandle(_fileno(stdout)));
  FlushFileBuffers((HANDLE)_get_osfhandle(_fileno(stderr)));
  _close(_fileno(stdout));
  _close(_fileno(stderr));
cleanup:
  written = snprintf(result, sizeof(result), "EXIT %d\n", rc);
  if (pipes[3][1] != INVALID_HANDLE_VALUE) {
    if (written > 0 && written < sizeof(result) &&
        WriteFile(pipes[3][1], result, written, &n, NULL))
      FlushFileBuffers(pipes[3][1]);
    DisconnectNamedPipe(pipes[3][1]);
    CloseHandle(pipes[3][1]);
    pipes[3][1] = INVALID_HANDLE_VALUE;
  }
  for (i = 0; i < 4; i++)
    for (j = 0; j < 2; j++)
      if (pipes[i][j] != INVALID_HANDLE_VALUE)
        CloseHandle(pipes[i][j]);
  return rc;
}
#endif
'''
def run(cmd,timeout=1800):
  p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                   timeout=timeout,check=False)
  if p.returncode:
    raise RuntimeError(f"command failed rc={p.returncode} {cmd!r}:\n"+
                       p.stdout.decode("utf-8","replace")[-1500:]+
                       p.stderr.decode("utf-8","replace")[-3500:])
def build(root:Path):
  p=root/"compat"/"mingw.c"
  source=p.read_text(encoding="utf-8")
  anchor="int wmain(int argc, const wchar_t **wargv)\n"
  assert source.count(anchor)==1,"unexpected Git Windows wmain signature"
  source=source.replace(anchor,HOOK+"\n"+DIRECT_HOOK+"\n"+anchor,1)
  call="\t/* invoke the real main() using our utf8 version of argv. */\n\texit_status = main(argc, argv);"
  assert source.count(call)==1,"unexpected Git main invocation"
  changed=(
    "\t/* invoke the real main() using our utf8 version of argv. */\n"
    "#ifdef GIT809_WORKER_PROBE\n"
    '\tif (argc == 3 && !strcmp(argv[1], "--git809-worker"))\n'
    "\t\texit_status = git809_worker_dispatch(argv[2]);\n"
    '\telse if (argc == 2 && !strcmp(argv[1], "--git809-direct-worker"))\n'
    "\t\texit_status = git809_direct_worker();\n"
    "\telse\n#endif\n"
    "\t\texit_status = main(argc, argv);"
  )
  source=source.replace(call,changed,1)
  p.write_text(source,encoding="utf-8")

  # git.c calls exit() for every builtin, including git --version.
  # Redirect those dispatch exits only in this ephemeral Windows build;
  # normal Git invocation and all other platforms retain their original code.
  gp=root/"git.c"
  gs=gp.read_text(encoding="utf-8")
  ga='#define RUN_SETUP\t\t(1<<0)'
  assert gs.count(ga)==1,"unexpected Git builtin dispatcher anchor"
  gs=gs.replace(ga,'#ifdef GIT809_WORKER_PROBE\n'
    'extern void git809_worker_exit(int status);\n'
    '#define exit(status) git809_worker_exit(status)\n'
    '#endif\n'+ga,1)
  gp.write_text(gs,encoding="utf-8")
  command=["make","-j4","NO_RUST=YesPlease","NO_CURL=YesPlease",
           "NO_GETTEXT=YesPlease","CFLAGS_APPEND=-DGIT809_WORKER_PROBE",
           "git.exe"]
  run(command)
  exe=(root/"git.exe").resolve()
  assert exe.is_file(), "source Git binary not built"
  return exe,command
