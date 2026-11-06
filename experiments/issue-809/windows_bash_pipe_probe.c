/* Issue 809: native Windows named pipe rendezvous for direct Git Bash tests.
 * Standalone diagnostic; no Git production code modifications. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

int main(int argc, char **argv) {
  if (argc != 3) return 2;
  int inbound = strcmp(argv[2], "in") == 0;
  int outbound = strcmp(argv[2], "out") == 0;
  int duplex = strcmp(argv[2], "duplex") == 0;
  if (!inbound && !outbound && !duplex) return 2;
  char name[180];
  int n = snprintf(name, sizeof(name), "\\\\.\\pipe\\git809-bash-%s", argv[1]);
  if (n < 0 || (size_t)n >= sizeof(name)) return 2;
  HANDLE h = CreateNamedPipeA(name, inbound ? PIPE_ACCESS_INBOUND :
                             outbound ? PIPE_ACCESS_OUTBOUND : PIPE_ACCESS_DUPLEX,
     PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, 1, 4096, 4096,
     5000, NULL);
  if (h == INVALID_HANDLE_VALUE) {
    fprintf(stderr, "CreateNamedPipe failed: %lu\n", GetLastError());
    return 3;
  }
  printf("READY\n"); fflush(stdout);
  BOOL connected = ConnectNamedPipe(h, NULL);
  if (!connected && GetLastError() != ERROR_PIPE_CONNECTED) {
    fprintf(stderr, "ConnectNamedPipe failed: %lu\n", GetLastError());
    CloseHandle(h); return 4;
  }
  char buf[80];
  DWORD got=0;
  if (!outbound && !ReadFile(h, buf, sizeof(buf), &got, NULL)) {
    fprintf(stderr, "ReadFile failed: %lu\n", GetLastError());
    CloseHandle(h); return 5;
  }
  if (!outbound && (got != 5 || memcmp(buf, "PING\n", 5) != 0)) {
    fprintf(stderr, "unexpected request of %lu bytes\n", (unsigned long)got);
    CloseHandle(h); return 6;
  }
  const char reply[] = "PONG\n";
  DWORD written=0;
  if (!inbound && (!WriteFile(h, reply, sizeof(reply)-1, &written, NULL) ||
      written != sizeof(reply)-1)) {
    fprintf(stderr, "WriteFile failed: %lu\n", GetLastError());
    CloseHandle(h); return 7;
  }
  if (!inbound) FlushFileBuffers(h);
  DisconnectNamedPipe(h);
  CloseHandle(h);
  puts("COMPLETE");
  return 0;
}
