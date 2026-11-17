/* A stand-in sdrplay_api with one RSP on it. It hands its samples over from inside
   sdrplay_api_Init, on the caller's thread, so it needs no threads of its own. */
#include <stddef.h>
#ifdef REAL_HDR
#include "sdrplay_api.h"
#else
typedef void *HANDLE;
typedef int sdrplay_api_ErrT;
typedef int sdrplay_api_TunerSelectT;
typedef int sdrplay_api_EventT;
typedef unsigned int sdrplay_api_ReasonForUpdateT;
typedef unsigned int sdrplay_api_ReasonForUpdateExtension1T;
typedef struct {
    char SerNo[64]; unsigned char hwVer; int tuner; int rspDuoMode; unsigned char valid;
    double rspDuoSampleFreq; HANDLE dev;
} sdrplay_api_DeviceT;
typedef struct { double fsHz; unsigned char syncUpdate; unsigned char reCal; } sdrplay_api_FsFreqT;
typedef struct { unsigned int sampleNum; unsigned int period; } sdrplay_api_SyncUpdateT;
typedef struct {
    unsigned char resetGainUpdate; unsigned char resetRfUpdate; unsigned char resetFsUpdate;
} sdrplay_api_ResetFlagsT;
typedef struct { unsigned char rfNotchEnable; unsigned char rfDabNotchEnable; } sdrplay_api_Rsp1aParamsT;
typedef struct { unsigned char extRefOutputEn; } sdrplay_api_Rsp2ParamsT;
typedef struct { int extRefOutputEn; } sdrplay_api_RspDuoParamsT;
typedef struct {
    unsigned char hdrEnable; unsigned char biasTEnable; int antennaSel;
    unsigned char rfNotchEnable; unsigned char rfDabNotchEnable;
} sdrplay_api_RspDxParamsT;
typedef struct {
    double ppm; sdrplay_api_FsFreqT fsFreq; sdrplay_api_SyncUpdateT syncUpdate;
    sdrplay_api_ResetFlagsT resetFlags; int mode; unsigned int samplesPerPkt;
    sdrplay_api_Rsp1aParamsT rsp1aParams; sdrplay_api_Rsp2ParamsT rsp2Params;
    sdrplay_api_RspDuoParamsT rspDuoParams; sdrplay_api_RspDxParamsT rspDxParams;
} sdrplay_api_DevParamsT;
typedef struct { float curr; float max; float min; } sdrplay_api_GainValuesT;
typedef struct {
    int gRdB; unsigned char LNAstate; unsigned char syncUpdate; int minGr;
    sdrplay_api_GainValuesT gainVals;
} sdrplay_api_GainT;
typedef struct { double rfHz; unsigned char syncUpdate; } sdrplay_api_RfFreqT;
typedef struct {
    unsigned char dcCal; unsigned char speedUp; int trackTime; int refreshRateTime;
} sdrplay_api_DcOffsetTunerT;
typedef struct {
    int bwType; int ifType; int loMode; sdrplay_api_GainT gain; sdrplay_api_RfFreqT rfFreq;
    sdrplay_api_DcOffsetTunerT dcOffsetTuner;
} sdrplay_api_TunerParamsT;
typedef struct { unsigned char DCenable; unsigned char IQenable; } sdrplay_api_DcOffsetT;
typedef struct {
    unsigned char enable; unsigned char decimationFactor; unsigned char wideBandSignal;
} sdrplay_api_DecimationT;
typedef struct {
    int enable; int setPoint_dBfs; unsigned short attack_ms; unsigned short decay_ms;
    unsigned short decay_delay_ms; unsigned short decay_threshold_dB; int syncUpdate;
} sdrplay_api_AgcT;
typedef struct {
    sdrplay_api_DcOffsetT dcOffset; sdrplay_api_DecimationT decimation; sdrplay_api_AgcT agc;
    int adsbMode;
} sdrplay_api_ControlParamsT;
typedef struct { unsigned char biasTEnable; } sdrplay_api_Rsp1aTunerParamsT;
typedef struct {
    unsigned char biasTEnable; int amPortSel; int antennaSel; unsigned char rfNotchEnable;
} sdrplay_api_Rsp2TunerParamsT;
typedef struct { unsigned char resetGainUpdate; unsigned char resetRfUpdate; } sdrplay_api_RspDuo_ResetSlaveFlagsT;
typedef struct {
    unsigned char biasTEnable; int tuner1AmPortSel; unsigned char tuner1AmNotchEnable;
    unsigned char rfNotchEnable; unsigned char rfDabNotchEnable;
    sdrplay_api_RspDuo_ResetSlaveFlagsT resetSlaveFlags;
} sdrplay_api_RspDuoTunerParamsT;
typedef struct { int hdrBw; } sdrplay_api_RspDxTunerParamsT;
typedef struct {
    sdrplay_api_TunerParamsT tunerParams; sdrplay_api_ControlParamsT ctrlParams;
    sdrplay_api_Rsp1aTunerParamsT rsp1aTunerParams; sdrplay_api_Rsp2TunerParamsT rsp2TunerParams;
    sdrplay_api_RspDuoTunerParamsT rspDuoTunerParams; sdrplay_api_RspDxTunerParamsT rspDxTunerParams;
} sdrplay_api_RxChannelParamsT;
typedef struct {
    sdrplay_api_DevParamsT *devParams; sdrplay_api_RxChannelParamsT *rxChannelA;
    sdrplay_api_RxChannelParamsT *rxChannelB;
} sdrplay_api_DeviceParamsT;
typedef struct {
    unsigned int firstSampleNum; int grChanged; int rfChanged; int fsChanged;
    unsigned int numSamples;
} sdrplay_api_StreamCbParamsT;
typedef struct { unsigned int gRdB; unsigned int lnaGRdB; double currGain; } sdrplay_api_GainCbParamT;
typedef struct { int powerOverloadChangeType; } sdrplay_api_PowerOverloadCbParamT;
typedef union {
    sdrplay_api_GainCbParamT gainParams; sdrplay_api_PowerOverloadCbParamT powerOverloadParams;
} sdrplay_api_EventParamsT;
typedef void (*sdrplay_api_StreamCallback_t)(short *xi, short *xq,
    sdrplay_api_StreamCbParamsT *params, unsigned int numSamples, unsigned int reset, void *ctx);
