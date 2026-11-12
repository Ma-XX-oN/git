/* Non-Git pool fixture: READY handshake and owner lifetime only.
 * The native allocation benchmark kills each allocated process after timing;
 * command-channel correctness is covered separately with real workers.
 */
#include <stdio.h>
#include <stdlib.h>
/** Record owned fixture PID before delayed READY; tests use it to verify shutdown. */
static void record_pid(int argc, char **argv, unsigned long pid)
{
	char path[4096];
	FILE *file;
	if (!getenv("PRELOAD_FIXTURE_DELAY_MS") || argc != 3) return;
	if (snprintf(path, sizeof(path), "%s/fixture-pid-%lu", argv[2], pid) >= (int)sizeof(path)) return;
	file = fopen(path, "w");
	if (file) { fprintf(file, "%lu\n", pid); fclose(file); }
}
#ifdef _WIN32
#include <windows.h>
#include <fcntl.h>
#include <io.h>
int main(int argc, char **argv)
{
	const char *owner = getenv("PRELOAD_PID");
	HANDLE process = owner ? OpenProcess(SYNCHRONIZE, FALSE, strtoul(owner, NULL, 10)) : NULL;
	if (!process) return 1;
	_setmode(_fileno(stdout), _O_BINARY);
	record_pid(argc, argv, GetCurrentProcessId());
	if (getenv("PRELOAD_FIXTURE_DELAY_MS")) Sleep(strtoul(getenv("PRELOAD_FIXTURE_DELAY_MS"), NULL, 10));
	printf("READY %lu\n", (unsigned long)GetCurrentProcessId());
	fflush(stdout);
	WaitForSingleObject(process, INFINITE);
	CloseHandle(process);
	return 0;
}
#else
#include <unistd.h>
#include <signal.h>
#include <errno.h>
int main(int argc, char **argv)
{
	const char *owner = getenv("PRELOAD_PID");
	long pid = owner ? strtol(owner, NULL, 10) : 0;
	if (pid <= 0) return 1;
	record_pid(argc, argv, (unsigned long)getpid());
	if (getenv("PRELOAD_FIXTURE_DELAY_MS")) usleep(strtoul(getenv("PRELOAD_FIXTURE_DELAY_MS"), NULL, 10) * 1000);
	printf("READY %ld\n", (long)getpid());
	fflush(stdout);
	while (!kill((pid_t)pid, 0) || errno == EPERM) sleep(1);
	return 0;
}
#endif
