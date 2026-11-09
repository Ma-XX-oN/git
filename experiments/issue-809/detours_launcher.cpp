// RWF #809 - launch stock Git Bash with Detours-injected tracing DLL.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <detours.h>
#include <string>
#include <stdio.h>
#include <vector>

static std::wstring quote(const wchar_t* text) {
  std::wstring result = L"\"";
  int slash = 0;
  for (const wchar_t* p = text; *p; ++p) {
    if (*p == L'\\') { ++slash; continue; }
    if (*p == L'"') { result.append(slash*2+1,L'\\'); result += L'"'; slash=0; continue; }
    result.append(slash,L'\\'); slash=0; result += *p;
  }
  result.append(slash*2,L'\\');
  result += L'"';
  return result;
}
int wmain(int argc, wchar_t** argv) {
  if (argc < 4) {
    fwprintf(stderr,L"usage: detours_launcher.exe <trace.dll> <child.exe> <args...>\n");
    return 2;
  }
  char dll[MAX_PATH*3];
  BOOL used_default = FALSE;
  int n = WideCharToMultiByte(CP_ACP,WC_NO_BEST_FIT_CHARS,argv[1],-1,
                              dll,sizeof(dll),nullptr,&used_default);
  if (!n || used_default) {
    fwprintf(stderr,L"DLL path must be representable in ANSI without substitution\n");
    return 3;
  }
  std::wstring command;
  for (int i=2; i<argc; ++i) {
    if (i>2) command += L' ';
    command += quote(argv[i]);
  }
  std::vector<wchar_t> writable(command.begin(), command.end());
  writable.push_back(0);
  STARTUPINFOW si = {};
  si.cb=sizeof(si);
  si.dwFlags=STARTF_USESTDHANDLES;
  si.hStdInput=GetStdHandle(STD_INPUT_HANDLE);
  si.hStdOutput=GetStdHandle(STD_OUTPUT_HANDLE);
  si.hStdError=GetStdHandle(STD_ERROR_HANDLE);
  PROCESS_INFORMATION pi = {};
  if (!DetourCreateProcessWithDllExW(argv[2],writable.data(),nullptr,nullptr,
        TRUE,CREATE_SUSPENDED,nullptr,nullptr,&si,&pi,dll,nullptr)) {
    fprintf(stderr,"GIT809_DETOURS_LAUNCH_FAIL winerr=%lu\n",GetLastError());
    return 4;
  }
  fprintf(stderr,"GIT809_DETOURS_INJECTED pid=%lu\n",pi.dwProcessId);
  ResumeThread(pi.hThread);
  WaitForSingleObject(pi.hProcess,INFINITE);
  DWORD result = 0;
  if (!GetExitCodeProcess(pi.hProcess,&result)) result=5;
  CloseHandle(pi.hThread);
  CloseHandle(pi.hProcess);
  return (int)result;
}
