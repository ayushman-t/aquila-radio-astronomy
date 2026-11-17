/* A stand-in LimeSuite with one LimeSDR Mini on it. */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#ifdef REAL_HDR
#include <lime/LimeSuite.h>
#else
typedef double float_type;
typedef void lms_device_t;
typedef char lms_info_str_t[256];
typedef char lms_name_t[16];
typedef struct {
    size_t handle; bool isTx; uint32_t channel; uint32_t fifoSize; float throughputVsLatency;
    enum { LMS_FMT_F32 = 0, LMS_FMT_I16, LMS_FMT_I12 } dataFmt;
    enum { LMS_LINK_FMT_DEFAULT = 0, LMS_LINK_FMT_I16, LMS_LINK_FMT_I12 } linkFmt;
} lms_stream_t;
typedef struct lms_stream_meta lms_stream_meta_t;
#endif
#include "fake_common.h"

static int the_dev;
static double rate = 0;
static unsigned gain = 0;
static size_t antenna = 1;

int LMS_GetDeviceList(lms_info_str_t *list) {
    if (knob("FAKE_RX_NONE")) return 0;
    if (list)
        strcpy(list[0], "LimeSDR Mini, media=USB 3.0, module=FT601, addr=24607:1027, "
                        "serial=1D3AFAKE0C7E51");
    return 1;
}
int LMS_Open(lms_device_t **device, const lms_info_str_t info, void *args) {
    (void)args;
    say("open %s", info);
    *device = (lms_device_t *)&the_dev;
    return 0;
}
int LMS_Close(lms_device_t *device) { (void)device; say("close"); return 0; }
int LMS_Init(lms_device_t *device) { (void)device; say("init"); return 0; }
int LMS_EnableChannel(lms_device_t *device, bool tx, size_t chan, bool enabled) {
    (void)device;
    say("channel %d %zu %d", (int)tx, chan, (int)enabled);
    return 0;
}
int LMS_SetSampleRate(lms_device_t *device, float_type r, size_t oversample) {
    (void)device;
    say("rate %.0f %zu", r, oversample);
    rate = r;
    return 0;
}
int LMS_GetSampleRate(lms_device_t *device, bool tx, size_t chan, float_type *host_Hz,
                      float_type *rf_Hz) {
    (void)device; (void)tx; (void)chan;
    *host_Hz = knob_num("FAKE_RX_RATE", rate);
    if (rf_Hz) *rf_Hz = rate * 8;
    return 0;
}
int LMS_SetLOFrequency(lms_device_t *device, bool tx, size_t chan, float_type f) {
    (void)device;
    say("frequency %d %zu %.0f", (int)tx, chan, f);
    return 0;
}
int LMS_GetAntennaList(lms_device_t *dev, bool tx, size_t chan, lms_name_t *list) {
    (void)dev; (void)tx; (void)chan;
    if (list) {
        strcpy(list[0], "NONE"); strcpy(list[1], "LNAH"); strcpy(list[2], "LNAL");
        strcpy(list[3], "LNAW");
    }
    return 4;
}
int LMS_SetAntenna(lms_device_t *dev, bool tx, size_t chan, size_t index) {
    (void)dev;
    say("antenna %d %zu %zu", (int)tx, chan, index);
    if (index == 255) antenna = 3;
    else antenna = index;
    return 0;
}
int LMS_GetAntenna(lms_device_t *dev, bool tx, size_t chan) {
    (void)dev; (void)tx; (void)chan;
    return (int)antenna;
}
int LMS_SetGaindB(lms_device_t *device, bool tx, size_t chan, unsigned g) {
    (void)device;
    say("gain %d %zu %u", (int)tx, chan, g);
    gain = g > 70 ? 70 : g;
    return 0;
}
int LMS_GetGaindB(lms_device_t *device, bool tx, size_t chan, unsigned *g) {
    (void)device; (void)tx; (void)chan;
    *g = gain;
    return 0;
}
int LMS_SetLPFBW(lms_device_t *device, bool tx, size_t chan, float_type bw) {
    (void)device;
    say("width %d %zu %.0f", (int)tx, chan, bw);
    return 0;
}
int LMS_Calibrate(lms_device_t *device, bool tx, size_t chan, double bw, unsigned flags) {
    (void)device;
    say("calibrate %d %zu %.0f %u", (int)tx, chan, bw, flags);
    return 0;
}
int LMS_SetupStream(lms_device_t *device, lms_stream_t *stream) {
    (void)device;
    say("stream tx=%d channel=%u fifo=%u bias=%.1f fmt=%d link=%d", (int)stream->isTx,
        stream->channel, stream->fifoSize, stream->throughputVsLatency, (int)stream->dataFmt,
        (int)stream->linkFmt);
    stream->handle = 77;
    return 0;
}
int LMS_DestroyStream(lms_device_t *dev, lms_stream_t *stream) {
    (void)dev;
    say("destroy %zu", stream->handle);
    return 0;
}
int LMS_StartStream(lms_stream_t *stream) { say("start %zu", stream->handle); return 0; }
int LMS_StopStream(lms_stream_t *stream) { say("stop %zu", stream->handle); return 0; }
int LMS_RecvStream(lms_stream_t *stream, void *samples, size_t count, lms_stream_meta_t *meta,
                   unsigned timeout_ms) {
    (void)stream; (void)meta; (void)timeout_ms;
    short *s = samples;
    if (knob("FAKE_RX_SILENT")) return -1;
    if (count > 5000) count = 5000;         /* a short read, as a real stream gives */
    for (size_t k = 0; k < count; k++) {
        int i = 16 * next_i();
        s[2 * k] = (short)i;
        s[2 * k + 1] = (short)-i;
    }
    return (int)count;
}
const char *LMS_GetLastErrorMessage(void) { return "fake LimeSuite error"; }
