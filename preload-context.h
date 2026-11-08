#ifndef PRELOAD_CONTEXT_H
#define PRELOAD_CONTEXT_H

#include <stddef.h>

/* Parse optional cwd/env request fields and apply to the current worker only. */
int preload_apply_context(const char *buf, size_t len);

#endif
