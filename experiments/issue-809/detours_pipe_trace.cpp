// RWF #809 - process-local, path-filtered NT handle lifecycle trace.
// Diagnostic only. Never changes native API arguments or return values.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winternl.h>
#include <intrin.h>
#include <detours.h>
#include <stdio.h>

using NtCreateFileFn = NTSTATUS (NTAPI *)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES,
  PIO_STATUS_BLOCK, PLARGE_INTEGER, ULONG, ULONG, ULONG, ULONG, PVOID, ULONG);
using NtOpenFileFn = NTSTATUS (NTAPI *)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES,
  PIO_STATUS_BLOCK, ULONG, ULONG);
// Windows SDK winternl.h does not provide a PFILE_BASIC_INFORMATION alias.
struct Git809BasicInfo {
  LARGE_INTEGER CreationTime;
  LARGE_INTEGER LastAccessTime;
  LARGE_INTEGER LastWriteTime;
  LARGE_INTEGER ChangeTime;
  ULONG FileAttributes;
};
static_assert(sizeof(Git809BasicInfo) == 40, "NT basic information layout changed");
using NtQueryAttributesFileFn = NTSTATUS (NTAPI *)(POBJECT_ATTRIBUTES, Git809BasicInfo*);
using NtCloseFn = NTSTATUS (NTAPI *)(HANDLE);

static NtCreateFileFn real_create = nullptr;
static NtOpenFileFn real_open = nullptr;
static NtQueryAttributesFileFn real_query = nullptr;
static NtCloseFn real_close = nullptr;
static WCHAR target[260] = {};
static WCHAR logfile[MAX_PATH * 2] = {};
static SRWLOCK handle_lock = SRWLOCK_INIT;
static HANDLE watched[512] = {};
static thread_local bool reentrant = false;
static volatile LONG events = 0;

