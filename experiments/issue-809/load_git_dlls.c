#include <windows.h>

/*
 * Diagnostic control: start a plain MSVC program, set Git's native DLL
 * directory, and load Git's four non-system direct-import libraries.
 * The ONLY_SETDIR variant measures SetDllDirectory without loading them.
 * Loading happens after main(), unlike Git's actual load-time imports;
 * this isolates a related DLL loader workload, not identical startup.
 */
int main(int argc, char **argv)
{
  static const char *const dependencies[] = {
    "libiconv-2.dll", "libintl-8.dll", "libpcre2-8-0.dll", "zlib1.dll"
  };
  unsigned int i;
  if (argc != 2 || !SetDllDirectoryA(argv[1]))
    return 2;
#ifndef ONLY_SETDIR
  for (i = 0; i < sizeof(dependencies) / sizeof(dependencies[0]); i++)
    if (!LoadLibraryA(dependencies[i]))
      return 10 + (int)i;
#else
  (void)dependencies;
  (void)i;
#endif
  return 0;
}
