#include "test-tool.h"
#include "preload-context.h"
#include "preload-transport.h"
#include "strbuf.h"

#ifdef GIT_WINDOWS_NATIVE
/**
 * @brief Apply a real context frame and emit the resulting Windows environment.
 * @param argc Argument count, including the fixture mode; must be three.
 * @param argv Borrowed mode, initial-environment path and context-frame path.
 * @return Zero on success, or a fixture error from 90 through 96.
 */
static int windows_context_fixture(int argc, const char **argv)
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
	result = preload_context_apply(frame.buf, frame.len);
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
		       &written, NULL) ||
	    written != bytes)
		return 96;
	FreeEnvironmentStringsW(environment);
	free(output);
	strbuf_release(&initial);
	strbuf_release(&frame);
	return 0;
}
#endif

/**
 * @brief Apply a context fixture through the production decoder and print its result.
 * @param path Borrowed filename containing the encoded context fields.
 * @return Zero on successful application, one for invalid context; read errors are fatal.
 */
static int context_fixture(const char *path)
{
	struct strbuf frame = STRBUF_INIT;
	int result;

	if (strbuf_read_file(&frame, path, 0) < 0)
		die_errno("cannot read context fixture");
	result = preload_context_apply(frame.buf, frame.len);
	strbuf_release(&frame);
	if (result)
		return 1;
	printf("cwd=%s\n", xgetcwd());
	printf("value=%s\n", getenv("PRELOAD_TEST_VALUE") ?
				     getenv("PRELOAD_TEST_VALUE") :
				     "<unset>");
	return 0;
}

/**
 * @brief Select real production transport channels and echo their payload.
 * @param location Borrowed Windows pipe stem or existing directory with test FIFOs.
 * @return A preload_transport_result, including system error for failed payload delivery.
 */
static int transport_fixture(const char *location)
{
	static const struct preload_channel_spec channels[] = {
		{ "request", 0 }, { "reply", 1 }
	};
	static const size_t order[] = { 0, 1 };
	struct preload_transport_config config = {
		location, channels, order, ARRAY_SIZE(channels), 2000, '0'
	};
	struct preload_transport *transport = NULL;
	char buffer[64];
	ssize_t count;
	int result;

	result = preload_transport_start(&transport, &config);
	if (!result) {
		puts("READY");
		fflush(stdout);
		result = preload_transport_wait(transport);
	}
	if (!result) {
		count = preload_transport_read(transport, 0, buffer, sizeof(buffer));
		if (count <= 0 || preload_transport_write(transport, 1, buffer, count) != count ||
		    preload_transport_flush(transport, 1))
			result = PRELOAD_TRANSPORT_SYSTEM_ERROR;
	}
	preload_transport_release(transport);
	return result;
}

/**
 * @brief Exercise the linked context and transport implementations.
 * @param argc Number of argv entries, including the preload fixture name.
 * @param argv Borrowed fixture name, mode and mode-specific paths.
 * @return Fixture status; invalid usage terminates through the test-tool usage handler.
 */
int cmd__preload(int argc, const char **argv)
{
#ifdef GIT_WINDOWS_NATIVE
	if (argc == 4 && !strcmp(argv[1], "windows-context"))
		return windows_context_fixture(argc - 1, argv + 1);
#endif
	if (argc != 3)
		usage("test-tool preload (context <frame> | transport <location>)");
	if (!strcmp(argv[1], "context"))
		return context_fixture(argv[2]);
	if (!strcmp(argv[1], "transport"))
		return transport_fixture(argv[2]);
	usage("test-tool preload (context <frame> | transport <location>)");
}
