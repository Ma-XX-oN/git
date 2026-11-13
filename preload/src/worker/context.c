#include "git-compat-util.h"
#include "worker/context.h"
#include "utf8.h"

/** Context fields in the order they are applied; each appears at most once. */
enum context_field {
	CONTEXT_CWD, CONTEXT_ENV_DEL, CONTEXT_ENV_ADD, CONTEXT_FIELD_COUNT
};

/** Wire tags indexed by enum context_field. */
static const char *const context_tags[CONTEXT_FIELD_COUNT] = {
	"cwd", "env-del", "env-add"
};

/** One received field value, borrowed from the request frame. */
struct context_value {
	const char *value; /**< Borrowed bytes, NUL-terminated in the frame. */
	size_t len; /**< Value length, excluding the wire NUL terminator. */
};

int preload_context_field(const char *tag)
{
	int i;

	for (i = 0; i < CONTEXT_FIELD_COUNT; i++)
		if (!strcmp(tag, context_tags[i]))
			return i;
	return -1;
}

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
 * @brief Check an environment field: newline-terminated UTF-8 entries.
 *
 * env-del entries are nonempty names without '='; env-add entries are
 * NAME=value with a nonempty name, split at the first '='. There is no
 * escaping, so names and values cannot contain newlines.
 *
 * @param value Borrowed field bytes, NUL-terminated at len.
 * @param len Readable bytes, excluding the wire NUL terminator.
 * @param add Nonzero for env-add, zero for env-del.
 * @return One if every entry is valid, zero otherwise.
 */
static int valid_env_field(const char *value, size_t len, int add)
{
	const char *entry = value, *end = value + len;

	if (!is_utf8(value))
		return 0;
	while (entry < end) {
		const char *eol = memchr(entry, '\n', end - entry);
		const char *equal;

		if (!eol || eol == entry)
			return 0;
		equal = memchr(entry, '=', eol - entry);
		if (equal == entry || !add != !equal)
			return 0;
		entry = eol + 1;
	}
	return 1;
}

/**
 * @brief Delete or set the variables of a validated environment field.
 *
 * Uses Git's setenv/unsetenv, which on Windows update the Win32 environment
 * that Git reads and passes to its children.
 *
 * @param value Borrowed field bytes accepted by valid_env_field().
 * @param len Readable bytes, excluding the wire NUL terminator.
 * @param add Nonzero for env-add, zero for env-del.
 * @return Zero on success, -1 if the platform rejects a change.
 */
static int apply_env_field(const char *value, size_t len, int add)
{
	const char *entry = value, *end = value + len;

	while (entry < end) {
		const char *eol = memchr(entry, '\n', end - entry);
		const char *equal = add ? memchr(entry, '=', eol - entry) : eol;
		char *name = xmemdupz(entry, equal - entry);
		int result;

		if (add) {
			char *text = xmemdupz(equal + 1, eol - equal - 1);
			result = setenv(name, text, 1);
			free(text);
		} else {
			result = unsetenv(name);
		}
		free(name);
		if (result)
			return -1;
		entry = eol + 1;
	}
	return 0;
}

/**
 * @brief Apply the optional working directory and environment changes.
 *
 * Every field is validated before anything is applied. The working
 * directory changes first, then env-del deletions, then env-add assignments.
 *
 * @param buf Borrowed context fields, excluding argv and the request end tag.
 * @param len Readable byte count; zero retains the inherited context.
 * @return Zero on success, -1 for malformed fields or a failed change.
 */
int preload_context_apply(const char *buf, size_t len)
{
	struct context_value fields[CONTEXT_FIELD_COUNT] = { { NULL, 0 } };
	size_t pos = 0;
	int i;

	while (pos < len) {
		const char *tag = buf + pos, *end = memchr(tag, '\0', len - pos);
		int field;

		if (!end)
			return -1;
		field = preload_context_field(tag);
		pos = end - buf + 1;
		end = memchr(buf + pos, '\0', len - pos);
		if (field < 0 || fields[field].value || !end)
			return -1;
		fields[field].value = buf + pos;
		fields[field].len = end - (buf + pos);
		pos = end - buf + 1;
	}
	for (i = CONTEXT_ENV_DEL; i <= CONTEXT_ENV_ADD; i++)
		if (fields[i].value &&
		    !valid_env_field(fields[i].value, fields[i].len, i == CONTEXT_ENV_ADD))
			return -1;
	if (fields[CONTEXT_CWD].value &&
	    apply_cwd_field(fields[CONTEXT_CWD].value, fields[CONTEXT_CWD].len))
		return -1;
	for (i = CONTEXT_ENV_DEL; i <= CONTEXT_ENV_ADD; i++)
		if (fields[i].value &&
		    apply_env_field(fields[i].value, fields[i].len, i == CONTEXT_ENV_ADD))
			return -1;
	return 0;
}
