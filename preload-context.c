#include "git-compat-util.h"
#include "preload-context.h"
#include "utf8.h"
#include "strvec.h"

#ifdef GIT_WINDOWS_NATIVE
# include <windows.h>
#endif

/** @brief Validate UTF-8 NAME=VALUE entries and their double-NUL terminator. */
static int valid_environment_block(const char *buf, size_t len)
{
	size_t offset = 0;

	if (len < 2 || buf[len - 1] || buf[len - 2])
		return -1;
	if (len == 2)
		return 0;
	while (offset < len - 1 && buf[offset]) {
		const char *entry = buf + offset;
		const char *end = memchr(entry, '\0', len - offset);
		const char *equal;
		if (!end || !is_utf8(entry))
			return -1;
		equal = strchr(entry, '=');
		if (!equal || equal == entry || equal >= end)
			return -1;
		offset = (size_t)(end - buf) + 1;
	}
	return offset == len - 1 ? 0 : -1;
}

#ifndef GIT_WINDOWS_NATIVE
/** @brief Remove ordinary inherited POSIX environment entries. */
static int clear_worker_environment(void)
{
	extern char **environ;
	while (environ && environ[0]) {
		const char *equal = strchr(environ[0], '=');
		char *key;
		size_t len;
		if (!equal || equal == environ[0])
			return -1;
		len = (size_t)(equal - environ[0]);
		key = malloc(len + 1);
		if (!key)
			return -1;
		memcpy(key, environ[0], len);
		key[len] = '\0';
		if (unsetenv(key)) {
			free(key);
			return -1;
		}
		free(key);
	}
	return 0;
}
#endif

/** @brief Validate and apply a complete UTF-8 binary environment snapshot. */
static int apply_environment_block(const char *buf, size_t len)
{
	size_t offset = 0;
#ifdef GIT_WINDOWS_NATIVE
	struct strvec environment = STRVEC_INIT;
	int result;
#endif
	if (valid_environment_block(buf, len))
		return -1;
#ifdef GIT_WINDOWS_NATIVE
	while (offset < len - 1 && buf[offset]) {
		strvec_push(&environment, buf + offset);
		offset += strlen(buf + offset) + 1;
	}
	result = mingw_replace_environment(environment.v);
	strvec_clear(&environment);
	return result;
#else
	if (clear_worker_environment())
		return -1;
	while (offset < len - 1 && buf[offset]) {
		const char *entry = buf + offset;
		const char *equal = strchr(entry, '=');
		char *key;
		size_t name_len = (size_t)(equal - entry);
		key = malloc(name_len + 1);
		if (!key)
			return -1;
		memcpy(key, entry, name_len);
		key[name_len] = '\0';
		if (setenv(key, equal + 1, 1)) {
			free(key);
			return -1;
		}
		free(key);
		offset += strlen(entry) + 1;
	}
	return 0;
#endif
}

/* Restricted Bash export -p decoder. Included in preload-context.c after
 * clear_worker_environment(); never interpret commands or expansions. */