static bool matches(POBJECT_ATTRIBUTES a) {
  if (!a || !a->ObjectName || !a->ObjectName->Buffer || !target[0]) return false;
  const auto* s = a->ObjectName;
  const size_t n = s->Length / sizeof(WCHAR);
  const size_t m = wcslen(target);
  if (m == 0 || n < m) return false;
  // Strictly match the final pipe component, including relative Nt object paths.
  if (CompareStringOrdinal(s->Buffer + n - m, (int)m, target, (int)m, TRUE) != CSTR_EQUAL) return false;
  return n == m || s->Buffer[n-m-1] == L'\\' || s->Buffer[n-m-1] == L'/';
}
static bool tracked(HANDLE h) {
  AcquireSRWLockShared(&handle_lock);
  bool ok = false;
  for (HANDLE v : watched) if (v == h && h) { ok = true; break; }
  ReleaseSRWLockShared(&handle_lock);
  return ok;
}
static void add(HANDLE h) {
  if (!h || h == INVALID_HANDLE_VALUE) return;
  AcquireSRWLockExclusive(&handle_lock);
  for (HANDLE v : watched) if (v == h) { ReleaseSRWLockExclusive(&handle_lock); return; }
  for (HANDLE &v : watched) if (!v) { v = h; break; }
  ReleaseSRWLockExclusive(&handle_lock);
}
static void remove(HANDLE h) {
  AcquireSRWLockExclusive(&handle_lock);
  for (HANDLE &v : watched) if (v == h) { v = nullptr; break; }
  ReleaseSRWLockExclusive(&handle_lock);
}
static void emit(const char* op, NTSTATUS status, HANDLE handle, ACCESS_MASK access, ULONG opts,
                 POBJECT_ATTRIBUTES attrs) {
  if (reentrant || !logfile[0]) return;
  reentrant = true;
  // Do not rely on CRT streams from DllMain. The reentrancy guard prevents the
  // logger's own Win32 file operation from becoming a watched NtCreateFile call.
  char path[600] = "(no-path)";
  if (attrs && attrs->ObjectName && attrs->ObjectName->Buffer) {
    int chars = attrs->ObjectName->Length / sizeof(WCHAR);
    int count = WideCharToMultiByte(CP_UTF8, 0, attrs->ObjectName->Buffer,
                                   chars, path, (int)sizeof(path)-1, nullptr, nullptr);
    if (count > 0) path[count] = 0;
    else path[0] = 0;
  }
  char line[1500];
  void* stack[8] = {};
  USHORT frames = CaptureStackBackTrace(1, 8, stack, nullptr);
  DWORD tid = GetCurrentThreadId();
  int length = _snprintf_s(line, sizeof(line), _TRUNCATE,
    "GIT809_DETOURS seq=%ld tick=%llu pid=%lu tid=%lu op=%s status=0x%08lx "
    "handle=%p access=0x%08lx opts=0x%08lx root=%p runtime_base=%p path=%s stack=",
    InterlockedIncrement(&events), GetTickCount64(), GetCurrentProcessId(), tid,
    op, (unsigned long)status, handle, (unsigned long)access, (unsigned long)opts,
    attrs ? attrs->RootDirectory : nullptr,
    GetModuleHandleW(L"msys-2.0.dll"), path);
  if (length > 0) {
    for (USHORT i = 0; i < frames && length < (int)sizeof(line)-40; ++i) {
      int n = _snprintf_s(line+length, sizeof(line)-length, _TRUNCATE, "%p,", stack[i]);
      if (n <= 0) break;
      length += n;
    }
    if (length < (int)sizeof(line)-3) {
      line[length++]='\r'; line[length++]='\n';
      HANDLE out = CreateFileW(logfile, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                               nullptr, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
      if (out != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(out, line, (DWORD)length, &written, nullptr);
        CloseHandle(out);
      }
    }
  }
  reentrant = false;
}
static NTSTATUS NTAPI hook_create(PHANDLE out, ACCESS_MASK access, POBJECT_ATTRIBUTES a,
  PIO_STATUS_BLOCK io, PLARGE_INTEGER size, ULONG attr, ULONG share, ULONG disposition,
  ULONG options, PVOID ea, ULONG ea_len) {
  if (reentrant) return real_create(out,access,a,io,size,attr,share,disposition,options,ea,ea_len);
  const bool hit = matches(a);
  if (hit) emit("NtCreateFile_BEGIN", 0, nullptr, access, options, a);
  NTSTATUS status = real_create(out,access,a,io,size,attr,share,disposition,options,ea,ea_len);
  if (hit) {
    HANDLE h = NT_SUCCESS(status) && out ? *out : nullptr;
    if (h) add(h);
    emit("NtCreateFile_END",status,h,access,options,a);
  }
  return status;
}
static NTSTATUS NTAPI hook_open(PHANDLE out, ACCESS_MASK access, POBJECT_ATTRIBUTES a,
  PIO_STATUS_BLOCK io, ULONG share, ULONG options) {
  if (reentrant) return real_open(out,access,a,io,share,options);
  const bool hit = matches(a);
  if (hit) emit("NtOpenFile_BEGIN",0,nullptr,access,options,a);
  NTSTATUS status = real_open(out,access,a,io,share,options);
  if (hit) {
    HANDLE h = NT_SUCCESS(status) && out ? *out : nullptr;
    if (h) add(h);
    emit("NtOpenFile_END",status,h,access,options,a);
  }
  return status;
}
static NTSTATUS NTAPI hook_query(POBJECT_ATTRIBUTES a, Git809BasicInfo* info) {
  if (reentrant) return real_query(a,info);
  const bool hit = matches(a);
  if (hit) emit("NtQueryAttributesFile_BEGIN",0,nullptr,0,0,a);
  NTSTATUS status = real_query(a,info);
  if (hit) emit("NtQueryAttributesFile_END",status,nullptr,0,
                NT_SUCCESS(status) && info ? info->FileAttributes : 0,a);
  return status;
}
static NTSTATUS NTAPI hook_close(HANDLE h) {
  if (reentrant) return real_close(h);
  const bool hit = tracked(h);
  if (hit) emit("NtClose_BEGIN",0,h,0,0,nullptr);
  NTSTATUS status = real_close(h);
  if (hit) {
    if (NT_SUCCESS(status)) remove(h);
    emit("NtClose_END",status,h,0,0,nullptr);
  }
  return status;
}
extern "C" BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
  if (DetourIsHelperProcess()) return TRUE;
  if (reason == DLL_PROCESS_ATTACH) {
    DisableThreadLibraryCalls(module);
    GetEnvironmentVariableW(L"GIT809_PIPE_LEAF",target,260);
    GetEnvironmentVariableW(L"GIT809_DETOURS_LOG",logfile,MAX_PATH*2);
    DetourRestoreAfterWith();
    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    if (!ntdll || !target[0] || !logfile[0]) return FALSE;
    real_create = reinterpret_cast<NtCreateFileFn>(GetProcAddress(ntdll,"NtCreateFile"));
    real_open = reinterpret_cast<NtOpenFileFn>(GetProcAddress(ntdll,"NtOpenFile"));
    real_query = reinterpret_cast<NtQueryAttributesFileFn>(GetProcAddress(ntdll,"NtQueryAttributesFile"));
    real_close = reinterpret_cast<NtCloseFn>(GetProcAddress(ntdll,"NtClose"));
    if (!real_create || !real_open || !real_query || !real_close) return FALSE;
    if (DetourTransactionBegin() != NO_ERROR) return FALSE;
    DetourUpdateThread(GetCurrentThread());
    LONG error = DetourAttach(reinterpret_cast<PVOID*>(&real_create),hook_create);
    if (error == NO_ERROR) error = DetourAttach(reinterpret_cast<PVOID*>(&real_open),hook_open);
    if (error == NO_ERROR) error = DetourAttach(reinterpret_cast<PVOID*>(&real_query),hook_query);
    if (error == NO_ERROR) error = DetourAttach(reinterpret_cast<PVOID*>(&real_close),hook_close);
    if (error != NO_ERROR) { DetourTransactionAbort(); return FALSE; }
    if (DetourTransactionCommit() != NO_ERROR) return FALSE;
    emit("HOOK_READY",0,nullptr,0,0,nullptr);
  }
  // Process exit reclaims trampoline state. Avoid I/O or detachment under loader lock.
  return TRUE;
}
