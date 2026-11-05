#include <wchar.h>

/* The Git for Windows SDK's MinGW/UCRT executable-startup baseline.
 * Compile with gcc -municode so the entry contract matches Git's wmain.
 */
int wmain(int argc, wchar_t **argv)
{
  (void)argc;
  (void)argv;
  return 0;
}
