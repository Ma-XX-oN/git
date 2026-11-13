#include "git-compat-util.h"
#include "common-init.h"
#include "preload/include/worker/worker.h"

int main(int argc, const char **argv)
{
	int result;

#ifdef GIT_IO_TRACE
  /*
   * Recognize the trace option before init_git(), including options
   * following global options with separate operands. Leave any later
   * duplicate for handle_options() to reject. Git commands' own
   * arguments are not scanned.
   */
  for (int i = 1; i < argc; i++) {
    if (!strcmp(argv[i], "--log-id") || !strcmp(argv[i], "--log-fn")) {
      if (i + 1 == argc) {
        fprintf(stderr, "git: missing value for %s\n", argv[i]);
        return 1;
      }
      if (io_trace_begin(argv[i], argv[i + 1])) {
        fprintf(stderr, "git: cannot initialize binary I/O trace: %s\n",
                strerror(errno));
        return 1;
      }
      for (int j = i; j + 2 <= argc; j++)
        argv[j] = argv[j + 2];
      argc -= 2;
      break;
    }
    if (!strcmp(argv[i], "-C") || !strcmp(argv[i], "-c") ||
        !strcmp(argv[i], "--git-dir") ||
        !strcmp(argv[i], "--work-tree") ||
        !strcmp(argv[i], "--namespace") ||
        !strcmp(argv[i], "--config-env") ||
        !strcmp(argv[i], "--attr-source") ||
        !strcmp(argv[i], "--shallow-file")) {
      i++;
      continue;
    }
    if (!strcmp(argv[i], "--version") || !strcmp(argv[i], "-v") ||
        !strcmp(argv[i], "--help") || !strcmp(argv[i], "-h") ||
        argv[i][0] != '-')
      break;
  }
#endif

	if (argc > 1 && !strcmp(argv[1], "--preload-worker")) {
		result = preload_worker_main(argc, argv, cmd_main);
		return result;
	}

	init_git(argv);
	result = cmd_main(argc, argv);

	/* Not exit(3), but a wrapper calling our common_exit() */
	exit(result);
}
