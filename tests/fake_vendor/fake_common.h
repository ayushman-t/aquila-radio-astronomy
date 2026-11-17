/* Shared by the stand-in vendor libraries the tests build: a call log and a known
   stream of samples. Sample k has I = 16 * ((k % 2000) - 1000) and Q = -I once it is
   full scale 16 bit; a receiver with 12 bit words gets the same without the 16. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void say(const char *fmt, ...) {
    const char *path = getenv("FAKE_RX_LOG");
    if (!path) return;
    FILE *f = fopen(path, "a");
    if (!f) return;
    va_list ap;
    va_start(ap, fmt);
    vfprintf(f, fmt, ap);
    va_end(ap);
    fputc('\n', f);
    fclose(f);
}

static long long sample_k = 0;

static int next_i(void) { return (int)(sample_k++ % 2000) - 1000; }

static int knob(const char *name) { return getenv(name) != NULL; }

static double knob_num(const char *name, double otherwise) {
    const char *v = getenv(name);
    return v ? atof(v) : otherwise;
}