typedef void (*sdrplay_api_EventCallback_t)(sdrplay_api_EventT eventId,
    sdrplay_api_TunerSelectT tuner, sdrplay_api_EventParamsT *params, void *ctx);
typedef struct {
    sdrplay_api_StreamCallback_t StreamACbFn; sdrplay_api_StreamCallback_t StreamBCbFn;
    sdrplay_api_EventCallback_t EventCbFn;
} sdrplay_api_CallbackFnsT;
#endif
#include "fake_common.h"

static int the_handle;
static sdrplay_api_DevParamsT devp;
static sdrplay_api_RxChannelParamsT cha, chb;
static sdrplay_api_DeviceParamsT params = {&devp, &cha, &chb};
static int chosen_tuner = 1;

sdrplay_api_ErrT sdrplay_api_Open(void) { say("open"); return knob("FAKE_RX_NOSERVICE") ? 1 : 0; }
sdrplay_api_ErrT sdrplay_api_Close(void) { say("close"); return 0; }
sdrplay_api_ErrT sdrplay_api_ApiVersion(float *v) { *v = 3.15f; return 0; }
sdrplay_api_ErrT sdrplay_api_LockDeviceApi(void) { return 0; }
sdrplay_api_ErrT sdrplay_api_UnlockDeviceApi(void) { return 0; }
sdrplay_api_ErrT sdrplay_api_GetDevices(sdrplay_api_DeviceT *devices, unsigned int *n,
                                        unsigned int max) {
    (void)max;
    if (knob("FAKE_RX_NONE")) { *n = 0; return 0; }
    memset(&devices[0], 0, sizeof(devices[0]));
    strcpy(devices[0].SerNo, "FAKE1A0001");
    devices[0].hwVer = (unsigned char)knob_num("FAKE_RX_MODEL", 255);
    devices[0].valid = 1;
    *n = 1;
    return 0;
}
sdrplay_api_ErrT sdrplay_api_SelectDevice(sdrplay_api_DeviceT *device) {
    say("select %s tuner=%d duo=%d", device->SerNo, (int)device->tuner, (int)device->rspDuoMode);
    chosen_tuner = device->tuner ? (int)device->tuner : 1;
    device->dev = &the_handle;
    cha.tunerParams.gain.gRdB = chb.tunerParams.gain.gRdB = 50;
    cha.ctrlParams.agc.enable = chb.ctrlParams.agc.enable = 2;     /* as the API starts */
    cha.rsp2TunerParams.antennaSel = 5;
    return 0;
}
sdrplay_api_ErrT sdrplay_api_ReleaseDevice(sdrplay_api_DeviceT *device) {
    say("release %s", device->SerNo);
    return 0;
}
const char *sdrplay_api_GetErrorString(sdrplay_api_ErrT err) {
    return err == 3 ? "sdrplay_api_OutOfRange" : "fake SDRplay error";
}
sdrplay_api_ErrT sdrplay_api_GetDeviceParams(HANDLE dev, sdrplay_api_DeviceParamsT **out) {
    if (dev != &the_handle) return 2;
    *out = &params;
    return 0;
}
sdrplay_api_ErrT sdrplay_api_Update(HANDLE dev, sdrplay_api_TunerSelectT tuner,
                                    sdrplay_api_ReasonForUpdateT reason,
                                    sdrplay_api_ReasonForUpdateExtension1T ext) {
    (void)dev;
    say("update tuner=%d reason=0x%08x ext=%u", (int)tuner, (unsigned)reason, (unsigned)ext);
    return 0;
}
sdrplay_api_ErrT sdrplay_api_Init(HANDLE dev, sdrplay_api_CallbackFnsT *fns, void *ctx) {
    sdrplay_api_RxChannelParamsT *ch = chosen_tuner == 2 ? &chb : &cha;
    if (dev != &the_handle) return 2;
    say("init fs=%.0f rf=%.0f bw=%d if=%d lna=%d grdb=%d agc=%d", devp.fsFreq.fsHz,
        ch->tunerParams.rfFreq.rfHz, (int)ch->tunerParams.bwType, (int)ch->tunerParams.ifType,
        (int)ch->tunerParams.gain.LNAstate, ch->tunerParams.gain.gRdB,
        (int)ch->ctrlParams.agc.enable);
    say("bias rsp1a=%d rsp2=%d ant2=%d duo=%d dx=%d antdx=%d",
        (int)ch->rsp1aTunerParams.biasTEnable, (int)ch->rsp2TunerParams.biasTEnable,
        (int)ch->rsp2TunerParams.antennaSel, (int)ch->rspDuoTunerParams.biasTEnable,
        (int)devp.rspDxParams.biasTEnable, (int)devp.rspDxParams.antennaSel);
    if (ch->tunerParams.gain.LNAstate > 9) return 3;
    ch->tunerParams.gain.gainVals.curr =
        (float)(100 - ch->tunerParams.gain.gRdB - 6 * ch->tunerParams.gain.LNAstate);
    if (knob("FAKE_RX_SILENT")) return 0;
    {
        sdrplay_api_EventParamsT ev;
        memset(&ev, 0, sizeof(ev));         /* overload detected: the reader must answer */
        fns->EventCbFn((sdrplay_api_EventT)1, (sdrplay_api_TunerSelectT)chosen_tuner, &ev, ctx);
    }
    long total = (long)knob_num("FAKE_RX_SAMPLES", 300000);
    static short xi[1344], xq[1344];
    sdrplay_api_StreamCbParamsT p;
    memset(&p, 0, sizeof(p));
    while (total > 0) {
        unsigned int n = total > 1344 ? 1344 : (unsigned int)total;
        for (unsigned int k = 0; k < n; k++) {
            int i = 16 * next_i();
            xi[k] = (short)i;
            xq[k] = (short)-i;
        }
        p.numSamples = n;
        if (chosen_tuner == 2) fns->StreamBCbFn(xi, xq, &p, n, 0, ctx);
        else fns->StreamACbFn(xi, xq, &p, n, 0, ctx);
        total -= n;
    }
    return 0;
}
sdrplay_api_ErrT sdrplay_api_Uninit(HANDLE dev) { (void)dev; say("uninit"); return 0; }
