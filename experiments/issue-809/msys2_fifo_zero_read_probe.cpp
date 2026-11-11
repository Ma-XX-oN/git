// RWF 809 isolated Windows API probe. This does NOT test MSYS2 native handles.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdio>
#include <cwchar>

static int fail(const char *step, DWORD err = GetLastError()) {
  std::fprintf(stderr, "FAIL %s error=%lu\n", step, err);
  return 1;
}

// Native NT API creation-path probe uses MSYS2's pipe creation flags.
static int probe_native_nt_fifo();

int main() {
  const wchar_t *path = L"\\\\.\\pipe\\git809-fifo-zero-read-probe";
  HANDLE server = CreateNamedPipeW(path,
    PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
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
  if (probe_native_nt_fifo() != 0) return 8;
  std::puts("PASS Win32 control and native NT pipe completion-mode probe");
  return 0;
}


/*
 * Reproduce MSYS2 fifo.cc::create_pipe_instance() independently of
 * CreateNamedPipeW. This exercises the NT creation entry point, access mask,
 * message type, queued completion mode, and the subsequent completion-mode
 * transition on the *same native handle*.
 *
 * This is not an actual fhandler_fifo handle or a patched MSYS2 DLL.
 */
struct native_unicode_string {
  USHORT Length;
  USHORT MaximumLength;
  PWSTR Buffer;
};
struct native_object_attributes {
  ULONG Length;
  HANDLE RootDirectory;
  native_unicode_string *ObjectName;
  ULONG Attributes;
  PVOID SecurityDescriptor;
  PVOID SecurityQualityOfService;
};
union native_status_or_pointer {
  LONG Status;
  PVOID Pointer;
};
struct native_io_status_block {
  native_status_or_pointer state;
  ULONG_PTR Information;
};
using nt_create_named_pipe = LONG (NTAPI *)(
  PHANDLE, ACCESS_MASK, native_object_attributes *,
  native_io_status_block *, ULONG, ULONG, ULONG, ULONG, ULONG, ULONG,
  ULONG, ULONG, ULONG, PLARGE_INTEGER);

struct native_handle {
  HANDLE value = nullptr;
  ~native_handle() {
    if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value);
  }
  native_handle(const native_handle &) = delete;
  native_handle &operator=(const native_handle &) = delete;
  native_handle() = default;
};

