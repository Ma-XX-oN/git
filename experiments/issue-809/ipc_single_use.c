#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <shellapi.h>
#include <stdio.h>
#include <stdint.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

#define TIMEOUT_MS 20000
#define POOL_SIZE 3
#define MAGIC 0x809CAFE1u
#define MAX_ARGS 32u
#define MAX_WIRE 4096u
typedef struct {
  volatile LONG size;
  volatile LONG answer;
  unsigned char data[MAX_WIRE];
} Shared;
typedef struct {
  uint32_t argc;
  const char *argv[MAX_ARGS + 1];
} ParsedArgs;

/* Count is a little-endian u32; exactly argc NUL-terminated UTF-8 strings follow. */
static uint32_t get_u32(const unsigned char *p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
         ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static void put_u32(unsigned char *p, uint32_t n) {
  for(int i=0;i<4;i++) p[i]=(unsigned char)(n >> (8*i));
}
static int decode_argv(const unsigned char *data, size_t len, ParsedArgs *out) {
  if(len < 4) return 0;
  uint32_t count=get_u32(data);
  if(count > MAX_ARGS) return 0;
  size_t p=4;
  out->argc=count;
  for(uint32_t i=0;i<count;i++) {
    if(p >= len) return 0;
    size_t start=p;
    while(p < len && data[p]) p++;
    if(p == len) return 0;
    size_t n=p-start;
    if(n > 0 && (n > (size_t)INT_MAX ||
       !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
                            (const char*)data+start, (int)n, NULL, 0)))
      return 0;
    out->argv[i]=(const char*)data+start;
    p++;
  }
  out->argv[count]=NULL;
  return p==len;
}
static uint32_t digest_argv(const unsigned char *data, size_t len) {
  ParsedArgs args;
  if(!decode_argv(data,len,&args)) {
    fputs("invalid counted argv payload\n",stderr);
    ExitProcess(94);
  }
  uint32_t h=2166136261u;
  for(size_t i=0;i<len;i++) {h^=data[i];h*=16777619u;}
  return h^MAGIC;
}
static DWORD build_from_args(unsigned char data[MAX_WIRE],
                             uint32_t count,const char *const *args) {
  if(count>MAX_ARGS) ExitProcess(94);
  put_u32(data,count);
  DWORD p=4;
  for(uint32_t i=0;i<count;i++) {
    size_t n=strlen(args[i])+1;
    if(n>MAX_WIRE-p) ExitProcess(94);
    memcpy(data+p,args[i],n);p+=(DWORD)n;
  }
  ParsedArgs parsed;
  if(!decode_argv(data,p,&parsed) || parsed.argc!=count) ExitProcess(94);
  return p;
}
static DWORD build_request(unsigned char data[MAX_WIRE]) {
  static const char *args[]={"git", "--version", "", "quoted \"path\" \\",
                              "UTF-8: \xE2\x86\x92"};
  return build_from_args(data,(uint32_t)(sizeof(args)/sizeof(args[0])),args);
}
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
    PIPE_TYPE_MESSAGE|PIPE_READMODE_MESSAGE|PIPE_WAIT,1,4096,4096,TIMEOUT_MS,NULL);
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
static uint32_t dispatch(const unsigned char *request,DWORD size,int *next) {
  int i=(*next)++%POOL_SIZE;
  uint32_t reply=0;
  ParsedArgs parsed;
  if(!decode_argv(request,size,&parsed)) ExitProcess(94);
  transfer(workers[i].pipe,(void*)request,size,1);
  transfer(workers[i].pipe,&reply,sizeof(reply),0);
  if(reply!=digest_argv(request,size)) { fputs("worker mismatch\n",stderr);ExitProcess(96); }
  stop_worker(i);
  return reply;
}
static void run_worker(const char *endpoint) {
  HANDLE h=open_pipe(endpoint);
  unsigned char req[MAX_WIRE];
  DWORD size=0;
  if(!ReadFile(h,req,sizeof(req),&size,NULL) || !size) fail("worker argv read");
  uint32_t answer=digest_argv(req,size);
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
    unsigned char req[MAX_WIRE];
    DWORD got=0;
    BOOL ok=ReadFile(h,req,sizeof(req),&got,NULL);
    if(ok && got>0) {
      uint32_t reply=dispatch(req,got,&next);
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
  s->size=0;s->answer=0;
  for(int i=0;i<POOL_SIZE;i++) start_worker(i,token);
  puts("READY");fflush(stdout);
  int next=0;
  for(;;) {
    if(WaitForSingleObject(req,INFINITE)!=WAIT_OBJECT_0) fail("req wait");
    LONG n=s->size;
    if(n<4 || n>(LONG)MAX_WIRE) ExitProcess(94);
    uint32_t answer=dispatch(s->data,(DWORD)n,&next);
    s->answer=(LONG)answer;
    MemoryBarrier();
    if(!SetEvent(res)) fail("set response event");
    start_worker((next-1)%POOL_SIZE,token);
  }
}
static int run_client(const char *mode,const char *token,
                      int argc,const char *const *argv) {
  unsigned char req[MAX_WIRE];
  DWORD size=argc ? build_from_args(req,(uint32_t)argc,argv)
                  : build_request(req);
  uint32_t answer=0;
  if(strcmp(mode,"pipe")==0) {
    char endpoint[128];make_name(endpoint,sizeof(endpoint),"mgr",token);
    HANDLE h=open_pipe(endpoint);
    transfer(h,req,size,1);
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
    memcpy(s->data,req,size);
    s->size=(LONG)size;
    MemoryBarrier();
    if(!SetEvent(request)) fail("client set");
    if(WaitForSingleObject(response,TIMEOUT_MS)!=WAIT_OBJECT_0) fail("client wait");
    MemoryBarrier();
    answer=(uint32_t)s->answer;
    UnmapViewOfFile(s);CloseHandle(mapping);
    CloseHandle(request);CloseHandle(response);
  }
  if(answer!=digest_argv(req,size)) return 95;
  puts("ok");
  return 0;
}
static int verify_decoder(void) {
  unsigned char msg[MAX_WIRE];
  DWORD len=build_request(msg);
  ParsedArgs args;
  if(!decode_argv(msg,len,&args) || args.argc!=5 ||
     strcmp(args.argv[0],"git") || strcmp(args.argv[1],"--version") ||
     strcmp(args.argv[2],"") || strcmp(args.argv[3],"quoted \"path\" \\") ||
     strcmp(args.argv[4],"UTF-8: \xE2\x86\x92") || args.argv[5]!=NULL)
    return 1;
  if(decode_argv(msg,len-1,&args)) return 2;
  if(decode_argv(msg,3,&args)) return 3;
  if(decode_argv(msg,len+1,&args)) return 4;
  unsigned char mutated[MAX_WIRE];
  memcpy(mutated,msg,len);
  put_u32(mutated,MAX_ARGS+1u);
  if(decode_argv(mutated,len,&args)) return 5;
  memcpy(mutated,msg,len);
  put_u32(mutated,6u);
  if(decode_argv(mutated,len,&args)) return 6;
  memcpy(mutated,msg,len);
  /* Change valid UTF-8 arrow to an invalid standalone continuation byte. */
  for(DWORD i=4;i+2<len;i++) {
    if(mutated[i]==0xE2u && mutated[i+1]==0x86u && mutated[i+2]==0x92u) {
      mutated[i]=0x80u;
      break;
    }
  }
  if(decode_argv(mutated,len,&args)) return 7;
  puts("argv decoder PASS");
  return 0;
}
static int run_entry(int argc,char **argv) {
  if(argc==2 && strcmp(argv[1],"selftest")==0) return verify_decoder();
  if(argc==2 && strcmp(argv[1],"base")==0) {puts("ok");return 0;}
  if(argc<3) {fputs("usage: client base | worker endpoint | manager MODE TOKEN | client MODE TOKEN\n",stderr);return 2;}
  if(strcmp(argv[1],"worker")==0) {run_worker(argv[2]);return 0;}
  if(argc<4) return 2;
  if(!GetModuleFileNameA(NULL,executable,MAX_PATH)) fail("GetModuleFileName");
  if(strcmp(argv[1],"manager")==0) {
    if(argc!=4) return 2;
    if(strcmp(argv[2],"pipe")==0) run_manager_pipe(argv[3]);
    else if(strcmp(argv[2],"shm")==0) run_manager_shm(argv[3]);
    else return 2;
    return 0;
  }
  if(strcmp(argv[1],"client")==0) return run_client(argv[2],argv[3],argc-4,(const char *const *)(argv+4));
  return 2;
}

/* Receive the real Windows command-line array as UTF-16, never via the ANSI
 * codepage. The wire protocol carries the resulting UTF-8 argv elements. */
int main(void) {
  int argc=0;
  LPWSTR *wargv=CommandLineToArgvW(GetCommandLineW(),&argc);
  if(!wargv || argc<=0) return 98;
  char **argv=(char**)calloc((size_t)argc+1,sizeof(*argv));
  if(!argv) return 99;
  for(int i=0;i<argc;i++) {
    int n=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wargv[i],-1,
                              NULL,0,NULL,NULL);
    if(n<=0) return 98;
    argv[i]=(char*)malloc((size_t)n);
    if(!argv[i]) return 99;
    if(!WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,wargv[i],-1,
                            argv[i],n,NULL,NULL)) return 98;
  }
  int code=run_entry(argc,argv);
  for(int i=0;i<argc;i++) free(argv[i]);
  free(argv);
  LocalFree(wargv);
  return code;
}
