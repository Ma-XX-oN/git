#ifndef PRELOAD_CONTEXT_H
#define PRELOAD_CONTEXT_H

#include <stddef.h>

/**
 * @brief Validate and apply the optional working-directory field.
 *
 * The caller retains ownership of the length-bounded UTF-8 frame. Return zero
 * on success or -1 for malformed fields or a chdir failure.
 *
 * @param buf Borrowed cwd field, excluding argv and the request end tag.
 * @param len Number of readable bytes; zero retains the inherited working directory.
 * @return Zero on success, -1 on invalid fields or chdir failure.
 */
int preload_context_apply(const char *buf, size_t len);

#endif
