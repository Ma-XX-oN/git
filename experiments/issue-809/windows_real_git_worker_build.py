#!/usr/bin/env python3
"""RWF #809: launch one *actual* source-built Git worker waiting in wmain.

The source modification is applied ONLY in the temporary CI checkout.
"""
from __future__ import annotations
import os
from pathlib import Path
import subprocess

HOOK = r'''
#ifdef GIT809_WORKER_PROBE
static int git809_worker_dispatch(const char *token)
{
  char name[180];
  HANDLE pipe;
  char data[4096];
  DWORD used=0, n=0;
  int count, pos, i, exit_code;
  const char *args[33]={0};
  if (strlen(token)>35 || strchr(token,'\\') || strchr(token,'/'))
    return 89;
  if (snprintf(name,sizeof(name),"\\\\.\\pipe\\git809-worker-%s",token)
      >=sizeof(name))
    return 89;
  pipe=CreateNamedPipeA(name, PIPE_ACCESS_INBOUND,
      PIPE_TYPE_BYTE|PIPE_READMODE_BYTE|PIPE_WAIT,
      1,4096,4096,5000,NULL);
  if(pipe==INVALID_HANDLE_VALUE)
    return 90;
  if(!ConnectNamedPipe(pipe,NULL) &&
     GetLastError()!=ERROR_PIPE_CONNECTED) {
    CloseHandle(pipe);return 91;
  }
  while(used<sizeof(data)) {
    if(!ReadFile(pipe,data+used,sizeof(data)-used,&n,NULL)) {
      if(GetLastError()!=ERROR_BROKEN_PIPE) {
        CloseHandle(pipe);return 92;
      }
      break;
    }
    if(!n) break;
    used+=n;
  }
  CloseHandle(pipe);
  if(used<4) return 93;
  count=(unsigned char)data[0]|
    ((unsigned char)data[1]<<8)|
    ((unsigned char)data[2]<<16)|
    ((unsigned char)data[3]<<24);
  if(count<1 || count>32) return 94;
  pos=4;
  for(i=0;i<count;i++) {
    int start=pos;
    if(pos>=used) return 95;
    while(pos<used && data[pos]) pos++;
    if(pos==used) return 96;
    if(pos>start && !MultiByteToWideChar(CP_UTF8,
        MB_ERR_INVALID_CHARS,data+start,pos-start,NULL,0))
      return 97;
    args[i]=data+start;
    pos++;
  }
  if(pos!=used || strcmp(args[0],"git")) return 98;
  args[count]=NULL;
  exit_code=main(count,args);
  return exit_code;
}
#endif
'''
def run(cmd,timeout=1800):
  p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                   timeout=timeout,check=False)
  if p.returncode:
    raise RuntimeError(f"command failed rc={p.returncode} {cmd!r}:\n"+
                       p.stdout.decode("utf-8","replace")[-1500:]+
                       p.stderr.decode("utf-8","replace")[-3500:])
def build(root:Path):
  p=root/"compat"/"mingw.c"
  source=p.read_text(encoding="utf-8")
  anchor="int wmain(int argc, const wchar_t **wargv)\n"
  assert source.count(anchor)==1,"unexpected Git Windows wmain signature"
  source=source.replace(anchor,HOOK+"\n"+anchor,1)
  call="\t/* invoke the real main() using our utf8 version of argv. */\n\texit_status = main(argc, argv);"
  assert source.count(call)==1,"unexpected Git main invocation"
  changed=("\t/* invoke the real main() using our utf8 version of argv. */\n"
           "#ifdef GIT809_WORKER_PROBE\n"
           "\tif (argc == 3 && !strcmp(argv[1], \"--git809-worker\"))\n"
           "\t\texit_status = git809_worker_dispatch(argv[2]);\n"
           "\telse\n#endif\n"
           "\t\texit_status = main(argc, argv);")
  source=source.replace(call,changed,1)
  p.write_text(source,encoding="utf-8")
  command=["make","-j4","NO_RUST=YesPlease","NO_CURL=YesPlease",
           "NO_GETTEXT=YesPlease","CFLAGS_APPEND=-DGIT809_WORKER_PROBE",
           "git.exe"]
  run(command)
  exe=(root/"git.exe").resolve()
  assert exe.is_file(), "source Git binary not built"
  return exe,command
