#!/usr/bin/env python3
"""Force cancellation races using the native worker's actual rendezvous helpers."""
from pathlib import Path
import os
import subprocess
import tempfile

FIXTURE = r'''
struct fixture {
    struct git809_connections connection;
    HANDLE pipes[5][2], clients[5][2], gate;
    int duplicate;
};

/** @brief Publish two synchronous instances and connect both fixture clients. */
static void open_pair(struct fixture *f, int channel)
{
    char name[128];
    int i;
    snprintf(name, sizeof(name), "\\\\.\\pipe\\git809-peer-fixture-%lu-%d",
             GetCurrentProcessId(), channel);
    for (i = 0; i < 2; i++) {
        f->pipes[channel][i] = CreateNamedPipeA(name, PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_WAIT, 2, 256, 256, 2000, NULL);
        assert(f->pipes[channel][i] != INVALID_HANDLE_VALUE);
    }
    for (i = 0; i < 2; i++) {
        f->clients[channel][i] = CreateFileA(name, GENERIC_READ | GENERIC_WRITE,
            0, NULL, OPEN_EXISTING, 0, NULL);
        assert(f->clients[channel][i] != INVALID_HANDLE_VALUE);
    }
    for (i = 0; i < 2; i++)
        assert(ConnectNamedPipe(f->pipes[channel][i], NULL) ||
               GetLastError() == ERROR_PIPE_CONNECTED);
}

/** @brief Complete a real marker read before entering the rendezvous. */
static void marker(struct fixture *f, int channel, int index)
{
    DWORD n;
    char value;
    assert(WriteFile(f->clients[channel][index], "1", 1, &n, NULL) && n == 1);
    assert(ReadFile(f->pipes[channel][index], &value, 1, &n, NULL) && n == 1);
    assert(value == '1');
    f->connection.channels[channel].marker[index] = value;
    f->connection.channels[channel].result[index] = CANDIDATE_MARKED;
}

/** @brief Delay the losing candidate until cancellation has found no pending I/O. */
static DWORD WINAPI loser(LPVOID data)
{
    struct fixture *f = data;
    DWORD n;
    char value;
    WaitForSingleObject(f->gate, INFINITE);
    if (!f->duplicate) {
        /* Models preemption after checking the winner but before ReadFile. */
        assert(!ReadFile(f->pipes[1][1], &value, 1, &n, NULL));
        assert(GetLastError() == ERROR_OPERATION_ABORTED);
    }
    git809_rendezvous(&f->connection, 1, 1, 1);
    return 0;
}

/** @brief Run the marker winner's real cancellation/acknowledgement loop. */
static DWORD WINAPI winner(LPVOID data)
{
    struct fixture *f = data;
    git809_rendezvous(&f->connection, 1, 1, 0);
    return 0;
}

/** @brief Test cancellation before read submission and after read completion. */
static void cancellation_case(int duplicate)
{
    struct fixture f = {0};
    HANDLE threads[2];
    DWORD began = GetTickCount();
    int i;
    f.connection.pipes = f.pipes;
    f.duplicate = duplicate;
    f.gate = CreateEvent(NULL, TRUE, FALSE, NULL);
    open_pair(&f, 1);
    marker(&f, 1, 0);
    if (duplicate)
        marker(&f, 1, 1);
    f.connection.channels[1].winner = 1;
    threads[1] = CreateThread(NULL, 0, loser, &f, 0, NULL);
    threads[0] = CreateThread(NULL, 0, winner, &f, 0, NULL);
    while (!git809_atomic_read(&f.connection.arrived[0])) {
        assert(GetTickCount() - began < 2000);
        YieldProcessor();
    }
    assert(!CancelIoEx(f.pipes[1][1], NULL));
    assert(GetLastError() == ERROR_NOT_FOUND);
    SetEvent(f.gate);
    assert(WaitForMultipleObjects(2, threads, TRUE, 2000) == WAIT_OBJECT_0);
    assert(f.connection.failure == (duplicate ? TRANSPORT_DUPLICATE_MARKER : 0));
    if (duplicate) {
        assert(f.connection.channels[1].marker[0] == '1');
        assert(f.connection.channels[1].marker[1] == '1');
    } else {
        assert(f.connection.accepted[1] == f.pipes[1][0]);
    }
    for (i = 0; i < 2; i++) {
        CloseHandle(threads[i]);
        CloseHandle(f.clients[1][i]);
        CloseHandle(f.pipes[1][i]);
    }
    CloseHandle(f.gate);
    printf("GIT809_PEER_RENDEZVOUS_PASS %s\n",
           duplicate ? "already-finished-duplicate-read" : "cancel-before-read");
}

/** @brief Traverse five generations with alternating winners and arrival orders. */
static DWORD WINAPI generations(LPVOID data)
{
    struct git809_connector *argument = data;
    int step, spin;
    for (step = 0; step < 5; step++) {
        if ((step + argument->index) % 2)
            for (spin = 0; spin < 100; spin++)
                YieldProcessor();
        assert(!git809_rendezvous(argument->connections, step, step + 1,
                                  argument->index));
    }
    return 0;
}

/** @brief Check that old acknowledgements cannot release a later generation. */
static void generation_case(void)
{
    struct fixture f = {0};
    HANDLE threads[2];
    int i, round, channel;
    f.connection.pipes = f.pipes;
    for (channel = 0; channel < 5; channel++)
        open_pair(&f, channel);
    for (round = 0; round < 32; round++) {
        memset(&f.connection, 0, sizeof(f.connection));
        f.connection.pipes = f.pipes;
        for (channel = 0; channel < 5; channel++) {
            int chosen = (channel + round) % 2;
            f.connection.channels[channel].winner = chosen + 1;
            f.connection.channels[channel].result[chosen] = CANDIDATE_MARKED;
            f.connection.channels[channel].marker[chosen] = '1';
        }
        for (i = 0; i < 2; i++) {
            f.connection.connectors[i].connections = &f.connection;
            f.connection.connectors[i].index = i;
            threads[i] = CreateThread(NULL, 0, generations,
                                     &f.connection.connectors[i], 0, NULL);
        }
        assert(WaitForMultipleObjects(2, threads, TRUE, 2000) == WAIT_OBJECT_0);
        assert(!f.connection.failure && f.connection.completed == 5);
        for (channel = 0; channel < 5; channel++)
            assert(f.connection.accepted[channel] == f.pipes[channel][(channel + round) % 2]);
        for (i = 0; i < 2; i++)
            CloseHandle(threads[i]);
    }
    for (channel = 0; channel < 5; channel++)
        for (i = 0; i < 2; i++) {
            CloseHandle(f.clients[channel][i]);
            CloseHandle(f.pipes[channel][i]);
        }
    puts("GIT809_PEER_RENDEZVOUS_PASS mixed-winners-repeated-generations");
}

/** @brief Run deterministic fixtures against the compiled production functions. */
int main(void)
{
    cancellation_case(0);
    cancellation_case(1);
    generation_case();
    return 0;
}
'''


