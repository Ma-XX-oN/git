// RWF 809 isolated Windows API probe. This does NOT test MSYS2 native handles.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdio>

static int fail(const char *step, DWORD err = GetLastError()) {
  std::fprintf(stderr, "FAIL %s error=%lu\n", step, err);
  return 1;
}

int main() {
  const wchar_t *path = L"\\\\.\\pipe\\git809-fifo-zero-read-probe";
  HANDLE server = CreateNamedPipeW(path,
    PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
    PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
    1, 4096, 4096, 0, nullptr);
  if (server == INVALID_HANDLE_VALUE) return fail("CreateNamedPipeW");

  HANDLE connect_evt = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!connect_evt) return fail("CreateEventW connect");
  OVERLAPPED connection = {};
  connection.hEvent = connect_evt;
  BOOL connected_now = ConnectNamedPipe(server, &connection);
  DWORD connect_issue = connected_now ? ERROR_SUCCESS : GetLastError();
  if (!connected_now && connect_issue != ERROR_IO_PENDING &&
      connect_issue != ERROR_PIPE_CONNECTED)
    return fail("ConnectNamedPipe", connect_issue);

  HANDLE client = CreateFileW(path, GENERIC_WRITE, 0, nullptr,
    OPEN_EXISTING, 0, nullptr);
  if (client == INVALID_HANDLE_VALUE) return fail("CreateFileW");

  if (!connected_now && connect_issue == ERROR_IO_PENDING) {
    if (WaitForSingleObject(connect_evt, 3000) != WAIT_OBJECT_0)
      return fail("ConnectNamedPipe timeout", WAIT_TIMEOUT);
    DWORD ignored = 0;
    if (!GetOverlappedResult(server, &connection, &ignored, FALSE))
      return fail("GetOverlappedResult connect");
  }
  CloseHandle(connect_evt);

  HANDLE ready = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!ready) return fail("CreateEventW zero-read");

  OVERLAPPED zero = {};
  zero.hEvent = ready;
  char scratch = 0;
  DWORD nbytes = 0;
  BOOL immediate = ReadFile(server, &scratch, 0, &nbytes, &zero);
  DWORD issue = immediate ? ERROR_SUCCESS : GetLastError();
  if (!immediate && issue != ERROR_IO_PENDING)
    return fail("ReadFile zero", issue);

  if (immediate || WaitForSingleObject(ready, 20) != WAIT_TIMEOUT) {
    std::fprintf(stderr, "FAIL zero-byte read did not pend until data\n");
    return 2;
  }

  DWORD sent = 0;
  const char payload = 'X';
  if (!WriteFile(client, &payload, 1, &sent, nullptr) || sent != 1)
    return fail("WriteFile payload");

  DWORD wake = WaitForMultipleObjects(1, &ready, FALSE, 3000);
  if (wake != WAIT_OBJECT_0) {
    std::fprintf(stderr, "FAIL readiness wake status=%lu\n", wake);
    return 3;
  }

  DWORD zero_count = 99;
  BOOL result = GetOverlappedResult(server, &zero, &zero_count, FALSE);
  DWORD completion = result ? ERROR_SUCCESS : GetLastError();
  // Message-mode zero-byte reads report ERROR_MORE_DATA when the
  // unread message remains in the pipe. This is a readiness signal;
  // validate that the subsequent real read still receives the byte.
  if (!result && completion != ERROR_MORE_DATA)
    return fail("GetOverlappedResult zero", completion);
  std::printf("ZERO_READ completion=%lu zero_count=%lu\n", completion, zero_count);
  if (zero_count != 0) {
    std::fprintf(stderr, "FAIL zero-read consumed %lu bytes\n", zero_count);
    return 4;
  }

  HANDLE data_evt = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!data_evt) return fail("CreateEventW data");
  OVERLAPPED data = {};
  data.hEvent = data_evt;
  char received = 0;
  DWORD count = 0;
  BOOL read_now = ReadFile(server, &received, 1, &count, &data);
  if (!read_now) {
    DWORD err = GetLastError();
    if (err != ERROR_IO_PENDING) return fail("ReadFile payload", err);
    if (WaitForSingleObject(data_evt, 3000) != WAIT_OBJECT_0)
      return fail("ReadFile payload timeout", WAIT_TIMEOUT);
    if (!GetOverlappedResult(server, &data, &count, FALSE))
      return fail("GetOverlappedResult data");
  }
  if (count != 1 || received != payload) {
    std::fprintf(stderr, "FAIL payload incorrect len=%lu byte=%d\n",
      count, static_cast<int>(received));
    return 5;
  }

  // Match the FILE_PIPE_COMPLETE_OPERATION mode selected by
  // MSYS2 record_connection() and observe whether a zero-byte read
  // genuinely remains pending on an empty connected message pipe.
  DWORD nowait_mode = PIPE_READMODE_MESSAGE | PIPE_NOWAIT;
  if (!SetNamedPipeHandleState(server, &nowait_mode, nullptr, nullptr))
    return fail("SetNamedPipeHandleState NOWAIT");
  HANDLE idle_evt = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!idle_evt) return fail("CreateEventW nowait");
  OVERLAPPED idle = {};
  idle.hEvent = idle_evt;
  DWORD empty_count = 99;
  BOOL empty_immediate = ReadFile(server, &scratch, 0, &empty_count, &idle);
  DWORD empty_status = empty_immediate ? ERROR_SUCCESS : GetLastError();
  std::printf("NOWAIT_ZERO immediate=%u status=%lu bytes=%lu\\n",
              static_cast<unsigned>(empty_immediate), empty_status,
              empty_count);
  if (!empty_immediate && empty_status == ERROR_IO_PENDING) {
    DWORD waited = WaitForSingleObject(idle_evt, 20);
    std::printf("NOWAIT_ZERO pending_wait=%lu\\n", waited);
    if (waited != WAIT_TIMEOUT) {
      std::fprintf(stderr, "FAIL unexpected empty-read completion=%lu\\n", waited);
      return 6;
    }
    if (!CancelIoEx(server, &idle) && GetLastError() != ERROR_NOT_FOUND)
      return fail("CancelIoEx nowait read");
    if (WaitForSingleObject(idle_evt, 3000) != WAIT_OBJECT_0)
      return fail("Nowait read cancellation timeout", WAIT_TIMEOUT);
    DWORD ignored = 0;
    BOOL settled = GetOverlappedResult(server, &idle, &ignored, FALSE);
    std::printf("NOWAIT_ZERO canceled_completion=%lu\\n",
                settled ? ERROR_SUCCESS : GetLastError());
  }
  CloseHandle(idle_evt);

  CloseHandle(data_evt);
  CloseHandle(ready);
  CloseHandle(client);
  CloseHandle(server);
  std::puts("PASS zero-byte overlapped read waits for data and consumes none");
  return 0;
}
