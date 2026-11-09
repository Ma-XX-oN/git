// RWF 809: isolated, diagnostic-only process termination Detours tracer.
// Hook the process-local Win32 and CRT exit entrypoints without changing them.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <detours.h>
#include <stdio.h>
#include <wchar.h>

using ExitProcessFn = VOID (WINAPI *)(UINT);
using CRunFn = void (__cdecl *)(int);
using AbortFn = void (__cdecl *)(void);
using RaiseFn = int (__cdecl *)(int);
using SignalHandler = void (__cdecl *)(int);
using SignalFn = SignalHandler (__cdecl *)(int, SignalHandler);

static ExitProcessFn real_exit_process = nullptr;
static CRunFn real_crt_exit = nullptr;
static CRunFn real_crt__exit = nullptr;
static AbortFn real_abort = nullptr;
static RaiseFn real_raise = nullptr;
static SignalFn real_signal = nullptr;
static wchar_t log_path[MAX_PATH * 4] = {};
static wchar_t fallback_path[MAX_PATH * 4] = {};
static thread_local bool logging = false;

static void emit(const char *name, int code)
{
  if (logging)
    return;
  logging = true;
  char line[320];
  const int count = _snprintf_s(line, sizeof(line), _TRUNCATE,
    "GIT809_EXIT_DETOURS pid=%lu tid=%lu tick=%llu path=%s code=%d\r\n",
    GetCurrentProcessId(), GetCurrentThreadId(), GetTickCount64(), name, code);
  if (count > 0) {
    const wchar_t *paths[] = { log_path, fallback_path };
    for (const wchar_t *path : paths) {
      if (!path[0])
        continue;
      HANDLE file = CreateFileW(path, FILE_APPEND_DATA,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
      if (file == INVALID_HANDLE_VALUE)
        continue;
      DWORD written = 0;
      const BOOL ok = WriteFile(file, line, static_cast<DWORD>(count),
                                &written, nullptr);
      const DWORD error = ok && written == static_cast<DWORD>(count) ? 0 :
                          GetLastError();
      CloseHandle(file);
      if (!error)
        break;
    }
  }
  logging = false;
}

static VOID WINAPI hook_exit_process(UINT code)
{
  emit("ExitProcess", static_cast<int>(code));
  real_exit_process(code);
}

static void __cdecl hook_crt_exit(int code)
{
  emit("crt.exit", code);
  real_crt_exit(code);
}

static void __cdecl hook_crt__exit(int code)
{
  emit("crt._exit", code);
  real_crt__exit(code);
}

static void __cdecl hook_abort(void)
{
  emit("crt.abort", -1);
  real_abort();
}

static int __cdecl hook_raise(int signal_number)
{
  emit("crt.raise", signal_number);
  return real_raise(signal_number);
}

static SignalHandler __cdecl hook_signal(int sig, SignalHandler handler)
{
  emit("crt.signal-registration", sig);
  return real_signal(sig, handler);
}

static bool attach(PVOID *original, PVOID hook)
{
  return DetourAttach(original, hook) == NO_ERROR;
}

extern "C" BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID)
{
  if (DetourIsHelperProcess())
    return TRUE;
  if (reason != DLL_PROCESS_ATTACH)
    return TRUE;
  DisableThreadLibraryCalls(module);
  DetourRestoreAfterWith();
  GetEnvironmentVariableW(L"GIT809_DETOURS_LOG", log_path,
                          static_cast<DWORD>(sizeof(log_path) / sizeof(log_path[0])));
  GetEnvironmentVariableW(L"GIT809_DETOURS_FALLBACK", fallback_path,
                          static_cast<DWORD>(sizeof(fallback_path) / sizeof(fallback_path[0])));
  if (!log_path[0] && !fallback_path[0])
    return FALSE;

  HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
  real_exit_process = kernel ?
    reinterpret_cast<ExitProcessFn>(GetProcAddress(kernel, "ExitProcess")) :
    nullptr;
  HMODULE crt = GetModuleHandleW(L"msvcrt.dll");
  if (!crt)
    crt = GetModuleHandleW(L"ucrtbase.dll");
  real_crt_exit = crt ?
    reinterpret_cast<CRunFn>(GetProcAddress(crt, "exit")) : nullptr;
  real_crt__exit = crt ?
    reinterpret_cast<CRunFn>(GetProcAddress(crt, "_exit")) : nullptr;
  real_abort = crt ?
    reinterpret_cast<AbortFn>(GetProcAddress(crt, "abort")) : nullptr;
  real_raise = crt ?
    reinterpret_cast<RaiseFn>(GetProcAddress(crt, "raise")) : nullptr;
  real_signal = crt ?
    reinterpret_cast<SignalFn>(GetProcAddress(crt, "signal")) : nullptr;

  if (!real_exit_process || !real_crt_exit || !real_crt__exit)
    return FALSE;
  if (DetourTransactionBegin() != NO_ERROR)
    return FALSE;
  DetourUpdateThread(GetCurrentThread());
  bool ok = attach(reinterpret_cast<PVOID*>(&real_exit_process),
                   reinterpret_cast<PVOID>(hook_exit_process)) &&
            attach(reinterpret_cast<PVOID*>(&real_crt_exit),
                   reinterpret_cast<PVOID>(hook_crt_exit)) &&
            attach(reinterpret_cast<PVOID*>(&real_crt__exit),
                   reinterpret_cast<PVOID>(hook_crt__exit));
  if (ok && real_abort)
    ok = attach(reinterpret_cast<PVOID*>(&real_abort),
                reinterpret_cast<PVOID>(hook_abort));
  if (ok && real_raise)
    ok = attach(reinterpret_cast<PVOID*>(&real_raise),
                reinterpret_cast<PVOID>(hook_raise));
  if (ok && real_signal)
    ok = attach(reinterpret_cast<PVOID*>(&real_signal),
                reinterpret_cast<PVOID>(hook_signal));
  if (!ok) {
    DetourTransactionAbort();
    return FALSE;
  }
  if (DetourTransactionCommit() != NO_ERROR)
    return FALSE;
  emit("HOOK_READY", 0);
  return TRUE;
}