def main():
    """Compile the production helper block with fixture-only scheduling gates."""
    if os.name != "nt":
        raise RuntimeError("Windows native rendezvous test only")
    root = Path(__file__).resolve().parents[2]
    source = (root / "compat/mingw.c").read_text(encoding="utf-8")
    begin = source.index("enum git809_candidate_result {")
    end = source.index("/*\n * The manager transfers", begin)
    prelude = ('#define WIN32_LEAN_AND_MEAN\n#include <windows.h>\n#include <stdio.h>\n#include <stdlib.h>\n'
               '#include <assert.h>\n#include <string.h>\n'
               '#define trace_printf_key(key, ...) do { if (0) fprintf(stderr, __VA_ARGS__); } while (0)\n'
               '#define BUG(...) abort()\n')
    with tempfile.TemporaryDirectory(prefix="git809-peer-rendezvous-") as directory:
        source_file = Path(directory) / "fixture.c"
        executable = Path(directory) / "fixture.exe"
        source_file.write_text(prelude + source[begin:end] + FIXTURE,
                               encoding="utf-8", newline="\n")
        subprocess.run(["gcc", "-O2", "-Wall", "-Wextra", "-Wno-unused-function",
                        str(source_file), "-o", str(executable)], check=True, timeout=30)
        subprocess.run([str(executable)], check=True, timeout=10)


if __name__ == "__main__":
    main()
