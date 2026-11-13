/*
 * Diagnostic only: a native process-launch control with no Git initialization.
 * Compile with the same C toolchain used by the Git benchmark build.
 */
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
  if (argc == 2 && !strcmp(argv[1], "output"))
    puts("native launch control");
  return 0;
}
