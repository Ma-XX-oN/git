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
struct git809_conn {
  HANDLE pipe;
  HANDLE done;
  LONG *first;
  int index;
  int ok;
  int closed_before_accept;
};

static DWORD WINAPI git809_wait_connection(void *arg)
{
  struct git809_conn *c = (struct git809_conn *)arg;
  BOOL connected = ConnectNamedPipe(c->pipe, NULL);
  DWORD error = connected ? ERROR_SUCCESS : GetLastError();
  c->closed_before_accept = error == ERROR_NO_DATA;
  c->ok = connected || error == ERROR_PIPE_CONNECTED ||
          c->closed_before_accept;
  if (c->ok)
    InterlockedCompareExchange(c->first, c->index, -1);
  SetEvent(c->done);
  return 0;
}

/* Query kernel pipe state after both client connections complete.
 * Thread wake order is not the order in which clients opened the pipe.
 * The short-lived metadata probe reaches CLOSING (4), while Bash retains
 * the actual transport connection in CONNECTED (3). */
struct git809_pipe_local {
  ULONG type, configuration, max_instances, instances;
  ULONG inbound_quota, read_available, outbound_quota, write_available;
  ULONG state, end;
};
typedef LONG (NTAPI *git809_query_fn)(HANDLE, PVOID, PVOID, ULONG, int);
static int git809_pipe_state(HANDLE pipe, ULONG *state)
{
  static git809_query_fn query;
  struct { union { LONG status; PVOID pointer; }; ULONG_PTR information; } io;
  struct git809_pipe_local local = {0};
  LONG status;
  if (!query) {
    HMODULE dll = GetModuleHandleA("ntdll.dll");
    query = dll ? (git809_query_fn)GetProcAddress(dll, "NtQueryInformationFile") : NULL;
  }
  if (!query) return 0;
  status = query(pipe, &io, &local, sizeof(local), 24);
  if (status < 0 || local.end != 1) return 0;
  *state = local.state;
  return 1;
}

/* Both pipe instances exist before either client connection. Threads wait
 * independently so the first completed connection is discovered dynamically.
 * All four channels retain exactly one probe and one data instance. */
static int git809_accept_pair(HANDLE pair[2], int *probe, int *data)
{
  struct git809_conn c[2];
  HANDLE threads[2] = { NULL, NULL };
  HANDLE events[2];
  LONG first = -1;
  DWORD winner;
  int j, ok = 0;
  c[0].done = NULL;
  c[1].done = NULL;
  for (j = 0; j < 2; j++) {
    c[j].pipe = pair[j];
    c[j].done = CreateEventW(NULL, TRUE, FALSE, NULL);
    if (!c[j].done)
      goto finish;
    c[j].first = &first;
    c[j].index = j;
    c[j].ok = 0;
    c[j].closed_before_accept = 0;
    threads[j] = CreateThread(NULL, 0, git809_wait_connection, &c[j], 0, NULL);
    if (!threads[j])
      goto finish;
  }
  events[0] = c[0].done;
  events[1] = c[1].done;
  winner = WaitForMultipleObjects(2, events, FALSE, INFINITE);
  if (winner != WAIT_OBJECT_0 && winner != WAIT_OBJECT_0 + 1)
    goto finish;
  if (WaitForMultipleObjects(2, threads, TRUE, INFINITE) != WAIT_OBJECT_0)
    goto finish;
  if (!c[0].ok || !c[1].ok || (first != 0 && first != 1))
    goto finish;
  /* Do not use the scheduler's winner index to classify the pipes.
   * Query the independent kernel connection state until the metadata
   * client closes. This preserves synchronous handles for Git stdout. */
  for (j = 0; j < 2000; j++) {
    ULONG state0, state1;
    if (!git809_pipe_state(pair[0], &state0) ||
        !git809_pipe_state(pair[1], &state1))
      goto finish;
    if (state0 == 4 && state1 == 3) {
      *probe = 0; *data = 1; ok = 1; break;
    }
    if (state1 == 4 && state0 == 3) {
      *probe = 1; *data = 0; ok = 1; break;
    }
    if ((state0 != 3 && state0 != 4) ||
        (state1 != 3 && state1 != 4))
      goto finish;
    Sleep(1);
  }
finish:
  for (j = 0; j < 2; j++) {
    if (threads[j]) CloseHandle(threads[j]);
    if (c[j].done) CloseHandle(c[j].done);
  }
  return ok;
}


