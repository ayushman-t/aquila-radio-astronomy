/* A stand-in libbladeRF with one bladeRF on it: three gain stages, like the x40. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#ifdef REAL_HDR
#include <libbladeRF.h>
#else
struct bladerf;
struct bladerf_metadata;
typedef int bladerf_channel;
typedef int bladerf_gain;
typedef int bladerf_gain_mode;
typedef int bladerf_channel_layout;
typedef int bladerf_format;
typedef unsigned int bladerf_sample_rate;
typedef unsigned int bladerf_bandwidth;
typedef uint64_t bladerf_frequency;
struct bladerf_devinfo {
    int backend; char serial[33]; uint8_t usb_bus; uint8_t usb_addr; unsigned int instance;
    char manufacturer[33]; char product[33];
};
struct bladerf_range { int64_t min; int64_t max; int64_t step; float scale; };
#endif
#include "fake_common.h"

static int the_dev;
static const char *stages[] = {"lna", "rxvga1", "rxvga2"};
static const struct bladerf_range ranges[] = {{0, 6, 3, 1}, {5, 30, 1, 1}, {0, 30, 1, 1}};
static const struct bladerf_range overall = {-1, 66, 1, 1};
static int stage_gain[3] = {0, 0, 0};

static int stage_of(const char *name) {
    for (int i = 0; i < 3; i++) if (!strcmp(name, stages[i])) return i;
    return -1;
}

int bladerf_get_device_list(struct bladerf_devinfo **devices) {
    if (knob("FAKE_RX_NONE")) return -7;
    struct bladerf_devinfo *d = calloc(1, sizeof(*d));
    strcpy(d->serial, "f12fakef12fakef12fakef12fakef12f");
    strcpy(d->product, "bladeRF x40");
    *devices = d;
    return 1;
}
void bladerf_free_device_list(struct bladerf_devinfo *devices) { free(devices); }
int bladerf_open(struct bladerf **device, const char *ident) {
    say("open [%s]", ident ? ident : "");
    if (knob("FAKE_RX_NONE")) return -7;
    *device = (struct bladerf *)&the_dev;
    return 0;
}
void bladerf_close(struct bladerf *dev) { (void)dev; say("close"); }
const char *bladerf_get_board_name(struct bladerf *dev) { (void)dev; return "bladerf1"; }
const char *bladerf_strerror(int error) { (void)error; return "fake bladeRF error"; }
int bladerf_set_frequency(struct bladerf *dev, bladerf_channel ch, bladerf_frequency f) {
    (void)dev;
    say("frequency %d %llu", ch, (unsigned long long)f);
    return 0;
}
int bladerf_set_sample_rate(struct bladerf *dev, bladerf_channel ch, bladerf_sample_rate rate,
                            bladerf_sample_rate *actual) {
    (void)dev;
    say("rate %d %u", ch, rate);
    *actual = (bladerf_sample_rate)knob_num("FAKE_RX_RATE", (double)rate);
    return 0;
}
int bladerf_set_bandwidth(struct bladerf *dev, bladerf_channel ch, bladerf_bandwidth bw,
                          bladerf_bandwidth *actual) {
    (void)dev;
    say("width %d %u", ch, bw);
    *actual = bw;
    return 0;
}
int bladerf_set_gain_mode(struct bladerf *dev, bladerf_channel ch, bladerf_gain_mode mode) {
    (void)dev;
    say("gainmode %d %d", ch, (int)mode);
    return 0;
}
int bladerf_set_gain(struct bladerf *dev, bladerf_channel ch, bladerf_gain gain) {
    (void)dev;
    say("gain %d %d", ch, gain);
    stage_gain[0] = 6; stage_gain[1] = 30; stage_gain[2] = gain - 36;
    return 0;
}
int bladerf_get_gain(struct bladerf *dev, bladerf_channel ch, bladerf_gain *gain) {
    (void)dev; (void)ch;
    *gain = stage_gain[0] + stage_gain[1] + stage_gain[2];
    return 0;
}
int bladerf_get_gain_range(struct bladerf *dev, bladerf_channel ch,
                           const struct bladerf_range **range) {
    (void)dev; (void)ch;
    *range = &overall;
    return 0;
}
int bladerf_get_gain_stages(struct bladerf *dev, bladerf_channel ch, const char **out,
                            size_t count) {
    (void)dev; (void)ch;
    for (size_t i = 0; i < count && i < 3; i++) out[i] = stages[i];
    return 3;
}
int bladerf_get_gain_stage_range(struct bladerf *dev, bladerf_channel ch, const char *stage,
                                 const struct bladerf_range **range) {
    (void)dev; (void)ch;
    int i = stage_of(stage);
    if (i < 0) return -1;
    *range = &ranges[i];
    return 0;
}
int bladerf_set_gain_stage(struct bladerf *dev, bladerf_channel ch, const char *stage,
                           bladerf_gain gain) {
    (void)dev;
    int i = stage_of(stage);
    say("stage %d %s %d", ch, stage, gain);
    if (i < 0) return -1;
    stage_gain[i] = gain;
    return 0;
}
int bladerf_get_gain_stage(struct bladerf *dev, bladerf_channel ch, const char *stage,
                           bladerf_gain *gain) {
    (void)dev; (void)ch;
    int i = stage_of(stage);
    if (i < 0) return -1;
    *gain = stage_gain[i];
    return 0;
}
int bladerf_set_bias_tee(struct bladerf *dev, bladerf_channel ch, bool enable) {
    (void)dev;
    say("bias %d %d", ch, (int)enable);
    return knob("FAKE_RX_NOBIAS") ? -8 : 0;
}
int bladerf_sync_config(struct bladerf *dev, bladerf_channel_layout layout, bladerf_format format,
                        unsigned int num_buffers, unsigned int buffer_size,
                        unsigned int num_transfers, unsigned int stream_timeout) {
    (void)dev;
    say("sync %d %d %u %u %u %u", (int)layout, (int)format, num_buffers, buffer_size,
        num_transfers, stream_timeout);
    return 0;
}
int bladerf_enable_module(struct bladerf *dev, bladerf_channel ch, bool enable) {
    (void)dev;
    say("module %d %d", ch, (int)enable);
    return 0;
}
int bladerf_sync_rx(struct bladerf *dev, void *samples, unsigned int num_samples,
                    struct bladerf_metadata *metadata, unsigned int timeout_ms) {
    (void)dev; (void)metadata; (void)timeout_ms;
    short *s = samples;
    if (knob("FAKE_RX_SILENT")) return -6;
    for (unsigned int k = 0; k < num_samples; k++) {
        int i = next_i();
        s[2 * k] = (short)i;
        s[2 * k + 1] = (short)-i;
    }
    return 0;
}
