#include "git-compat-util.h"
#include "preload-context.h"
#include "utf8.h"
#include "strvec.h"

#ifdef GIT_WINDOWS_NATIVE
# include <windows.h>
#endif

/**
 * @brief Validate UTF-8 NAME=VALUE entries and their double-NUL terminator.
 * @param buf Borrowed complete environment block, bounded by len.
 * @param len Block byte count, including its two final NUL bytes.
 * @return Zero if valid, -1 for malformed entries or termination.
 */
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
/**
 * @brief Remove ordinary inherited POSIX environment entries.
 * @return Zero after clearing the environment, -1 on invalid entries or removal failure.
 */
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

/**
 * @brief Validate and apply a complete UTF-8 binary environment snapshot.
 * @param buf Borrowed double-NUL-terminated NAME=VALUE block.
 * @param len Readable block size in bytes, including its terminators.
 * @return Zero on success, -1 on validation or environment application failure.
 */
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

/* Restricted Bash export -p decoder: never interpret commands or expansions. */
/* Native Windows snapshot application uses mingw_replace_environment(). */
/** One fully decoded exported variable, owned until snapshot application ends. */
struct export_pair {
	char *name; /**< Owned validated ASCII shell identifier. */
	char *value; /**< Owned decoded UTF-8 value, including the empty string. */
};
/**
 * @brief Decode one ASCII hexadecimal digit.
 * @param c Input byte, with either letter case accepted.
 * @return Digit value from zero through 15, or -1 for invalid input.
 */
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
/**
 * @brief Append a non-NUL Unicode scalar in UTF-8.
 * @param out Caller-owned destination with at least four available bytes.
 * @param n In/out number of bytes written to out.
 * @param cp Unicode scalar value to encode.
 * @return Zero on success, -1 for NUL, surrogate or out-of-range scalars.
 */
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
/**
 * @brief Decode a hexadecimal byte or Unicode escape after its prefix.
 * @param p Borrowed quoted value contents, bounded by len.
 * @param len Number of readable input bytes.
 * @param i In/out index of x, u or U; advances through consumed digits.
 * @param out Caller-owned output buffer with four bytes per input byte.
 * @param n In/out number of output bytes written.
 * @return Zero on success, -1 for invalid digits, NUL or Unicode scalars.
 */
static int decode_export_hex(const char *p, size_t len, size_t *i,
			     char *out, size_t *n)
{
	int limit = p[*i] == 'x' ? 2 : p[*i] == 'u' ? 4 : 8;
	unsigned long cp = 0;
	int d, count = 0;

	while (count < limit && *i + 1 < len &&
	       (d = export_hex(p[*i + 1])) >= 0) {
		cp = cp * 16 + d;
		(*i)++;
		count++;
	}
	if (!count || (limit > 2 && count != limit))
		return -1;
	if (limit != 2)
		return export_cp(out, n, cp);
	/* Bash hex escapes describe raw bytes, not Unicode code points. */
	if (!cp)
		return -1;
	out[(*n)++] = (char)cp;
	return 0;
}

/**
 * @brief Decode one escape in ordinary double quotes.
 * @param ch Byte immediately following the backslash.
 * @param out Caller-owned output buffer with space for two bytes.
 * @param n In/out byte count in out.
 * @return Zero; unknown escapes retain their backslash, and newline is continued.
 */
static int decode_double_quote_escape(unsigned char ch, char *out, size_t *n)
{
	if (strchr("$\x60\"\\", ch))
		out[(*n)++] = ch;
	else if (ch != '\n') {
		out[(*n)++] = '\\';
		out[(*n)++] = ch;
	}
	return 0;
}

/**
 * @brief Decode one ANSI-quoted octal byte escape.
 * @param p Borrowed value contents, bounded by len.
 * @param len Number of readable bytes.
 * @param i In/out index of the first octal digit, advanced over consumed digits.
 * @param out Caller-owned output buffer with space for one byte.
 * @param n In/out byte count in out.
 * @return Zero on success, -1 if the decoded byte is NUL.
 */
static int decode_export_octal(const char *p, size_t len, size_t *i,
			       char *out, size_t *n)
{
	unsigned int cp = p[*i] - '0';
	int count;

	for (count = 1; count < 3 && *i + 1 < len &&
	     p[*i + 1] >= '0' && p[*i + 1] <= '7'; count++)
		cp = cp * 8 + (unsigned char)p[++*i] - '0';
	if (!(unsigned char)cp)
		return -1;
	out[(*n)++] = (unsigned char)cp;
	return 0;
}

