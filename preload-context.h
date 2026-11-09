#ifndef PRELOAD_CONTEXT_H
#define PRELOAD_CONTEXT_H

#include <stddef.h>

/**
 * @brief Validate and apply optional cwd and complete environment fields.
 *
 * The caller retains ownership of the length-bounded UTF-8 frame. Return zero
 * on success or -1 for malformed fields or an application failure. Apply only
 * in a single-use worker: failure can leave partially applied process context.
 */
int preload_context_apply(const char *buf, size_t len);

#endif
