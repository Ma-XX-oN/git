#include "git-compat-util.h"
#include "common-init.h"
#include "preload/include/worker/worker.h"

int main(int argc, const char **argv)
{
	int result;

	if (argc > 1 && !strcmp(argv[1], "--preload-worker")) {
		result = preload_worker_main(argc, argv);
		return result;
	}

	init_git(argv);
	result = cmd_main(argc, argv);

	/* Not exit(3), but a wrapper calling our common_exit() */
	exit(result);
}