static int probe_native_nt_fifo() {
  HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
  if (!ntdll) return fail("GetModuleHandleW ntdll");
  auto create_pipe = reinterpret_cast<nt_create_named_pipe>(
    GetProcAddress(ntdll, "NtCreateNamedPipeFile"));
  if (!create_pipe) return fail("GetProcAddress NtCreateNamedPipeFile");

  const DWORD pid = GetCurrentProcessId();
  wchar_t native_path[128] = {};
  wchar_t win32_path[128] = {};
  if (swprintf(native_path, 128, L"\\Device\\NamedPipe\\git809-nt-%lu",
               static_cast<unsigned long>(pid)) < 0 ||
      swprintf(win32_path, 128, L"\\\\.\\pipe\\git809-nt-%lu",
               static_cast<unsigned long>(pid)) < 0) {
    std::fputs("FAIL native pipe path formatting\n", stderr);
    return 1;
  }
  native_unicode_string name = {
    static_cast<USHORT>(wcslen(native_path) * sizeof(wchar_t)),
    static_cast<USHORT>((wcslen(native_path) + 1) * sizeof(wchar_t)),
    native_path
  };
  native_object_attributes attributes = {
    sizeof(native_object_attributes), nullptr, &name, 0x40,
    nullptr, nullptr
  };
  native_io_status_block io = {};
  LARGE_INTEGER timeout = {};
  timeout.QuadPart = -500000;
  native_handle server;
  // Values match fifo.cc::create_pipe_instance(): FILE_OPEN_IF=3,
  // FILE_PIPE_MESSAGE_TYPE=1, REJECT_REMOTE_CLIENTS=2,
  // FILE_PIPE_MESSAGE_MODE=1, FILE_PIPE_QUEUE_OPERATION=0.
  LONG status = create_pipe(
    &server.value, GENERIC_READ | FILE_READ_ATTRIBUTES |
    FILE_WRITE_ATTRIBUTES | SYNCHRONIZE,
    &attributes, &io, FILE_SHARE_READ | FILE_SHARE_WRITE, 3, 0,
    1 | 2, 1, 0, 0xffffffffu, 65536, 65536, &timeout);
  std::printf("NT_CREATE status=0x%08lX\n",
              static_cast<unsigned long>(status));
  if (status < 0 || !server.value)
    return fail("NtCreateNamedPipeFile", static_cast<DWORD>(status));

  native_handle connect_event;
  connect_event.value = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!connect_event.value) return fail("native connect CreateEvent");
  OVERLAPPED connection = {};
  connection.hEvent = connect_event.value;
  BOOL connected = ConnectNamedPipe(server.value, &connection);
  DWORD connect_status = connected ? ERROR_SUCCESS : GetLastError();
  if (!connected && connect_status != ERROR_IO_PENDING &&
      connect_status != ERROR_PIPE_CONNECTED)
    return fail("native ConnectNamedPipe", connect_status);
  native_handle client;
  client.value = CreateFileW(win32_path, GENERIC_WRITE, 0, nullptr,
                             OPEN_EXISTING, 0, nullptr);
  if (client.value == INVALID_HANDLE_VALUE)
    return fail("native client CreateFileW");
  if (!connected && connect_status == ERROR_IO_PENDING) {
    if (WaitForSingleObject(connect_event.value, 3000) != WAIT_OBJECT_0)
      return fail("native connect wait", WAIT_TIMEOUT);
    DWORD bytes = 0;
    if (!GetOverlappedResult(server.value, &connection, &bytes, FALSE))
      return fail("native connect completion");
  }

  // Match fifo.cc::record_connection(): switch connected server
  // from FILE_PIPE_QUEUE_OPERATION to FILE_PIPE_COMPLETE_OPERATION.
  DWORD mode = PIPE_READMODE_MESSAGE | PIPE_NOWAIT;
  if (!SetNamedPipeHandleState(server.value, &mode, nullptr, nullptr))
    return fail("native switch to complete-operation");
  native_handle complete_event;
  complete_event.value = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!complete_event.value) return fail("native complete CreateEvent");
  OVERLAPPED immediate_op = {};
  immediate_op.hEvent = complete_event.value;
  char scratch = '\0';
  DWORD nbytes = 0;
  BOOL immediate = ReadFile(server.value, &scratch, 0, &nbytes,
                            &immediate_op);
  DWORD complete_status = immediate ? ERROR_SUCCESS : GetLastError();
  std::printf("NT_COMPLETE_EMPTY immediate=%u status=%lu\n",
              static_cast<unsigned>(immediate), complete_status);
  if (!immediate && complete_status == ERROR_IO_PENDING) {
    if (!CancelIoEx(server.value, &immediate_op) &&
        GetLastError() != ERROR_NOT_FOUND)
      return fail("native unexpected pending cancel");
    WaitForSingleObject(complete_event.value, 3000);
    DWORD ignored = 0;
    GetOverlappedResult(server.value, &immediate_op, &ignored, FALSE);
  }
  if (immediate || complete_status != ERROR_NO_DATA) {
    std::fprintf(stderr, "FAIL native complete-operation did not return"
                 " ERROR_NO_DATA: %lu\n", complete_status);
    return 2;
  }

  // Queue an asynchronous zero-length read on the *same* connected
  // NtCreateNamedPipeFile handle after switching to queued mode.
  mode = PIPE_READMODE_MESSAGE | PIPE_WAIT;
  if (!SetNamedPipeHandleState(server.value, &mode, nullptr, nullptr))
    return fail("native restore queue-operation");
  native_handle ready;
  ready.value = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!ready.value) return fail("native ready CreateEvent");
  OVERLAPPED zero = {};
  zero.hEvent = ready.value;
  nbytes = 0;
  immediate = ReadFile(server.value, &scratch, 0, &nbytes, &zero);
  DWORD queued_status = immediate ? ERROR_SUCCESS : GetLastError();
  std::printf("NT_QUEUE_EMPTY immediate=%u status=%lu\n",
              static_cast<unsigned>(immediate), queued_status);
  if (immediate || queued_status != ERROR_IO_PENDING)
    return fail("native empty zero-read not pending", queued_status);
  if (WaitForMultipleObjects(1, &ready.value, FALSE, 30) != WAIT_TIMEOUT) {
    CancelIoEx(server.value, &zero);
    WaitForSingleObject(ready.value, 3000);
    DWORD ignored = 0;
    GetOverlappedResult(server.value, &zero, &ignored, FALSE);
    std::fputs("FAIL native idle zero-read completed before write\n", stderr);
    return 3;
  }

  const char payload = 'N';
  DWORD sent = 0;
  if (!WriteFile(client.value, &payload, 1, &sent, nullptr) || sent != 1) {
    CancelIoEx(server.value, &zero);
    WaitForSingleObject(ready.value, 3000);
    DWORD ignored = 0;
    GetOverlappedResult(server.value, &zero, &ignored, FALSE);
    return fail("native client payload write");
  }
  if (WaitForMultipleObjects(1, &ready.value, FALSE, 3000) != WAIT_OBJECT_0) {
    CancelIoEx(server.value, &zero);
    WaitForSingleObject(ready.value, 3000);
    DWORD ignored = 0;
    GetOverlappedResult(server.value, &zero, &ignored, FALSE);
    return fail("native readiness wake", WAIT_TIMEOUT);
  }
  DWORD zero_count = 99;
  BOOL zero_ok = GetOverlappedResult(server.value, &zero, &zero_count, FALSE);
  DWORD zero_status = zero_ok ? ERROR_SUCCESS : GetLastError();
  std::printf("NT_ZERO_WAKE status=%lu consumed=%lu\n",
              zero_status, zero_count);
  if ((zero_status != ERROR_MORE_DATA && !zero_ok) || zero_count != 0) {
    std::fputs("FAIL native zero-read consumed data or failed\n", stderr);
    return 4;
  }

  // The actual payload must remain readable after the readiness event.
  native_handle payload_event;
  payload_event.value = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!payload_event.value) return fail("native payload CreateEvent");
  OVERLAPPED data = {};
  data.hEvent = payload_event.value;
  char got = '\0';
  DWORD received = 0;
  BOOL read_ok = ReadFile(server.value, &got, 1, &received, &data);
  if (!read_ok) {
    DWORD err = GetLastError();
    if (err != ERROR_IO_PENDING) return fail("native payload ReadFile", err);
    if (WaitForSingleObject(payload_event.value, 3000) != WAIT_OBJECT_0)
      return fail("native payload wait", WAIT_TIMEOUT);
    if (!GetOverlappedResult(server.value, &data, &received, FALSE))
      return fail("native payload completion");
  }
  if (received != 1 || got != payload) {
    std::fprintf(stderr, "FAIL native preserved byte n=%lu value=%u\n",
                 received, static_cast<unsigned>(got));
    return 5;
  }
  // The proposed FIFO reader must cancel and settle every outstanding
  // zero-byte I/O before handing off its native handle or freeing storage.
  native_handle cancel_event;
  cancel_event.value = CreateEventW(nullptr, TRUE, FALSE, nullptr);
  if (!cancel_event.value) return fail("native cancellation CreateEvent");
  OVERLAPPED cancel_read = {};
  cancel_read.hEvent = cancel_event.value;
  DWORD cancel_bytes = 0;
  BOOL cancel_now = ReadFile(server.value, &scratch, 0,
                             &cancel_bytes, &cancel_read);
  DWORD cancel_issue = cancel_now ? ERROR_SUCCESS : GetLastError();
  if (cancel_now || cancel_issue != ERROR_IO_PENDING)
    return fail("native empty read for cancellation", cancel_issue);
  if (!CancelIoEx(server.value, &cancel_read) &&
      GetLastError() != ERROR_NOT_FOUND)
    return fail("native CancelIoEx");
  if (WaitForSingleObject(cancel_event.value, 3000) != WAIT_OBJECT_0)
    return fail("native cancellation settlement timeout", WAIT_TIMEOUT);
  DWORD settled_bytes = 99;
  BOOL settled = GetOverlappedResult(server.value, &cancel_read,
                                     &settled_bytes, FALSE);
  DWORD cancel_result = settled ? ERROR_SUCCESS : GetLastError();
  std::printf("NT_CANCEL settled=%u status=%lu bytes=%lu\n",
              static_cast<unsigned>(settled), cancel_result, settled_bytes);
  if (settled || cancel_result != ERROR_OPERATION_ABORTED)
    return fail("native cancellation completion", cancel_result);

  // MSYS2 duplicates connected client handles during reader ownership
  // transfers. Verify a duplicate can set completion mode after the
  // pending read has settled, and that the original sees that mode.
  native_handle duplicate;
  if (!DuplicateHandle(GetCurrentProcess(), server.value,
                       GetCurrentProcess(), &duplicate.value, 0, FALSE,
                       DUPLICATE_SAME_ACCESS))
    return fail("native DuplicateHandle");
  mode = PIPE_READMODE_MESSAGE | PIPE_NOWAIT;
  if (!SetNamedPipeHandleState(duplicate.value, &mode, nullptr, nullptr))
    return fail("native duplicate NOWAIT");
  OVERLAPPED duplicate_check = {};
  DWORD duplicate_bytes = 0;
  BOOL duplicate_now = ReadFile(server.value, &scratch, 0,
                                &duplicate_bytes, &duplicate_check);
  DWORD duplicate_status = duplicate_now ? ERROR_SUCCESS : GetLastError();
  std::printf("NT_DUPLICATE_MODE immediate=%u status=%lu\n",
              static_cast<unsigned>(duplicate_now), duplicate_status);
  if (duplicate_now || duplicate_status != ERROR_NO_DATA)
    return fail("native duplicate mode not shared", duplicate_status);
  std::puts("PASS native NtCreateNamedPipeFile complete->queue"
            " zero-byte wake, no consumed data");
  return 0;
}
