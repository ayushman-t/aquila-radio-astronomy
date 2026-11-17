/* A stand-in libiio with one ADALM-Pluto on it. */
#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>
#ifdef REAL_HDR
#include <iio.h>
#else
struct iio_scan_context; struct iio_context_info; struct iio_context;
struct iio_device; struct iio_channel; struct iio_buffer;
#endif
#include "fake_common.h"

struct iio_context_info { const char *text, *uri; };
struct iio_scan_context { int x; };
struct iio_context { int x; };
struct iio_device { const char *name; };
struct iio_channel { const char *name; bool output; struct iio_device *dev; };
struct iio_buffer { short *data; size_t n; };

static struct iio_context_info the_info = {
    "0456:b673 (Analog Devices Inc. PlutoSDR (ADALM-PLUTO)), serial=104473FAKE", "usb:1.2.5"};
static struct iio_scan_context the_scan;
static struct iio_context the_ctx;
static struct iio_device phy = {"ad9361-phy"}, rxd = {"cf-ad9361-lpc"};
static struct iio_channel lo = {"altvoltage0", true, &phy}, rf = {"voltage0", false, &phy},
    ci = {"voltage0", false, &rxd}, cq = {"voltage1", false, &rxd};
static long long rate = 0, width = 0, freq = 0;
static double gain = 0;

struct iio_scan_context *iio_create_scan_context(const char *backend, unsigned int flags) {
    say("scan %s %u", backend ? backend : "", flags);
    return &the_scan;
}
void iio_scan_context_destroy(struct iio_scan_context *ctx) { (void)ctx; }
ssize_t iio_scan_context_get_info_list(struct iio_scan_context *ctx,
                                       struct iio_context_info ***info) {
    (void)ctx;
    if (knob("FAKE_RX_NONE")) { *info = NULL; return 0; }
    *info = malloc(sizeof(**info));
    (*info)[0] = &the_info;
    return 1;
}
void iio_context_info_list_free(struct iio_context_info **info) { free(info); }
const char *iio_context_info_get_description(const struct iio_context_info *info) {
    return info->text;
}
const char *iio_context_info_get_uri(const struct iio_context_info *info) { return info->uri; }
struct iio_context *iio_create_context_from_uri(const char *uri) {
    say("open %s", uri);
    return &the_ctx;
}
void iio_context_destroy(struct iio_context *ctx) { (void)ctx; say("close"); }
int iio_context_set_timeout(struct iio_context *ctx, unsigned int ms) {
    (void)ctx; (void)ms; return 0;
}
struct iio_device *iio_context_find_device(const struct iio_context *ctx, const char *name) {
    (void)ctx;
    return !strcmp(name, phy.name) ? &phy : !strcmp(name, rxd.name) ? &rxd : NULL;
}
struct iio_channel *iio_device_find_channel(const struct iio_device *dev, const char *name,
                                            bool output) {
    struct iio_channel *all[] = {&lo, &rf, &ci, &cq};
    for (int i = 0; i < 4; i++)
        if (all[i]->dev == dev && all[i]->output == output && !strcmp(all[i]->name, name))
            return all[i];
    return NULL;
}
int iio_channel_attr_write_longlong(const struct iio_channel *chn, const char *attr,
                                    long long val) {
    say("set %s.%s %lld", chn->name, attr, val);
    if (!strcmp(attr, "sampling_frequency")) rate = val;
    else if (!strcmp(attr, "rf_bandwidth")) width = val;
    else if (!strcmp(attr, "frequency")) freq = val;
    else return -22;
    return 0;
}
int iio_channel_attr_read_longlong(const struct iio_channel *chn, const char *attr,
                                   long long *val) {
    (void)chn;
    if (strcmp(attr, "sampling_frequency")) return -22;
    *val = (long long)knob_num("FAKE_RX_RATE", (double)rate);
    return 0;
}
ssize_t iio_channel_attr_write(const struct iio_channel *chn, const char *attr,
                               const char *src) {
    say("set %s.%s %s", chn->name, attr, src);
    return (ssize_t)strlen(src);
}
ssize_t iio_channel_attr_read(const struct iio_channel *chn, const char *attr, char *dst,
                              size_t len) {
    (void)chn;
    if (strcmp(attr, "hardwaregain_available")) return -22;
    snprintf(dst, len, "[-3 1 71]");
    return (ssize_t)strlen(dst) + 1;
}
int iio_channel_attr_write_double(const struct iio_channel *chn, const char *attr, double val) {
    say("set %s.%s %g", chn->name, attr, val);
    gain = val;
    return 0;
}
int iio_channel_attr_read_double(const struct iio_channel *chn, const char *attr, double *val) {
    (void)chn; (void)attr;
    *val = gain;
    return 0;
}
void iio_channel_enable(struct iio_channel *chn) { say("enable %s", chn->name); }
struct iio_buffer *iio_device_create_buffer(const struct iio_device *dev, size_t count,
                                            bool cyclic) {
    (void)dev; (void)cyclic;
    struct iio_buffer *b = malloc(sizeof(*b));
    b->n = count;
    b->data = malloc(count * 4);
    return b;
}
void iio_buffer_destroy(struct iio_buffer *buf) { free(buf->data); free(buf); }
ssize_t iio_buffer_refill(struct iio_buffer *buf) {
    static int first = 1;
    if (knob("FAKE_RX_SILENT")) return -110;
    for (size_t k = 0; k < buf->n; k++) {
        /* the first buffer is from before the tuning: the reader must drop it */
        int i = first ? 2047 : next_i();
        buf->data[2 * k] = (short)i;
        buf->data[2 * k + 1] = (short)-i;
    }
    first = 0;
    return (ssize_t)(buf->n * 4);
}
void *iio_buffer_start(const struct iio_buffer *buf) { return buf->data; }
void *iio_buffer_end(const struct iio_buffer *buf) { return buf->data + 2 * buf->n; }
ptrdiff_t iio_buffer_step(const struct iio_buffer *buf) { (void)buf; return 4; }
void iio_strerror(int err, char *dst, size_t len) { snprintf(dst, len, "fake error %d", err); }
