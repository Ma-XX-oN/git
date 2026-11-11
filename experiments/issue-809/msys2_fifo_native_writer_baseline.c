/* RWF 809: FIFO native WriteFile() wake/CPU RED control.
 * Build in Git-for-Windows SDK: gcc -std=gnu11 -O2 -Wall -Wextra
 * -Werror -pthread ... -o ...
 * Bypasses the MSYS2 write() notification intentionally; stock/patched comparison only.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <windows.h>
#include <io.h>

enum { MAX_WRITERS = 48 };
typedef struct {
  pthread_mutex_t mu;
  pthread_cond_t cv;
  int *writer_fds;
  int count;
  int iterations;
  int idle_ms;
  bool requested;
  int request;
  int error;
  atomic_ullong write_started_us;
} shared_t;

static uint64_t micros(clockid_t clock) {
  struct timespec ts;
  if (clock_gettime(clock, &ts) != 0) {
    perror("clock_gettime");
    exit(2);
  }
  return (uint64_t)ts.tv_sec * 1000000u + (uint64_t)ts.tv_nsec / 1000u;
}

static void sleep_ms(int milliseconds) {
  struct timespec ts = {
    .tv_sec = milliseconds / 1000,
    .tv_nsec = (milliseconds % 1000) * 1000000L
  };
  while (nanosleep(&ts, &ts) != 0 && errno == EINTR) {}
}

static void *writer_thread(void *ptr) {
  shared_t *s = ptr;
  for (int i = 0; i < s->iterations; ++i) {
    pthread_mutex_lock(&s->mu);
    while (!s->requested) pthread_cond_wait(&s->cv, &s->mu);
    int request = s->request;
    pthread_mutex_unlock(&s->mu);

    sleep_ms(i == 0 ? s->idle_ms : 2);
    char payload = (char)('A' + request % 26);
    atomic_store_explicit(&s->write_started_us, micros(CLOCK_MONOTONIC),
                          memory_order_release);
    HANDLE native = (HANDLE)(intptr_t)_get_osfhandle(s->writer_fds[request % s->count]);
    if (!native || native == INVALID_HANDLE_VALUE) {
      fputs("invalid native FIFO writer handle\n", stderr);
      exit(2);
    }
    DWORD sent = 0;
    BOOL ok = WriteFile(native, &payload, 1, &sent, NULL);
    ssize_t n = ok ? (ssize_t)sent : -1;
    pthread_mutex_lock(&s->mu);
    if (n != 1) s->error = errno ? errno : EIO;
    s->requested = false;
    pthread_cond_broadcast(&s->cv);
    pthread_mutex_unlock(&s->mu);
    if (n != 1) break;
  }
  return NULL;
}

static int compare_u64(const void *a, const void *b) {
  uint64_t lhs = *(const uint64_t *)a, rhs = *(const uint64_t *)b;
  return (lhs > rhs) - (lhs < rhs);
}

int main(int argc, char **argv) {
  if (argc != 4) {
    fprintf(stderr, "usage: %s WRITERS ITERATIONS IDLE_MS\n", argv[0]);
    return 2;
  }
  char trailer;
  int writers = 0, iterations = 0, idle_ms = 0;
  if (sscanf(argv[1], "%d%c", &writers, &trailer) != 1 ||
      sscanf(argv[2], "%d%c", &iterations, &trailer) != 1 ||
      sscanf(argv[3], "%d%c", &idle_ms, &trailer) != 1 ||
      writers < 1 || writers > MAX_WRITERS ||
      iterations < 3 || iterations > 200000 ||
      idle_ms < 1 || idle_ms > 60000) {
    fputs("invalid benchmark parameters\n", stderr);
    return 2;
  }

  char tmp[] = "/tmp/git809-fifo-baseline-XXXXXX";
  if (!mkdtemp(tmp)) { perror("mkdtemp"); return 2; }
  char path[sizeof(tmp) + 16];
  int plen = snprintf(path, sizeof(path), "%s/fifo", tmp);
  if (plen < 0 || (size_t)plen >= sizeof(path)) return 2;
  if (mkfifo(path, 0600) != 0) { perror("mkfifo"); return 2; }
  int reader = open(path, O_RDONLY | O_NONBLOCK);
  if (reader < 0) { perror("open reader"); return 2; }

  int *fds = calloc((size_t)writers, sizeof(*fds));
  uint64_t *latencies = calloc((size_t)iterations - 1, sizeof(*latencies));
  if (!fds || !latencies) { perror("calloc"); return 2; }
  for (int i = 0; i < writers; ++i) {
    fds[i] = open(path, O_WRONLY | O_NONBLOCK);
    if (fds[i] < 0) { perror("open writer"); return 2; }
  }

  int flags = fcntl(reader, F_GETFL);
  if (flags < 0 || fcntl(reader, F_SETFL, flags & ~O_NONBLOCK) != 0) {
    perror("fcntl reader blocking"); return 2;
  }

  shared_t sh = {
    .mu = PTHREAD_MUTEX_INITIALIZER,
    .cv = PTHREAD_COND_INITIALIZER,
    .writer_fds = fds, .count = writers,
    .iterations = iterations, .idle_ms = idle_ms
  };
  atomic_init(&sh.write_started_us, 0);
  pthread_t thread;
  if (pthread_create(&thread, NULL, writer_thread, &sh) != 0) {
    fputs("pthread_create failed\n", stderr); return 2;
  }

  uint64_t wall0 = micros(CLOCK_MONOTONIC);
  uint64_t cpu0 = micros(CLOCK_PROCESS_CPUTIME_ID);
  uint64_t idle_wall_us = 0, idle_cpu_us = 0;
  for (int i = 0; i < iterations; ++i) {
    pthread_mutex_lock(&sh.mu);
    while (sh.requested) pthread_cond_wait(&sh.cv, &sh.mu);
    if (sh.error) { pthread_mutex_unlock(&sh.mu); fprintf(stderr,"writer error=%d\n", sh.error); return 2; }
    sh.requested = true;
    sh.request = i;
    pthread_cond_broadcast(&sh.cv);
    pthread_mutex_unlock(&sh.mu);

    char payload = 0;
    ssize_t got = read(reader, &payload, 1);
    uint64_t finished = micros(CLOCK_MONOTONIC);
    if (got != 1 || payload != (char)('A' + i % 26)) {
      fprintf(stderr,"read failed iter=%d got=%zd payload=%d errno=%d\n", i, got, (int)payload, errno);
      return 2;
    }
    uint64_t started = atomic_load_explicit(&sh.write_started_us,
                                             memory_order_acquire);
    if (finished < started || !started) {
      fputs("invalid timing order\n", stderr); return 2;
    }
    if (i == 0) {
      idle_wall_us = finished - wall0;
      idle_cpu_us = micros(CLOCK_PROCESS_CPUTIME_ID) - cpu0;
    } else {
      latencies[i-1] = finished - started;
    }
  }
  pthread_join(thread, NULL);
  uint64_t cpu_us = micros(CLOCK_PROCESS_CPUTIME_ID) - cpu0;
  uint64_t wall_us = micros(CLOCK_MONOTONIC) - wall0;

  qsort(latencies, (size_t)iterations - 1, sizeof(*latencies), compare_u64);
  size_t samples = (size_t)iterations - 1;
  size_t p95 = ((samples * 95 + 99) / 100) - 1;
  uint64_t sum = 0;
  for (size_t i = 0; i < samples; ++i) sum += latencies[i];
  printf("BASELINE_FIFO writers=%d iterations=%d idle_ms=%d "
         "p50_wake_us=%" PRIu64 " p95_wake_us=%" PRIu64
         " max_wake_us=%" PRIu64 " mean_wake_us=%" PRIu64
         " idle_wall_us=%" PRIu64 " idle_cpu_us=%" PRIu64
         " total_wall_us=%" PRIu64 " total_cpu_us=%" PRIu64 "\n",
         writers, iterations, idle_ms, latencies[samples / 2],
         latencies[p95], latencies[samples - 1], sum / samples,
         idle_wall_us, idle_cpu_us, wall_us, cpu_us);
  fflush(stdout);
  for (int i = 0; i < writers; ++i) close(fds[i]);
  close(reader);
  free(fds);
  free(latencies);
  if (unlink(path) != 0 || rmdir(tmp) != 0) {
    perror("fifo cleanup"); return 2;
  }
  return 0;
}