/**
 * @brief Decode one escape in ANSI-quoted Bash text.
 * @param p Borrowed value contents, bounded by len.
 * @param len Number of readable bytes.
 * @param i In/out index immediately after the backslash, advanced over the escape.
 * @param out Caller-owned output buffer with four bytes per input byte.
 * @param n In/out byte count in out.
 * @return Zero on success, -1 for an incomplete escape or encoded NUL.
 */
static int decode_ansi_escape(const char *p, size_t len, size_t *i,
			      char *out, size_t *n)
{
	static const char letters[] = "abeEfnrtv\\'\"?";
	static const char replacements[] = "\a\b\033\033\f\n\r\t\v\\'\"?";
	unsigned char ch = p[*i];
	const char *simple = strchr(letters, ch);

	if (simple)
		ch = replacements[simple - letters];
	else if (ch == 'c') {
		if (++*i == len)
			return -1;
		ch = p[*i] & 31;
	} else if (strchr("xuU", ch)) {
		return decode_export_hex(p, len, i, out, n);
	} else if (ch >= '0' && ch <= '7') {
		return decode_export_octal(p, len, i, out, n);
	} else {
		out[(*n)++] = '\\';
	}
	if (!ch)
		return -1;
	out[(*n)++] = ch;
	return 0;
}

/**
 * @brief Decode Bash literal quoting into a newly owned UTF-8 value.
 * @param p Borrowed complete quoted value, including its quote delimiters.
 * @param len Number of readable bytes in p.
 * @param value Receives newly allocated decoded text on success; caller frees it.
 * @return Zero on success, -1 for malformed quoting, invalid UTF-8 or allocation failure.
 */
