#ifndef PRELOAD_CONTEXT_H
#define PRELOAD_CONTEXT_H

#include <stddef.h>

/**
 * @brief Identify a context field tag.
 *
 * @param tag Borrowed NUL-terminated tag.
 * @return Non-negative field index for `cwd`, `env-del` or `env-add`;
 *   -1 for any other tag.
 */
int preload_context_field(const char *tag);

/**
 * @brief Validate and apply the optional cwd, env-del and env-add fields.
 *
 * The caller retains ownership of the length-bounded UTF-8 frame. Each field
 * appears at most once, in any order. Every field is validated before any is
 * applied; then the working directory changes, the env-del names are deleted
 * and the env-add assignments are set.
 *
 * @param buf Borrowed context fields, excluding argv and the request end tag.
 * @param len Number of readable bytes; zero retains the inherited context.
 * @return Zero on success, -1 on invalid fields or a failed change.
 */
int preload_context_apply(const char *buf, size_t len);

#endif
