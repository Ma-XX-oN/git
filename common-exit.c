#include "git-compat-util.h"
#include "trace2.h"

#ifdef GIT_WINDOWS_NATIVE
/* Defined in compat/mingw.c; does not use Git's trace descriptor. */
void git_preinit_exit_diagnostic(const char *event, int status);
#endif

static void (*preload_exit_callback)(int);

void set_preload_exit_callback(void (*callback)(int))
{
	preload_exit_callback = callback;
}

static void check_bug_if_BUG(void)
{
	if (!bug_called_must_BUG)
		return;
	BUG("on exit(): had bug() call(s) in this process without explicit BUG_if_bug()");
}

/* We wrap exit() to call common_exit() in git-compat-util.h */
int common_exit(const char *file, int line, int code)
{
	/*
	 * For non-POSIX systems: Take the lowest 8 bits of the "code"
	 * to e.g. turn -1 into 255. On a POSIX system this is
	 * redundant, see exit(3) and wait(2), but as it doesn't harm
	 * anything there we don't need to guard this with an "ifdef".
	 */
	code &= 0xff;

#ifdef GIT_WINDOWS_NATIVE
	git_preinit_exit_diagnostic("common_exit", code);
#endif
	check_bug_if_BUG();
	trace2_cmd_exit_fl(file, line, code);
	if (preload_exit_callback)
		preload_exit_callback(code);

	return code;
}
