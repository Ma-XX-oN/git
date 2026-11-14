/* RWF 809: Dobby instrumentation for the SAME native Win32/NT benchmark.
 * Only one layer's hooks may exist at once; failure is fatal.
 * Include once after the NT API typedefs and nt_network_info definition.
 */
#ifndef RWF809_WIN32_NT_DOBBY_H
#define RWF809_WIN32_NT_DOBBY_H
static void check(int ok, const char *message);
static double ms(unsigned long long ticks);
typedef int (*dobby_hook_t)(void *, void *, void **);
typedef int (*dobby_destroy_t)(void *);
enum { WIN32_HOOK_COUNT = 4, NT_HOOK_COUNT = 4, HOOK_BYTES = 32 };
struct hook_record {
  const char *name;
  void *target;
  void *replacement;
  void **trampoline;
  unsigned char original[HOOK_BYTES];
  volatile LONG64 calls;
  volatile LONG64 ticks;
};
static struct hook_record hooks[4];
static unsigned current_hook_count;
static dobby_hook_t dobby_hook;
static dobby_destroy_t dobby_destroy;
static const char *active_hook_layer;
static const char *benchmark_phase;
static unsigned long long hook_clock(void)
{
  LARGE_INTEGER x;
  check(QueryPerformanceCounter(&x), "QPC in Dobby hook");
  return (unsigned long long)x.QuadPart;
}
static void hook_account(unsigned which, unsigned long long start)
{
  unsigned long long elapsed = hook_clock() - start;
  InterlockedExchangeAdd64(&hooks[which].ticks, (LONG64)elapsed);
  InterlockedIncrement64(&hooks[which].calls);
}
typedef HANDLE (WINAPI *win_open_t)(LPCWSTR, DWORD, DWORD,
                                  LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
typedef BOOL (WINAPI *win_read_t)(HANDLE, LPVOID, DWORD,
                                  LPDWORD, LPOVERLAPPED);
typedef BOOL (WINAPI *win_close_t)(HANDLE);
typedef BOOL (WINAPI *win_meta_t)(LPCWSTR, GET_FILEEX_INFO_LEVELS, LPVOID);
static win_open_t orig_win_open;
static win_read_t orig_win_read;
static win_close_t orig_win_close;
static win_meta_t orig_win_meta;
static nt_create_t orig_nt_create;
static nt_read_t orig_nt_read;
static nt_close_t orig_nt_close;
static nt_meta_t orig_nt_meta;

static HANDLE WINAPI traced_win_open(LPCWSTR path, DWORD access, DWORD share,
                 LPSECURITY_ATTRIBUTES security, DWORD disposition,
                 DWORD flags, HANDLE templ)
{
  unsigned long long begin = hook_clock();
  HANDLE result = orig_win_open(path, access, share, security, disposition,
                                flags, templ);
  DWORD last = GetLastError();
  hook_account(0, begin);
  SetLastError(last);
  return result;
}
static BOOL WINAPI traced_win_read(HANDLE h, LPVOID buf, DWORD size,
                                   LPDWORD got, LPOVERLAPPED ov)
{
  unsigned long long begin = hook_clock();
  BOOL result = orig_win_read(h, buf, size, got, ov);
  DWORD last = GetLastError();
  hook_account(1, begin);
  SetLastError(last);
  return result;
}
static BOOL WINAPI traced_win_close(HANDLE h)
{
  unsigned long long begin = hook_clock();
  BOOL result = orig_win_close(h);
  DWORD last = GetLastError();
  hook_account(2, begin);
  SetLastError(last);
  return result;
}
static BOOL WINAPI traced_win_meta(LPCWSTR path, GET_FILEEX_INFO_LEVELS type,
                                   LPVOID info)
{
  unsigned long long begin = hook_clock();
  BOOL result = orig_win_meta(path, type, info);
  DWORD last = GetLastError();
  hook_account(3, begin);
  SetLastError(last);
  return result;
}
static NTSTATUS NTAPI traced_nt_create(PHANDLE h, ACCESS_MASK access,
     POBJECT_ATTRIBUTES attrs, PIO_STATUS_BLOCK iosb, PLARGE_INTEGER alloc,
     ULONG file_attrs, ULONG share, ULONG disposition, ULONG options,
     PVOID ea, ULONG ea_len)
{
  unsigned long long begin = hook_clock();
  NTSTATUS result = orig_nt_create(h, access, attrs, iosb, alloc,
       file_attrs, share, disposition, options, ea, ea_len);
  hook_account(0, begin);
  return result;
}
static NTSTATUS NTAPI traced_nt_read(HANDLE h, HANDLE event, PVOID apc,
     PVOID context, PIO_STATUS_BLOCK iosb, PVOID buf, ULONG len,
     PLARGE_INTEGER off, PULONG key)
{
  unsigned long long begin = hook_clock();
  NTSTATUS result = orig_nt_read(h, event, apc, context, iosb, buf,
                                 len, off, key);
  hook_account(1, begin);
  return result;
}
static NTSTATUS NTAPI traced_nt_close(HANDLE h)
{
  unsigned long long begin = hook_clock();
  NTSTATUS result = orig_nt_close(h);
  hook_account(2, begin);
  return result;
}
static NTSTATUS NTAPI traced_nt_meta(POBJECT_ATTRIBUTES attrs,
                                   struct nt_network_info *info)
{
  unsigned long long begin = hook_clock();
  NTSTATUS result = orig_nt_meta(attrs, info);
  hook_account(3, begin);
  return result;
}
static void load_dobby(void)
{
  HMODULE library;
  const char *filename = getenv("RWF809_DOBBY_DLL");
  check(filename && filename[0], "Dobby DLL path provided");
  library = LoadLibraryA(filename);
  check(library != NULL, "load pinned native Dobby DLL");
  dobby_hook = (dobby_hook_t)GetProcAddress(library, "DobbyHook");
  dobby_destroy = (dobby_destroy_t)GetProcAddress(library, "DobbyDestroy");
  check(dobby_hook && dobby_destroy, "resolve DobbyHook and DobbyDestroy");
}
static void assign_hook(unsigned index, const char *name, void *target,
                        void *replacement, void **trampoline)
{
  struct hook_record *rec = &hooks[index];
  check(index < 4 && target && replacement && trampoline,
        "hook registration");
  rec->name = name;
  rec->target = target;
  rec->replacement = replacement;
  rec->trampoline = trampoline;
  rec->calls = rec->ticks = 0;
  memcpy(rec->original, target, HOOK_BYTES);
}
static void install_hook_set(int native)
{
  HMODULE dll = GetModuleHandleW(native ? L"ntdll.dll" : L"kernel32.dll");
  check(active_hook_layer == NULL && current_hook_count == 0,
        "prior hooks completely removed before next install");
  check(dll != NULL, "API module exists");
#define SET_HOOK(index, symbol, fun, original) \
  assign_hook(index, symbol, (void *)GetProcAddress(dll, symbol), \
              (void *)fun, (void **)&original)
  if (native) {
    SET_HOOK(0, "NtCreateFile", traced_nt_create, orig_nt_create);
    SET_HOOK(1, "NtReadFile", traced_nt_read, orig_nt_read);
    SET_HOOK(2, "NtClose", traced_nt_close, orig_nt_close);
    SET_HOOK(3, "NtQueryFullAttributesFile", traced_nt_meta, orig_nt_meta);
  } else {
    SET_HOOK(0, "CreateFileW", traced_win_open, orig_win_open);
    SET_HOOK(1, "ReadFile", traced_win_read, orig_win_read);
    SET_HOOK(2, "CloseHandle", traced_win_close, orig_win_close);
    SET_HOOK(3, "GetFileAttributesExW", traced_win_meta, orig_win_meta);
  }
#undef SET_HOOK
  /* Reject overlapping hook targets, including forwarded export aliases. */
  {
    unsigned i, j;
    for (i = 0; i < 4; i++)
      for (j = i + 1; j < 4; j++)
        check(hooks[i].target != hooks[j].target, "distinct hook addresses");
  }
  /* Install only while no benchmark worker threads are running. */
  {
    unsigned i;
    for (i = 0; i < 4; i++) {
      check(dobby_hook(hooks[i].target, hooks[i].replacement,
                       hooks[i].trampoline) == 0, "DobbyHook installation");
      ++current_hook_count;
      check(*hooks[i].trampoline != NULL, "Dobby returned trampoline");
    }
  }
  active_hook_layer = native ? "nt" : "win32";
  printf("RWF809_DOBBY_LIFECYCLE installed,%s,4\n", active_hook_layer);
}
static void uninstall_hook_set(void)
{
  unsigned i;
  check(active_hook_layer != NULL && current_hook_count == 4,
        "complete hook set installed");
  for (i = 0; i < 4; i++) {
    struct hook_record *r = &hooks[i];
    LONG64 calls = InterlockedCompareExchange64(&r->calls, 0, 0);
    LONG64 ticks = InterlockedCompareExchange64(&r->ticks, 0, 0);
    check(calls > 0 && ticks > 0, "hook actually intercepted calls");
    printf("RWF809_DOBBY_HOOK %s,%s,%lld,%.6f\n",
           active_hook_layer, r->name, (long long)calls,
           ms((unsigned long long)ticks));
  }
  for (i = current_hook_count; i > 0; i--) {
    struct hook_record *r = &hooks[i - 1];
    check(dobby_destroy(r->target) == 0, "DobbyDestroy successful");
    check(FlushInstructionCache(GetCurrentProcess(), r->target, HOOK_BYTES),
          "FlushInstructionCache");
    check(memcmp(r->target, r->original, HOOK_BYTES) == 0,
          "original 32 instruction bytes restored after DobbyDestroy");
    --current_hook_count;
  }
  printf("RWF809_DOBBY_LIFECYCLE restored,%s,4\n", active_hook_layer);
  active_hook_layer = NULL;
}
#endif