/* Native Windows snapshot application uses mingw_replace_environment(). */
struct export_pair {
	char *name, *value;
};
/** @brief Decode one hexadecimal digit, returning -1 for an invalid digit. */
static int export_hex(unsigned char c)
{
	if (c >= '0' && c <= '9')
		return c - '0';
	if (c >= 'a' && c <= 'f')
		return c - 'a' + 10;
	if (c >= 'A' && c <= 'F')
		return c - 'A' + 10;
	return -1;
}
/** @brief Append a non-NUL Unicode scalar in UTF-8, updating the byte count. */
static int export_cp(char *out, size_t *n, unsigned long cp)
{
	if (!cp || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
		return -1;
	if (cp < 128)
		out[(*n)++] = cp;
	else if (cp < 2048) {
		out[(*n)++] = 0xc0 | (cp >> 6);
		out[(*n)++] = 0x80 | (cp & 63);
	} else if (cp < 65536) {
		out[(*n)++] = 0xe0 | (cp >> 12);
		out[(*n)++] = 0x80 | ((cp >> 6) & 63);
		out[(*n)++] = 0x80 | (cp & 63);
	} else {
		out[(*n)++] = 0xf0 | (cp >> 18);
		out[(*n)++] = 0x80 | ((cp >> 12) & 63);
		out[(*n)++] = 0x80 | ((cp >> 6) & 63);
		out[(*n)++] = 0x80 | (cp & 63);
	}
	return 0;
}
/** @brief Decode Bash literal quoting into a newly owned UTF-8 value. */
static int decode_export_value(const char *p, size_t len, char **value)
{
	int ansi = len >= 3 && p[0] == '$' && p[1] == '\'';
	char quote = ansi ? '\'' : '"', *out;
	size_t i, n = 0;
	if (ansi) {
		p += 2;
		len -= 2;
	} else {
		if (len < 2 || p[0] != '"')
			return -1;
		p++;
		len--;
	}
	if (!len || p[len - 1] != quote)
		return -1;
	len--;
	out = malloc(len * 4 + 1);
	if (!out)
		return -1;
	for (i = 0; i < len; i++) {
		unsigned char ch = p[i];
		unsigned long cp;
		int d, count, limit;
		if (!ch || ch == quote)
			goto fail;
		if (ch != '\\') {
			out[n++] = ch;
			continue;
		}
		if (++i == len)
			goto fail;
		ch = p[i];
		if (!ansi) {
			if (ch == '$' || ch == 0x60 || ch == '"' || ch == '\\')
				out[n++] = ch;
			else if (ch != '\n') {
				out[n++] = '\\';
				out[n++] = ch;
			}
			continue;
		}
		switch (ch) {
		case 'a':
			ch = '\a';
			break;
		case 'b':
			ch = '\b';
			break;
		case 'e':
		case 'E':
			ch = 27;
			break;
		case 'f':
			ch = '\f';
			break;
		case 'n':
			ch = '\n';
			break;
		case 'r':
			ch = '\r';
			break;
		case 't':
			ch = '\t';
			break;
		case 'v':
			ch = '\v';
			break;
		case '\\':
		case '\'':
		case '"':
		case '?':
			break;
		case 'c':
			if (++i == len)
				goto fail;
			ch = p[i] & 31;
			break;
		case 'x':
		case 'u':
		case 'U':
			limit = ch == 'x' ? 2 : ch == 'u' ? 4 :
							    8;
			cp = 0;
			count = 0;
			while (count < limit && i + 1 < len &&
			       (d = export_hex(p[i + 1])) >= 0) {
				cp = cp * 16 + d;
				i++;
				count++;
			}
			if (!count || (limit > 2 && count != limit))
				goto fail;
			if (limit == 2) {
				/* Bash hex escapes describe raw bytes, not Unicode code points. */
				if (!cp)
					goto fail;
				out[n++] = (char)cp;
			} else if (export_cp(out, &n, cp))
				goto fail;
			continue;
		default:
			if (ch >= '0' && ch <= '7') {
				cp = ch - '0';
				for (count = 1; count < 3 && i + 1 < len &&
						p[i + 1] >= '0' && p[i + 1] <= '7';
				     count++)
					cp = cp * 8 + (unsigned char)p[++i] - '0';
				ch = cp;
			} else
				out[n++] = '\\';
			break;
		}
		if (!ch)
			goto fail;
		out[n++] = ch;
	}
	out[n] = '\0';
	if (!is_utf8(out))
		goto fail;
	*value = out;
	return 0;
fail:
	free(out);
	return -1;
}
/** @brief Recognize a shell variable name without interpreting shell syntax. */
static int export_name_ok(const char *p, size_t len)
{
	size_t i;
	if (!len || !((p[0] >= 'a' && p[0] <= 'z') ||
		      (p[0] >= 'A' && p[0] <= 'Z') || p[0] == '_'))
		return 0;
	for (i = 1; i < len; i++)
		if (!((p[i] >= 'a' && p[i] <= 'z') ||
		      (p[i] >= 'A' && p[i] <= 'Z') ||
		      (p[i] >= '0' && p[i] <= '9') || p[i] == '_'))
			return 0;
	return 1;
}
/** @brief Decode restricted Bash exports and apply their complete snapshot. */
static int apply_export_stream(const char *buf, size_t len)
{
	struct export_pair *pairs = NULL, *grown;
	size_t off = 0, used = 0, cap = 0, i;
	int rc = -1;
#ifdef GIT_WINDOWS_NATIVE
	struct strvec environment = STRVEC_INIT;
#endif
	if ((len && buf[len - 1] != '\n') || memchr(buf, '\0', len))
		return -1;
	while (off < len) {
		const char *line = buf + off;
		const char *end = memchr(line, '\n', len - off);
		const char *name, *eq;
		size_t line_len, name_len;
		if (!end)
			goto cleanup;
		line_len = end - line;
		if (line_len < 11 || memcmp(line, "declare -x ", 11))
			goto cleanup;
		name = line + 11;
		eq = memchr(name, '=', line_len - 11);
		name_len = eq ? (size_t)(eq - name) : line_len - 11;
		if (!export_name_ok(name, name_len))
			goto cleanup;
		off = (size_t)(end - buf) + 1;
		if (!eq)
			continue;
		if (used == cap) {
			size_t next = cap ? cap * 2 : 32;
			if (next > SIZE_MAX / sizeof(*pairs))
				goto cleanup;
			grown = realloc(pairs, next * sizeof(*pairs));
			if (!grown)
				goto cleanup;
			pairs = grown;
			cap = next;
		}
		pairs[used].name = malloc(name_len + 1);
		if (!pairs[used].name)
			goto cleanup;
		memcpy(pairs[used].name, name, name_len);
		pairs[used].name[name_len] = '\0';
		pairs[used].value = NULL;
		used++;
		if (decode_export_value(eq + 1, end - eq - 1,
					&pairs[used - 1].value))
			goto cleanup;
	}
#ifdef GIT_WINDOWS_NATIVE
	for (i = 0; i < used; i++)
		strvec_pushf(&environment, "%s=%s", pairs[i].name, pairs[i].value);
	rc = mingw_replace_environment(environment.v);
#else
	if (clear_worker_environment())
		goto cleanup;
	for (i = 0; i < used; i++)
		if (setenv(pairs[i].name, pairs[i].value, 1))
			goto cleanup;
	rc = 0;
#endif
cleanup:
#ifdef GIT_WINDOWS_NATIVE
	strvec_clear(&environment);
#endif
	for (i = 0; i < used; i++) {
		free(pairs[i].name);
		free(pairs[i].value);
	}
	free(pairs);
	return rc;
}

/** @brief Apply validated cwd and environment fields to this worker process. */
int preload_context_apply(const char *buf, size_t len)
{
	size_t offset = 0;
	int seen_cwd = 0, seen_env = 0;

	while (offset < len) {
		const char *tag = buf + offset;
		const char *end = memchr(tag, '\0', len - offset);
		if (!end)
			return -1;
		offset = (size_t)(end - buf) + 1;
		if (!strcmp(tag, "cwd") && !seen_cwd) {
			size_t n;
			char *path;
			int bad;
			end = memchr(buf + offset, '\0', len - offset);
			if (!end)
				return -1;
			n = (size_t)(end - (buf + offset));
			if (n < 2 || buf[offset + n - 1] != '\n')
				return -1;
			path = malloc(n);
			if (!path)
				return -1;
			memcpy(path, buf + offset, n - 1);
			path[n - 1] = '\0';
			bad = !is_utf8(path) || chdir(path);
			free(path);
			if (bad)
				return -1;
			seen_cwd = 1;
			offset = (size_t)(end - buf) + 1;
		} else if (!strcmp(tag, "env-export") && !seen_env) {
			end = memchr(buf + offset, '\0', len - offset);
			if (!end || apply_export_stream(buf + offset,
							(size_t)(end - (buf + offset))))
				return -1;
			seen_env = 1;
			offset = (size_t)(end - buf) + 1;
		} else if (!strcmp(tag, "env") && !seen_env) {
			unsigned long size;
			if (len - offset < 4)
				return -1;
			size = (unsigned char)buf[offset] |
			       ((unsigned long)(unsigned char)buf[offset + 1] << 8) |
			       ((unsigned long)(unsigned char)buf[offset + 2] << 16) |
			       ((unsigned long)(unsigned char)buf[offset + 3] << 24);
			offset += 4;
			if (size > len - offset ||
			    apply_environment_block(buf + offset, size))
				return -1;
			offset += size;
			seen_env = 1;
		} else {
			return -1;
		}
	}
	return 0;
}
