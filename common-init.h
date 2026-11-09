#ifndef COMMON_INIT_H
#define COMMON_INIT_H

void init_git(const char **argv);

#ifndef GIT_WINDOWS_NATIVE
/* One-shot POSIX worker entry point, called before normal Git initialization. */
int git_preload_unix_worker(const char *directory);
#endif

#endif /* COMMON_INIT_H */
