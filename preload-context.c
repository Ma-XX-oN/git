#include "git-compat-util.h"
#include "preload-context.h"
#include "utf8.h"

/**
 * @brief Apply a path emitted directly by the caller's pwd builtin.
 *
 * @param value Borrowed native UTF-8 path followed by one framing newline.
 * @param len Readable bytes, excluding the wire NUL terminator.
 * @return Zero on success, -1 for invalid text or chdir failure.
 */
static int apply_cwd_field(const char *value, size_t len)
{
	char *path;
	int result;

	if (len < 2 || value[len - 1] != '\n')
		return -1;
	path = xmemdupz(value, len - 1);
	result = !is_utf8(path) || chdir(path) ? -1 : 0;
	free(path);
	return result;
}

/**
 * @brief Apply the optional working directory to this single-use worker.
 *
 * @param buf Borrowed cwd field, excluding argv and the request end tag.
 * @param len Readable byte count; zero retains the inherited working directory.
 * @return Zero on success, -1 for malformed fields or chdir failure.
 */
int preload_context_apply(const char *buf, size_t len)
{
	const char *value, *end;

	if (!len)
		return 0;
	if (len < 4 || memcmp(buf, "cwd\0", 4))
		return -1;
	value = buf + 4;
	end = memchr(value, '\0', len - 4);
	if (!end || end != buf + len - 1)
		return -1;
	return apply_cwd_field(value, end - value);
}
