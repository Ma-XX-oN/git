#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define TIMEOUT_MS 20000
#define POOL_SIZE 3
#define MAGIC 0x809CAFE1u
typedef struct { volatile LONG value; volatile LONG answer; } Shared;
typedef struct { HANDLE pipe; PROCESS_INFORMATION process; char endpoint[128]; } Worker;
static char executable[MAX_PATH];
static Worker workers[POOL_SIZE];

static void fail(const char *where) {
  fprintf(stderr,"%s: Windows error %lu\n",where,GetLastError());
  ExitProcess(98);
}
static void make_name(char *dst, size_t cap, const char *kind, const char *token) {
  int n = snprintf(dst, cap, "\\\\.\\pipe\\git809-%s-%s",kind,token);
  if (n < 0 || (size_t)n >= cap) ExitProcess(97);
}
static HANDLE open_pipe(const char *name) {
  HANDLE h = INVALID_HANDLE_VALUE;
  for (int i=0;i<500;i++) {
    h=CreateFileA(name,GENERIC_READ|GENERIC_WRITE,0,NULL,OPEN_EXISTING,0,NULL);
    if (h!=INVALID_HANDLE_VALUE) return h;
    DWORD err=GetLastError();
    if (err!=ERROR_PIPE_BUSY && err!=ERROR_FILE_NOT_FOUND) fail("connect pipe");
    Sleep(2);
  }
  fail("pipe connection timeout");
  return INVALID_HANDLE_VALUE;
}
static void transfer(HANDLE h, void *data, DWORD size, int write_flag) {
  DWORD done=0;
  BOOL ok=write_flag ? WriteFile(h,data,size,&done,NULL)
                     : ReadFile(h,data,size,&done,NULL);
  if(!ok || done!=size) fail(write_flag?"pipe write":"pipe read");
}
static void start_worker(int i,const char *token) {
  Worker *w=&workers[i];
  int n=snprintf(w->endpoint,sizeof(w->endpoint),
    "\\\\.\\pipe\\git809-worker-%s-%d",token,i);
  if(n<0 || (size_t)n>=sizeof(w->endpoint)) ExitProcess(97);
  w->pipe=CreateNamedPipeA(w->endpoint,PIPE_ACCESS_DUPLEX,
    PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,1,4096,4096,TIMEOUT_MS,NULL);
  if(w->pipe==INVALID_HANDLE_VALUE) fail("worker pipe create");
  char line[2*MAX_PATH];
  n=snprintf(line,sizeof(line),"\"%s\" worker \"%s\"",executable,w->endpoint);
  if(n<0 || (size_t)n>=sizeof(line)) ExitProcess(97);
  STARTUPINFOA si={0};si.cb=sizeof(si);
  ZeroMemory(&w->process,sizeof(w->process));
  if(!CreateProcessA(executable,line,NULL,NULL,FALSE,
        CREATE_NO_WINDOW,NULL,NULL,&si,&w->process)) fail("worker launch");
  BOOL connected=ConnectNamedPipe(w->pipe,NULL);
  if(!connected && GetLastError()!=ERROR_PIPE_CONNECTED) fail("worker connect");
}
static void stop_worker(int i) {
  Worker *w=&workers[i];
  DWORD wait=WaitForSingleObject(w->process.hProcess,TIMEOUT_MS);
  if(wait!=WAIT_OBJECT_0) fail("worker exit wait");
  DWORD code=0;
  if(!GetExitCodeProcess(w->process.hProcess,&code) || code!=0) fail("worker exit");
  CloseHandle(w->pipe);
  CloseHandle(w->process.hThread);
  CloseHandle(w->process.hProcess);
}
static uint32_t dispatch(uint32_t request,int *next,const char *token) {
  int i=(*next)++%POOL_SIZE;
  uint32_t reply=0;
  transfer(workers[i].pipe,&request,sizeof(request),1);
  transfer(workers[i].pipe,&reply,sizeof(reply),0);
  if(reply!=(request^MAGIC)) { fputs("worker mismatch\n",stderr);ExitProcess(96); }
  stop_worker(i);
  return reply;
}
static void run_worker(const char *endpoint) {
  HANDLE h=open_pipe(endpoint);
  uint32_t req=0,answer=0;
  transfer(h,&req,sizeof(req),0);
  answer=req^MAGIC;
  transfer(h,&answer,sizeof(answer),1);
  FlushFileBuffers(h);
  CloseHandle(h);
}
static void run_manager_pipe(const char *token) {
  char endpoint[128];
  make_name(endpoint,sizeof(endpoint),"mgr",token);
  for(int i=0;i<POOL_SIZE;i++) start_worker(i,token);
  puts("READY");fflush(stdout);
  int next=0;
  for(;;) {
    HANDLE h=CreateNamedPipeA(endpoint,PIPE_ACCESS_DUPLEX,
      PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,1,4096,4096,TIMEOUT_MS,NULL);
    if(h==INVALID_HANDLE_VALUE) fail("manager pipe");
    BOOL connected=ConnectNamedPipe(h,NULL);
    if(!connected && GetLastError()!=ERROR_PIPE_CONNECTED) fail("manager connect");
    uint32_t req=0;
    DWORD got=0;
    BOOL ok=ReadFile(h,&req,sizeof(req),&got,NULL);
    if(ok && got==sizeof(req)) {
      uint32_t reply=dispatch(req,&next,token);
      transfer(h,&reply,sizeof(reply),1);
      FlushFileBuffers(h);
      start_worker((next-1)%POOL_SIZE,token);
    }
    DisconnectNamedPipe(h);CloseHandle(h);
  }
}
static void shm_name(char *dst,size_t cap,const char *type,const char *token) {
  int n=snprintf(dst,cap,"Local\\Git809-%s-%s",type,token);
  if(n<0 || (size_t)n>=cap) ExitProcess(97);
}
static void run_manager_shm(const char *token) {
  char mapname[128],reqname[128],resname[128];
  shm_name(mapname,sizeof(mapname),"map",token);
  shm_name(reqname,sizeof(reqname),"req",token);
  shm_name(resname,sizeof(resname),"res",token);
  HANDLE mapping=CreateFileMappingA(INVALID_HANDLE_VALUE,NULL,PAGE_READWRITE,
       0,sizeof(Shared),mapname);
  if(!mapping) fail("manager map");
  Shared *s=(Shared*)MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared));
  if(!s) fail("manager map view");
  HANDLE req=CreateEventA(NULL,FALSE,FALSE,reqname);
  HANDLE res=CreateEventA(NULL,FALSE,FALSE,resname);
  if(!req||!res) fail("manager event");
  s->value=0;s->answer=0;
  for(int i=0;i<POOL_SIZE;i++) start_worker(i,token);
  puts("READY");fflush(stdout);
  int next=0;
  for(;;) {
    if(WaitForSingleObject(req,INFINITE)!=WAIT_OBJECT_0) fail("req wait");
    uint32_t v=(uint32_t)s->value;
    uint32_t answer=dispatch(v,&next,token);
    s->answer=(LONG)answer;
    MemoryBarrier();
    if(!SetEvent(res)) fail("set response event");
    start_worker((next-1)%POOL_SIZE,token);
  }
}
static int run_client(const char *mode,const char *token) {
  uint32_t req=0x1A2B3C4Du,answer=0;
  if(strcmp(mode,"pipe")==0) {
    char endpoint[128];make_name(endpoint,sizeof(endpoint),"mgr",token);
    HANDLE h=open_pipe(endpoint);
    transfer(h,&req,sizeof(req),1);
    transfer(h,&answer,sizeof(answer),0);
    CloseHandle(h);
  } else {
    char mapname[128],reqname[128],resname[128];
    shm_name(mapname,sizeof(mapname),"map",token);
    shm_name(reqname,sizeof(reqname),"req",token);
    shm_name(resname,sizeof(resname),"res",token);
    HANDLE mapping=OpenFileMappingA(FILE_MAP_ALL_ACCESS,FALSE,mapname);
    HANDLE request=OpenEventA(EVENT_MODIFY_STATE,FALSE,reqname);
    HANDLE response=OpenEventA(SYNCHRONIZE,FALSE,resname);
    if(!mapping||!request||!response) fail("client open mapping/events");
    Shared *s=(Shared*)MapViewOfFile(mapping,FILE_MAP_ALL_ACCESS,0,0,sizeof(Shared));
    if(!s) fail("client map view");
    s->value=(LONG)req;
    MemoryBarrier();
    if(!SetEvent(request)) fail("client set");
    if(WaitForSingleObject(response,TIMEOUT_MS)!=WAIT_OBJECT_0) fail("client wait");
    MemoryBarrier();
    answer=(uint32_t)s->answer;
    UnmapViewOfFile(s);CloseHandle(mapping);
    CloseHandle(request);CloseHandle(response);
  }
  if(answer!=(req^MAGIC)) return 95;
  puts("ok");
  return 0;
}
int main(int argc,char **argv) {
  if(argc==2 && strcmp(argv[1],"base")==0) {puts("ok");return 0;}
  if(argc<3) {fputs("usage: client base | worker endpoint | manager MODE TOKEN | client MODE TOKEN\n",stderr);return 2;}
  if(strcmp(argv[1],"worker")==0) {run_worker(argv[2]);return 0;}
  if(argc!=4) return 2;
  if(!GetModuleFileNameA(NULL,executable,MAX_PATH)) fail("GetModuleFileName");
  if(strcmp(argv[1],"manager")==0) {
    if(strcmp(argv[2],"pipe")==0) run_manager_pipe(argv[3]);
    else if(strcmp(argv[2],"shm")==0) run_manager_shm(argv[3]);
    else return 2;
    return 0;
  }
  if(strcmp(argv[1],"client")==0) return run_client(argv[2],argv[3]);
  return 2;
}
