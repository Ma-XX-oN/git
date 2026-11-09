#ifndef COMMON_INIT_H
#define COMMON_INIT_H

void init_git(const char **argv);

#ifdef GIT_WINDOWS_NATIVE
/* Append a shutdown record even after Git's trace descriptors are closed. */
void git_preinit_exit_diagnostic(const char *event, int status);
#else
/* One-shot POSIX worker entry point, called before normal Git initialization. */
int git_preload_unix_worker(const char *directory);
#endif

#endif /* COMMON_INIT_H */