static int decode_export_value(const char *p, size_t len, char **value)
{
	int ansi = len >= 3 && p[0] == '$' && p[1] == '\'';
	char quote = ansi ? '\'' : '"', *out;
	size_t i, n = 0, prefix = ansi ? 2 : 1;

	if ((!ansi && (len < 2 || p[0] != '"')) || len <= prefix)
		return -1;
	p += prefix;
	len -= prefix;
	if (p[len - 1] != quote)
		return -1;
	len--;
	out = malloc(st_add(st_mult(len, 4), 1));
	if (!out)
		return -1;
	for (i = 0; i < len; i++) {
		unsigned char ch = p[i];

		if (!ch || ch == quote)
			goto fail;
		if (ch != '\\') {
			out[n++] = ch;
			continue;
		}
		if (++i == len)
			goto fail;
		if (ansi ? decode_ansi_escape(p, len, &i, out, &n) :
		    decode_double_quote_escape(p[i], out, &n))
			goto fail;
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
/**
 * @brief Recognize a shell variable name without interpreting shell syntax.
 * @param p Borrowed name bytes, excluding any equal sign or terminator.
 * @param len Number of readable name bytes.
 * @return Nonzero for an ASCII shell identifier, zero otherwise.
 */
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
/**
 * @brief Recognize exported variable attributes without applying shell behavior.
 * @param line Borrowed complete declaration, excluding its newline.
 * @param len Number of readable bytes in line.
 * @param name Receives a borrowed pointer to the variable name and optional value.
 * @return Zero for a scalar, one for an array (not part of the child environment),
 *   or -1 for an invalid declaration or a missing export attribute.
 */
static int export_declaration(const char *line, size_t len, const char **name)
{
	const char *flags, *end;
	int exported = 0, array = 0;

	if (len >= 11 && !memcmp(line, "declare -x ", 11)) {
		*name = line + 11;
		return 0;
	}
	if (len < 11 || memcmp(line, "declare -", 9))
		return -1;
	flags = line + 9;
	end = memchr(flags, ' ', len - 9);
	if (!end)
		return -1;
	for (; flags < end; flags++) {
		if (!strchr("aAilnrtux", *flags))
			return -1;
		exported |= *flags == 'x';
		array |= *flags == 'a' || *flags == 'A';
	}
	*name = end + 1;
	return exported ? array : -1;
}

/**
 * @brief Decode one export declaration without evaluating shell syntax.
 * @param line Borrowed declaration, excluding its terminating newline.
 * @param len Number of readable bytes in line.
 * @param pair Zero-initialized result; receives owned name/value strings, or no name
 *   for an exported variable with no value. Caller frees both strings.
 * @return Zero on success, -1 for malformed text or allocation failure.
 */
static int decode_export_line(const char *line, size_t len, struct export_pair *pair)
{
	const char *name, *eq;
	size_t name_len;
	int array = export_declaration(line, len, &name);

	if (array < 0)
		return -1;
	eq = memchr(name, '=', line + len - name);
	name_len = eq ? (size_t)(eq - name) : (size_t)(line + len - name);
	if (!export_name_ok(name, name_len))
		return -1;
	if (!eq || array)
		return 0;
	pair->name = xmemdupz(name, name_len);
	if (decode_export_value(eq + 1, line + len - eq - 1, &pair->value)) {
		free(pair->name);
		pair->name = NULL;
		return -1;
	}
	return 0;
}

/**
 * @brief Apply a fully decoded snapshot after all declarations have validated.
 * @param pairs Borrowed array of complete name/value pairs.
 * @param nr Number of entries; zero replaces the environment with an empty snapshot.
 * @return Zero on success, -1 on environment application failure.
 */
static int apply_export_pairs(const struct export_pair *pairs, size_t nr)
{
	size_t i;
#ifdef GIT_WINDOWS_NATIVE
	struct strvec environment = STRVEC_INIT;
	int result;

	for (i = 0; i < nr; i++)
		strvec_pushf(&environment, "%s=%s", pairs[i].name, pairs[i].value);
	result = mingw_replace_environment(environment.v);
	strvec_clear(&environment);
	return result;
#else
	if (clear_worker_environment())
		return -1;
	for (i = 0; i < nr; i++)
		if (setenv(pairs[i].name, pairs[i].value, 1))
			return -1;
	return 0;
#endif
}

/**
 * @brief Decode and apply a restricted Bash export snapshot.
 * @param buf Borrowed UTF-8 declarations, each terminated by newline.
 * @param len Number of readable bytes; excludes the wire NUL terminator.
 * @return Zero on success, -1 for invalid text or environment application failure.
 */
static int apply_export_stream(const char *buf, size_t len)
{
	struct export_pair *pairs = NULL;
	size_t off = 0, used = 0, cap = 0, i;
	int result = -1;

	if ((len && buf[len - 1] != '\n') || memchr(buf, '\0', len))
		return -1;
	while (off < len) {
		const char *line = buf + off;
		const char *end = memchr(line, '\n', len - off);
		struct export_pair pair = { 0 };

		if (!end || decode_export_line(line, end - line, &pair))
			goto done;
		off = end - buf + 1;
		if (!pair.name)
			continue;
		ALLOC_GROW(pairs, used + 1, cap);
		pairs[used++] = pair;
	}
	result = apply_export_pairs(pairs, used);
done:
	for (i = 0; i < used; i++) {
		free(pairs[i].name);
		free(pairs[i].value);
	}
	free(pairs);
	return result;
}

/**
 * @brief Apply a path emitted directly by the caller's pwd builtin.
 * @param value Borrowed native UTF-8 path followed by exactly one framing newline.
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
 * @brief Apply one byte-counted binary environment field.
 * @param buf Borrowed context frame, bounded by len.
 * @param len Number of readable bytes.
 * @param offset In/out position of the little-endian byte count; advances past the block.
 * @return Zero on success, -1 for invalid framing or environment application failure.
 */
static int apply_binary_env_field(const char *buf, size_t len, size_t *offset)
{
	const unsigned char *p;
	uint32_t size;

	if (len - *offset < 4)
		return -1;
	p = (const unsigned char *)buf + *offset;
	size = (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
		((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
	*offset += 4;
	if (size > len - *offset || apply_environment_block(buf + *offset, size))
		return -1;
	*offset += size;
	return 0;
}

/**
 * @brief Apply validated cwd and environment fields to this worker process.
 * @param buf Borrowed encoded fields, excluding argv and the request end tag.
 * @param len Number of readable bytes; zero retains inherited context.
 * @return Zero on success, -1 for invalid fields or a context application failure.
 */
int preload_context_apply(const char *buf, size_t len)
{
	size_t offset = 0;
	int seen_cwd = 0, seen_env = 0;

	while (offset < len) {
		const char *tag = buf + offset;
		const char *end = memchr(tag, '\0', len - offset);
		const char *value;

		if (!end)
			return -1;
		offset = end - buf + 1;
		if (!strcmp(tag, "env") && !seen_env) {
			if (apply_binary_env_field(buf, len, &offset))
				return -1;
			seen_env = 1;
			continue;
		}
		value = buf + offset;
		end = memchr(value, '\0', len - offset);
		if (!end)
			return -1;
		if (!strcmp(tag, "cwd") && !seen_cwd) {
			if (apply_cwd_field(value, end - value))
				return -1;
			seen_cwd = 1;
		} else if (!strcmp(tag, "env-export") && !seen_env) {
			if (apply_export_stream(value, end - value))
				return -1;
			seen_env = 1;
		} else {
			return -1;
		}
		offset = end - buf + 1;
	}
	return 0;
}
