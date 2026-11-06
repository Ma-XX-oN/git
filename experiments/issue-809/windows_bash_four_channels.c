/* #809 isolated native Windows 4-channel Bash IPC proof, no Git changes. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#define SLOTS 6
#define MAXMSG 4096
typedef struct {
  char path[180];
  HANDLE ready;
  HANDLE stop;
  CRITICAL_SECTION lock;
  unsigned char payload[MAXMSG];
  DWORD length;
  volatile LONG successful;
  int incoming;
} Channel;
typedef struct { Channel *channel; int slot; } Slot;
static Channel channels[4];
static Slot slots[4][SLOTS];
static HANDLE threads[4][SLOTS];
static volatile LONG parse_ok=0;
static volatile LONG chosen_exit=0;
static char token_global[50];

static void fatal(const char *what) {
  fprintf(stderr,"%s: winerr=%lu\n",what,GetLastError());
  ExitProcess(80);
}
static int parse(const unsigned char *p,DWORD len) {
  if(len<4) return 0;
  uint32_t count=(uint32_t)p[0]|((uint32_t)p[1]<<8)|
    ((uint32_t)p[2]<<16)|((uint32_t)p[3]<<24);
  if(count != 2) return 0;
  DWORD pos=4;
  const char *arg[2]={0};
  for(uint32_t i=0;i<count;i++) {
    if(pos>=len) return 0;
    DWORD start=pos;
    while(pos<len && p[pos]) pos++;
    if(pos==len) return 0;
    arg[i]=(const char*)p+start;
    pos++;
  }
  if(pos!=len || strcmp(arg[0],"git")) return 0;
  if(!strcmp(arg[1],"success")) return 1;
  if(!strcmp(arg[1],"failure")) return 2;
  return 0;
}
static DWORD WINAPI serve(void *opaque) {
  Slot *s=(Slot*)opaque;
  Channel *c=s->channel;
  HANDLE pipe=CreateNamedPipeA(c->path,
    c->incoming?PIPE_ACCESS_INBOUND:PIPE_ACCESS_OUTBOUND,
    PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,
    PIPE_UNLIMITED_INSTANCES,4096,4096,1000,NULL);
  if(pipe==INVALID_HANDLE_VALUE) return 71;
  BOOL connected=ConnectNamedPipe(pipe,NULL);
  if(!connected && GetLastError()!=ERROR_PIPE_CONNECTED) {
    CloseHandle(pipe);return 72;
  }
  if(c->incoming) {
    unsigned char buf[MAXMSG]; DWORD n=0;
    /* Client Bash keeps the write fd open during printf, closes afterward. */
    if(ReadFile(pipe,buf,sizeof(buf),&n,NULL) && n>0) {
      int mode=parse(buf,n);
      if(mode) {
        if(InterlockedCompareExchange(&parse_ok,mode,0)==0) {
          const unsigned char success_out[]={'O','U','T',0,'X','\n'};
          const unsigned char fail_out[]={'F','A','I','L','\n'};
          const unsigned char success_err[]={'E','R','R','\n'};
          const unsigned char fail_err[]={'B','A','D','\n'};
          Channel *out=&channels[1], *err=&channels[2], *code=&channels[3];
          const unsigned char *o=mode==1?success_out:fail_out;
          DWORD on=mode==1?sizeof(success_out):sizeof(fail_out);
          const unsigned char *e=mode==1?success_err:fail_err;
          DWORD en=mode==1?sizeof(success_err):sizeof(fail_err);
          memcpy(out->payload,o,on);out->length=on;
          memcpy(err->payload,e,en);err->length=en;
          chosen_exit=mode==1?0:37;
          /* Status has its own dedicated unidirectional channel. */
          code->length=(DWORD)snprintf((char*)code->payload,MAXMSG,
                                      "%ld\n",chosen_exit);
          SetEvent(out->ready);SetEvent(err->ready);
          /* For this transport probe status is sent after output availability.
             A real manager must track actual process completion. */
          SetEvent(code->ready);
        }
      }
    }
  } else {
    if(WaitForSingleObject(c->ready,8000)==WAIT_OBJECT_0) {
      DWORD written=0;
      if(WriteFile(pipe,c->payload,c->length,&written,NULL) &&
         written==c->length) {
        InterlockedIncrement(&c->successful);
        FlushFileBuffers(pipe);
      }
    }
  }
  DisconnectNamedPipe(pipe);CloseHandle(pipe);
  return 0;
}
int main(int argc,char **argv) {
  if(argc!=2 || strlen(argv[1])>35) return 2;
  snprintf(token_global,sizeof(token_global),"%s",argv[1]);
  const char *types[]={"request","stdout","stderr","status"};
  for(int i=0;i<4;i++) {
    Channel *c=&channels[i];c->incoming=(i==0);
    snprintf(c->path,sizeof(c->path),
      "\\\\.\\pipe\\git809-four-%s-%s",types[i],token_global);
    c->ready=CreateEventA(NULL,TRUE,FALSE,NULL);
    c->stop=CreateEventA(NULL,TRUE,FALSE,NULL);
    InitializeCriticalSection(&c->lock);
    if(!c->ready||!c->stop) fatal("CreateEvent");
  }
  for(int i=0;i<4;i++) for(int j=0;j<SLOTS;j++) {
    slots[i][j].channel=&channels[i];slots[i][j].slot=j;
    threads[i][j]=CreateThread(NULL,0,serve,&slots[i][j],0,NULL);
    if(!threads[i][j]) fatal("CreateThread");
  }
  puts("READY");fflush(stdout);
  /* Wait until at least one input is parsed, then allow Bash to read. */
  for(int i=0;i<800;i++) {
    if(parse_ok!=0) break;
    Sleep(10);
  }
  if(!parse_ok) {fprintf(stderr,"invalid or missing request\n");return 3;}
  for(int i=0;i<800;i++) {
    if(channels[1].successful && channels[2].successful &&
       channels[3].successful) break;
    Sleep(10);
  }
  printf("DONE mode=%ld stdout=%ld stderr=%ld status=%ld\n",
         parse_ok,channels[1].successful,channels[2].successful,
         channels[3].successful);
  fflush(stdout);
  return channels[1].successful && channels[2].successful &&
         channels[3].successful ? 0:4;
}