/* Diagnostic-only logging. The wrappers preserve the API result and
 * GetLastError; the production transport and connection algorithm are
 * unchanged. Every ENTER record is flushed before the blocking call. */
static void git809_trace_api(const char *op, const char *phase,
                            HANDLE h, DWORD result, DWORD error, DWORD aux)
{
  char directory[1024], filename[1160], line[512];
  DWORD len = GetEnvironmentVariableA("GIT809_NATIVE_TRACE_DIR",
                                      directory, sizeof(directory));
  DWORD pid = GetCurrentProcessId();
  HANDLE file;
  int chars;
  DWORD written;
  DWORD old_error = GetLastError();
  if (!len || len >= sizeof(directory))
    return;
  chars = snprintf(filename, sizeof(filename), "%s\\worker-%lu.log",
                   directory, (unsigned long)pid);
  if (chars < 1 || chars >= sizeof(filename))
    return;
  file = CreateFileA(filename, FILE_APPEND_DATA,
                     FILE_SHARE_READ | FILE_SHARE_WRITE,
                     NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
  if (file != INVALID_HANDLE_VALUE) {
    chars = snprintf(line, sizeof(line),
      "t=%llu pid=%lu tid=%lu phase=%s op=%s handle=%p result=%lu error=%lu aux=%lu\n",
      (unsigned long long)GetTickCount64(), (unsigned long)pid,
      (unsigned long)GetCurrentThreadId(), phase, op, h,
      (unsigned long)result, (unsigned long)error, (unsigned long)aux);
    if (chars > 0 && chars < sizeof(line))
      WriteFile(file, line, (DWORD)chars, &written, NULL);
    CloseHandle(file);
  }
  SetLastError(old_error);
}
static BOOL git809_traced_connect(HANDLE h, LPOVERLAPPED ov)
{
  BOOL ok; DWORD error;
  git809_trace_api("ConnectNamedPipe", "ENTER", h, 0, 0, 0);
  ok = ConnectNamedPipe(h, ov);
  error = ok ? ERROR_SUCCESS : GetLastError();
  git809_trace_api("ConnectNamedPipe", "RETURN", h, ok, error, 0);
  SetLastError(error);
  return ok;
}
static BOOL git809_traced_read(HANDLE h, LPVOID b, DWORD n, LPDWORD used, LPOVERLAPPED ov)
{
  BOOL ok; DWORD error;
  git809_trace_api("ReadFile", "ENTER", h, 0, 0, n);
  ok = ReadFile(h, b, n, used, ov);
  error = ok ? ERROR_SUCCESS : GetLastError();
  git809_trace_api("ReadFile", "RETURN", h, ok, error, ok && used ? *used : 0);
  SetLastError(error);
  return ok;
}
static BOOL git809_traced_write(HANDLE h, LPCVOID b, DWORD n, LPDWORD used, LPOVERLAPPED ov)
{
  BOOL ok; DWORD error;
  git809_trace_api("WriteFile", "ENTER", h, 0, 0, n);
  ok = WriteFile(h, b, n, used, ov);
  error = ok ? ERROR_SUCCESS : GetLastError();
  git809_trace_api("WriteFile", "RETURN", h, ok, error, ok && used ? *used : 0);
  SetLastError(error);
  return ok;
}
static BOOL git809_traced_flush(HANDLE h)
{
  BOOL ok; DWORD error;
  git809_trace_api("FlushFileBuffers", "ENTER", h, 0, 0, 0);
  ok = FlushFileBuffers(h);
  error = ok ? ERROR_SUCCESS : GetLastError();
  git809_trace_api("FlushFileBuffers", "RETURN", h, ok, error, 0);
  SetLastError(error);
  return ok;
}
static BOOL git809_traced_disconnect(HANDLE h)
{
  BOOL ok; DWORD error;
  git809_trace_api("DisconnectNamedPipe", "ENTER", h, 0, 0, 0);
  ok = DisconnectNamedPipe(h);
  error = ok ? ERROR_SUCCESS : GetLastError();
  git809_trace_api("DisconnectNamedPipe", "RETURN", h, ok, error, 0);
  SetLastError(error);
  return ok;
}
#define ConnectNamedPipe git809_traced_connect
#define ReadFile git809_traced_read
#define WriteFile git809_traced_write
#define FlushFileBuffers git809_traced_flush
#define DisconnectNamedPipe git809_traced_disconnect

static int git809_direct_worker(void)
{
  char names[4][96];
  const char *suffixes[4] = {"stdin", "stdout", "stderr", "exit-code"};
  HANDLE pipes[4][2];
  char frame[4096], result[64];
  DWORD used = 0, n;
  int i, j, count, pos, rc = 120, written, outfd, errfd;
  int data_index[4] = { -1, -1, -1, -1 };
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
  git809_trace_api("worker-ready", "EVENT", NULL, 0, 0, 0);
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
      int probe, data;
      git809_trace_api(suffixes[channel], "ACCEPT_ENTER", pipes[channel][0], 0, 0, channel);
      if (!git809_accept_pair(pipes[channel], &probe, &data)) {
        rc = 122;
        goto cleanup;
      }
      git809_trace_api(suffixes[channel], "ACCEPT_RETURN", pipes[channel][data], 1, 0, probe);
      data_index[channel] = data;
      DisconnectNamedPipe(pipes[channel][probe]);
      CloseHandle(pipes[channel][probe]);
      pipes[channel][probe] = INVALID_HANDLE_VALUE;
    }
  }
  while (used < sizeof(frame)) {
    if (!ReadFile(pipes[0][data_index[0]], frame + used, sizeof(frame) - used, &n, NULL)) {
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
  FlushFileBuffers(pipes[0][data_index[0]]);
  DisconnectNamedPipe(pipes[0][data_index[0]]);
  CloseHandle(pipes[0][data_index[0]]);
  pipes[0][data_index[0]] = INVALID_HANDLE_VALUE;

  /* Transfer output handles to CRT descriptors used by Git's main(). */
  outfd = _open_osfhandle((intptr_t)pipes[1][data_index[1]], _O_BINARY);
  if (outfd < 0) {
    rc = 129;
    goto cleanup;
  }
  pipes[1][data_index[1]] = INVALID_HANDLE_VALUE;
  errfd = _open_osfhandle((intptr_t)pipes[2][data_index[2]], _O_BINARY);
  if (errfd < 0) {
    _close(outfd);
    rc = 130;
    goto cleanup;
  }
  pipes[2][data_index[2]] = INVALID_HANDLE_VALUE;
  if (_dup2(outfd, _fileno(stdout)) < 0 ||
      _dup2(errfd, _fileno(stderr)) < 0) {
    _close(outfd);
    _close(errfd);
    rc = 131;
    goto cleanup;
  }
  _close(outfd);
  _close(errfd);
  git809_trace_api("git-main", "ENTER", NULL, 0, 0, count);
  git809_direct_active = 1;
  if (!setjmp(git809_direct_jump))
    rc = main(count, args);
  else
    rc = git809_direct_exit_status;
  git809_direct_active = 0;
  git809_trace_api("git-main", "RETURN", NULL, (DWORD)rc, 0, 0);
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
  if (data_index[3] >= 0 &&
      pipes[3][data_index[3]] != INVALID_HANDLE_VALUE) {
    if (written > 0 && written < sizeof(result) &&
        WriteFile(pipes[3][data_index[3]], result, written, &n, NULL))
      FlushFileBuffers(pipes[3][data_index[3]]);
    DisconnectNamedPipe(pipes[3][data_index[3]]);
    CloseHandle(pipes[3][data_index[3]]);
    pipes[3][data_index[3]] = INVALID_HANDLE_VALUE;
  }
  for (i = 0; i < 4; i++)
    for (j = 0; j < 2; j++)
      if (pipes[i][j] != INVALID_HANDLE_VALUE)
        CloseHandle(pipes[i][j]);
  git809_trace_api("worker-exit", "EVENT", NULL, (DWORD)rc, 0, 0);
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
