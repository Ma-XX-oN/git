/* Issue 809: targeted metadata-substitution experiment.
 * Do not patch MSYS2 globally. CreateFile opens the named pipe once, then
 * cygwin_attach_handle_to_fd maps that native handle onto a POSIX fd while
 * looking up a distinct, existing, harmless regular file for its metadata.
 * Experimental only: MSYS2 warns attachment is best-effort.
 */
#include <windows.h>
#include <sys/cygwin.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include "builtins.h"
#include "shell.h"

int git809attach_builtin(WORD_LIST *list)
{
  const char *pipe_path, *regular_file, *direction;
  HANDLE h;
  DWORD access;
  int fd;
  if (!list || !list->word || !list->next ||
      !list->next->word || !list->next->next ||
      !list->next->next->word || list->next->next->next) {
    fprintf(stderr, "GIT809_ATTACH_ERROR invalid arguments\n");
    return 2;
  }
  pipe_path = list->word->word;
  regular_file = list->next->word->word;
  direction = list->next->next->word->word;
  if (!pipe_path || !regular_file || !direction ||
      strncmp(pipe_path, "\\\\.\\pipe\\git809-redirect-", 23) != 0) {
    fprintf(stderr, "GIT809_ATTACH_ERROR invalid named pipe or metadata path\n");
    return 2;
  }
  if (!strcmp(direction, "read"))
    access = GENERIC_READ;
  else if (!strcmp(direction, "write"))
    access = GENERIC_WRITE;
  else
    return 2;
  h = CreateFileA(pipe_path, access, 0, NULL, OPEN_EXISTING, 0, NULL);
  if (h == INVALID_HANDLE_VALUE) {
    fprintf(stderr, "GIT809_ATTACH_ERROR CreateFile error=%lu\n",
            (unsigned long)GetLastError());
    return 1;
  }
  /* fd 9 is intentionally reserved by the test shell and must be closed
   * at call entry. No underlying pipe pathname is subjected to MSYS2 stat.
   */
  fd = cygwin_attach_handle_to_fd((char *)regular_file, 9, h, O_BINARY, access);
  if (fd != 9) {
    fprintf(stderr, "GIT809_ATTACH_ERROR attach returned=%d\n", fd);
    CloseHandle(h);
    return 1;
  }
  fprintf(stderr, "GIT809_ATTACH_OK fd=%d access=%s\n", fd, direction);
  return 0;
}
char *git809attach_doc[] = {
  "Open a native issue-809 pipe, substituting a regular file's metadata.",
  (char *)NULL
};
struct builtin git809attach_struct = {
  "git809attach", git809attach_builtin, BUILTIN_ENABLED, git809attach_doc,
  "git809attach PIPE EXISTING_REGULAR_FILE read|write", 0
};
