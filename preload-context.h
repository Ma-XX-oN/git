#ifndef PRELOAD_CONTEXT_H
#define PRELOAD_CONTEXT_H

#include <stddef.h>

/**
 * @brief Validate and apply optional cwd and complete environment fields.
 *
 * The caller retains ownership of the length-bounded UTF-8 frame. Return zero
 * on success or -1 for malformed fields or an application failure. Apply only
 * in a single-use worker: failure can leave partially applied process context.
 * @param buf Borrowed encoded cwd/environment fields, excluding the request end tag.
 * @param len Number of readable bytes in buf; zero leaves inherited context intact.
 * @return Zero on success, -1 on invalid fields or a context application failure.
 */
int preload_context_apply(const char *buf, size_t len);

#endif
