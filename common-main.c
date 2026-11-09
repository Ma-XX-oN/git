#include "git-compat-util.h"
#include "common-init.h"

int main(int argc, const char **argv)
{
	int result;

#ifndef GIT_WINDOWS_NATIVE
	if (argc > 1 && !strcmp(argv[1], "--preload")) {
		if (argc != 3) {
			fprintf(stderr, "error: --preload requires a worker directory\n");
			exit(129);
		}
		result = git_preload_unix_worker(argv[2]);
		/* The worker has already reported and flushed its own status. */
		_exit(result);
	}
#endif

	init_git(argv);
	result = cmd_main(argc, argv);

	/* Not exit(3), but a wrapper calling our common_exit() */
	exit(result);
}
