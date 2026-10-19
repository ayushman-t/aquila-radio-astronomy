# Aquila Radio Astronomy. Ayushman Tripathi, www.radioastronomy.in
# GPL-3.0-or-later: free to use and modify, derived works must stay open.
"""Command line.

  aquila setup     make a config (asks four questions)
  aquila run       SETI survey until stopped
  aquila hline     hydrogen line scans until stopped
  aquila cal       one receiver-only scan (terminator on the LNA) for the H-line plots
  aquila curate    tier the hits into candidates
  aquila status    print what the node is doing
  aquila serve     just the read-only API (for a node the app reaches remotely)
  aquila gui       the desktop app
"""
import argparse
import json
import os
import sys

from . import api, config, curate, sdr
from .hline import HLine
from .survey import Survey


def cmd_setup(args):
    cfg = config.load(args.config)
    found = sdr.detect()
    print("SDR tools found: %s" % (", ".join(found) or "none"))
    if args.yes:
        if found:
            cfg["sdr"] = found[0]
    else:
        def ask(prompt, cur):
            v = input(f"{prompt} [{cur}]: ").strip()
            return v or cur
        config.rename(cfg, ask("Node name", cfg["name"]))
        choices = "/".join(found + ["synthetic"])
        cfg["sdr"] = ask(f"SDR ({choices})", found[0] if found else cfg["sdr"])
        try:
            cfg["center_mhz"] = float(ask("Centre frequency MHz", cfg["center_mhz"]))
            cfg["dish"]["az"] = float(ask("Dish azimuth deg", cfg["dish"]["az"]))
            cfg["dish"]["el"] = float(ask("Dish elevation deg", cfg["dish"]["el"]))
        except ValueError:
            raise RuntimeError("frequency and angles must be numbers") from None
    path = config.save(cfg, args.config)
    print(f"saved {path}")
    print("data will go to {}".format(cfg["state_dir"]))


def cmd_run(args):
    cfg = config.load(args.config)
    if args.sdr:
        cfg["sdr"] = args.sdr
    s = Survey(cfg)
    s.install_signals()
    s.start_api()
    s.run(blocks=args.blocks or 0)


def cmd_hline(args):
    cfg = config.load(args.config)
    if args.sdr:
        cfg["sdr"] = args.sdr
    h = HLine(cfg)
    import signal
    signal.signal(signal.SIGTERM, h.stop)
    signal.signal(signal.SIGINT, h.stop)
    try:
        api.serve(cfg["state_dir"], cfg["advanced"]["api_bind"], cfg["advanced"]["api_port"],
                  daily_mb=cfg["advanced"].get("api_daily_mb", 0),
                  token=cfg["advanced"].get("api_token", ""))
    except OSError as e:
        print(f"api not started: {e}")
    h.run(blocks=args.blocks or 0)


def cmd_cal(args):
    cfg = config.load(args.config)
    if args.sdr:
        cfg["sdr"] = args.sdr
    if not HLine(cfg).calibrate():
        sys.exit(1)


def cmd_serve(args):
    cfg = config.load(args.config)
    adv = cfg["advanced"]
    print(f"serving {cfg['state_dir']} on {adv['api_bind']}:{adv['api_port']}")
    try:
        api.serve(cfg["state_dir"], adv["api_bind"], adv["api_port"], background=False,
                  daily_mb=adv.get("api_daily_mb", 0), token=adv.get("api_token", ""))
    except OSError as e:
        raise RuntimeError(f"api not started on {adv['api_bind']}:{adv['api_port']}: {e}") from None


def cmd_gui(args):
    try:
        from .gui import main as gui_main
    except ImportError as e:
        raise RuntimeError("the desktop app needs PySide6 and pyqtgraph: "
                           f"pip install 'aquila-radio-astronomy[gui]'  ({e})") from None
    gui_main(args.config)


def gui_entry():
    main(sys.argv[1:] + ["gui"])


def cmd_curate(args):
    cfg = config.load(args.config)
    from .skymap import beam_deg
    beam = beam_deg(cfg["dish"].get("diameter_m"), cfg["center_mhz"] * 1e6)
    adv = cfg["advanced"]
    print(json.dumps(curate.run(cfg["state_dir"], beam, cfg["center_mhz"] * 1e6,
                                adv.get("exclude_if_khz") or [], float(adv["snr_thresh"]),
                                adv.get("comb_period_hz") or [10000, 100000]), indent=2))


def cmd_status(args):
    cfg = config.load(args.config)
    p = os.path.join(cfg["state_dir"], "status.json")
    if not os.path.exists(p):
        print(f"no status yet ({p})")
        return
    d = json.load(open(p))
    for k in ("state", "name", "sdr", "center_hz", "chan_bw_hz", "blocks", "hits_total",
              "footprint_mb", "uptime_s"):
        if k in d:
            print(f"{k:<14} {d[k]}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aquila", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config",
                    help="config file (default ~/aquila-radio-astronomy/config.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("setup")
    a.add_argument("-y", "--yes", action="store_true", help="take defaults, no questions")
    a.set_defaults(fn=cmd_setup)
    a = sub.add_parser("run")
    a.add_argument("--sdr", help="override the SDR for this run")
    a.add_argument("--blocks", type=int, help="stop after N blocks")
    a.set_defaults(fn=cmd_run)
    a = sub.add_parser("hline")
    a.add_argument("--sdr")
    a.add_argument("--blocks", type=int, help="stop after N scans")
    a.set_defaults(fn=cmd_hline)
    a = sub.add_parser("cal")
    a.add_argument("--sdr")
    a.set_defaults(fn=cmd_cal)
    sub.add_parser("serve").set_defaults(fn=cmd_serve)
    sub.add_parser("gui").set_defaults(fn=cmd_gui)
    sub.add_parser("curate").set_defaults(fn=cmd_curate)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
