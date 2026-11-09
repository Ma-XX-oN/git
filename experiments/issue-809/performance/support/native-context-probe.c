/* Apply context through the linked checkout's production implementation.
 * Arguments are UTF-8 NUL-delimited initial-environment and context-frame files.
 * Stdout receives the complete resulting Windows environment as UTF-8 entries.
 */
#include "git-compat-util.h"
#include "preload-context.h"
#include "strbuf.h"
#include <windows.h>

/** @brief Apply a real context frame and emit the resulting Windows block. */
int cmd_main(int argc, const char **argv)
{
	struct strbuf initial = STRBUF_INIT, frame = STRBUF_INIT;
	wchar_t *wide, *environment, *end;
	char *output;
	int count, bytes, result;
	DWORD written;

	if (argc != 3)
		return 90;
	if (strbuf_read_file(&initial, argv[1], 0) < 0 ||
	    strbuf_read_file(&frame, argv[2], 0) < 0)
		return 91;
	count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
		initial.buf, initial.len, NULL, 0);
	if (!count)
		return 92;
	ALLOC_ARRAY(wide, count);
	MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS,
		initial.buf, initial.len, wide, count);
	/* Fixture setup only: production replacement uses per-variable updates. */
	if (!SetEnvironmentStringsW(wide))
		return 93;
	free(wide);
	result = preload_apply_context(frame.buf, frame.len);
	if (result)
		return 94;
	environment = GetEnvironmentStringsW();
	if (!environment)
		return 95;
	for (end = environment; *end; end += wcslen(end) + 1)
		;
	count = end == environment ? 2 : end - environment + 1;
	bytes = WideCharToMultiByte(CP_UTF8, 0, environment, count,
		NULL, 0, NULL, NULL);
	ALLOC_ARRAY(output, bytes);
	WideCharToMultiByte(CP_UTF8, 0, environment, count,
		output, bytes, NULL, NULL);
	if (!WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), output, bytes,
		       &written, NULL) || written != bytes)
		return 96;
	FreeEnvironmentStringsW(environment);
	free(output);
	strbuf_release(&initial);
	strbuf_release(&frame);
	return 0;
}
