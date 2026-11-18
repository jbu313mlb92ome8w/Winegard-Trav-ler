#!/usr/bin/env python3
# ============ [MODULE: atos_table.py | builds angle-to-steps tables via ODU 'atos' command; probes dcmc for motor identity | logging: stdout only] ============
import argparse
import csv
import os
import re
import sys
import time
import serial

# ---------------- [CONFIG] ----------------
MOTOR_RANGES = {"az": (0.0, 450.0), "el": (-58.5, 95.0), "sk": (-95.0, 95.0)}
MOTOR_RANGES_SAFE = {"az": (5.0, 450.0), "el": (-5.0, 94.0), "sk": (-90.0, 90.0)}
MOTOR_INDEX = {"az": 0, "el": 1, "sk": 2}
EL_STEPS_TO_PKG = {20800: "lg", 10750: "sm"}
AZ_STEPS = 6600
SK_STEPS = 2400
SETTLE = 0.25
STEPS_RE = re.compile(r"Steps\s*=\s*(-?\d+)\s*\r")
SPR_RE = re.compile(r"steps/rev\s*=\s*(\d+)")

# ---------------- [ARG PARSER] ----------------
def parse_args():
    def _desc(tbl):
        return ", ".join("%s=%g..%g" % (k, v[0], v[1]) for k, v in tbl.items())
    p = argparse.ArgumentParser(
        description="Query ODU MOT> menu command 'atos' and build angle-to-steps tables; LG or SM package auto-detected from dcmc motor probe.\n"
                    "Default sweeps use safe ranges %s\n"
                    "\nNote: Requested ranges outside a motor's safe envelope are permitted but warned; "
                    "out-of-travel values are clamped to the motor's limits; --angles values outside travel are skipped" % (_desc(MOTOR_RANGES_SAFE)),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-d", "--device", required=True, help="serial device path, e.g. /dev/ttyUSB0")
    p.add_argument("-b", "--baud", required=True, type=int, help="serial baud rate, e.g. 57600")
    p.add_argument("-m", "--motors", required=True, help="comma-separated motor list: az,el,sk")
    p.add_argument("-s", "--step", type=float, help="angle increment in degrees; required for all sweep modes")
    p.add_argument("--angles", help="comma-separated explicit angles, e.g. 405,405.5,-12.25; exempts -s")
    p.add_argument("--range", help="bounded sweep as lo:hi, e.g. -10:20; -s still required; clamped to motor limits")
    p.add_argument("--full", action="store_true", help="default whole-envelope sweeps use factory travel %s instead of safe ranges" % _desc(MOTOR_RANGES))
    p.add_argument("--retries", type=int, default=2, help="resend attempts when no parseable reply arrives")
    p.add_argument("--timeout", type=float, default=1.5, help="seconds to wait for each reply")
    p.add_argument("-f", "--format", required=True, choices=["csv", "md"], help="table file format; dictates the file extension")
    p.add_argument("-o", "--out", help="explicit output filename; -f extension appended if missing")
    p.add_argument("-i", "--inferred", action="store_true", help="auto-name one file per motor from the run parameters")
    p.add_argument("-a", "--inferred-all", action="store_true", help="auto-name one aggregate file for all motors (cluster as -ia)")
    p.add_argument("--outdir", help="directory for -i/-a files; defaults to the current directory")
    p.add_argument("-v", "--verbose", action="store_true", help="echo every raw device response")
    return p.parse_args()

# ---------------- [VALIDATION] ----------------
def validate(args):
    if sum([bool(args.out), bool(args.inferred), bool(args.inferred_all)]) != 1:
        sys.exit("exactly one output mode required: -o, -i, or -a")
    if args.angles and args.range:
        sys.exit("--angles and --range are mutually exclusive")
    if args.step is None and not args.angles:
        sys.exit("-s/--step is required for all sweep modes")
    if args.step is not None and args.step <= 0:
        sys.exit("--step must be positive")
    if args.step is not None and args.angles:
        print("note: -s ignored because --angles enumerates its own values")
    if args.angles:
        try:
            args.angles = [float(x) for x in args.angles.split(",")]
        except ValueError:
            sys.exit("--angles must be numeric comma-separated values")
    if args.range:
        parts = args.range.split(":")
        if len(parts) != 2:
            sys.exit("--range must be exactly lo:hi")
        try:
            args.range = [float(x) for x in parts]
        except ValueError:
            sys.exit("--range bounds must be numeric")
        if args.range[0] > args.range[1]:
            sys.exit("--range lower bound exceeds upper bound")
    args.motors = [x.strip() for x in args.motors.split(",") if x.strip()]
    for mo in args.motors:
        if mo not in MOTOR_RANGES:
            sys.exit("unknown motor '%s' (valid: az, el, sk)" % mo)
    if args.out:
        ext = "." + args.format
        lower = args.out.lower()
        if lower.endswith(".csv") or lower.endswith(".md"):
            if not lower.endswith(ext):
                sys.exit("-o filename extension does not match -f format '%s'" % args.format)
        else:
            args.out += ext

# ---------------- [NAMING] ----------------
def fmt_angle(a):
    return "%g" % a

def angle_series(lo, hi, step):
    out = []
    i = 0
    while True:
        a = round(lo + i * step, 4)
        if a > hi + 1e-9:
            break
        out.append(a)
        i += 1
    return out

def build_plan(args, mo):
    flo, fhi = MOTOR_RANGES[mo]
    slo, shi = MOTOR_RANGES_SAFE[mo]
    if args.angles:
        angles = [a for a in args.angles if flo <= a <= fhi]
        skipped = [a for a in args.angles if not (flo <= a <= fhi)]
        risky = [a for a in angles if not (slo <= a <= shi)]
        return {"motor": mo, "angles": angles, "skipped": skipped, "risky": risky, "c_lo": None, "c_hi": None, "clamped": False}
    if args.range:
        c_lo = max(flo, args.range[0])
        c_hi = min(fhi, args.range[1])
        clamped = (c_lo, c_hi) != (args.range[0], args.range[1])
        risky = args.range[0] < slo or args.range[1] > shi
        if c_hi < flo or c_lo > fhi:
            return {"motor": mo, "angles": [], "skipped": [], "risky": [], "c_lo": c_lo, "c_hi": c_hi, "clamped": clamped}
        return {"motor": mo, "angles": angle_series(c_lo, c_hi, args.step), "skipped": [], "risky": [], "c_lo": c_lo, "c_hi": c_hi, "clamped": clamped}
    lo, hi = (MOTOR_RANGES[mo] if args.full else MOTOR_RANGES_SAFE[mo])
    tok = "rfull" if args.full else "rsafe"
    return {"motor": mo, "angles": angle_series(lo, hi, args.step), "skipped": [], "risky": [], "c_lo": lo, "c_hi": hi, "clamped": False, "tok": tok}

def inferred_paths(args, pkg, plans):
    ext = "." + args.format
    outdir = args.outdir or "."
    stok = ("s%s" % ("%g" % args.step)) if args.step is not None else ""
    paths = {}
    if args.inferred_all:
        mjoin = "..".join(args.motors)
        if args.angles:
            base = "%s_%s_spot_%s" % (pkg, mjoin, ",".join(fmt_angle(a) for a in args.angles))
        else:
            rtok = ("r%s..%s" % (fmt_angle(args.range[0]), fmt_angle(args.range[1]))) if args.range else ("rfull" if args.full else "rsafe")
            base = "%s_%s_%s_%s" % (pkg, mjoin, stok, rtok)
        paths["_all"] = os.path.join(outdir, base + ext)
    else:
        for plan in plans:
            if not plan["angles"]:
                continue
            if args.angles:
                base = "%s_%s_spot_%s" % (pkg, plan["motor"], ",".join(fmt_angle(a) for a in plan["angles"]))
            else:
                base = "%s_%s_%s_r%s..%s" % (pkg, plan["motor"], stok, fmt_angle(plan["c_lo"]), fmt_angle(plan["c_hi"]))
            paths[plan["motor"]] = os.path.join(outdir, base + ext)
    return paths

# ---------------- [SERIAL QUERY] ----------------
def send(ser, data):
    # transmit a command with a carriage-return terminator; ESC is sent bare
    if isinstance(data, str) and not data.startswith("\x1b"):
        data += "\r"
    ser.reset_input_buffer()
    ser.write(data.encode("ascii") if isinstance(data, str) else data)
    time.sleep(SETTLE)

def read_available(ser, wait, verbose=False):
    time.sleep(wait)
    return ser.read(4096).decode("ascii", "replace")

def read_until(ser, marker, window, verbose=False):
    # read serial output until a marker string appears or the window expires
    buf = ""
    end = time.time() + window
    while time.time() < end:
        chunk = ser.read(256).decode("ascii", "replace")
        if chunk:
            buf += chunk
            if verbose:
                sys.stdout.write("[RAW] %r\n" % chunk)
            if marker in buf:
                return buf
    return buf

def verify_link(ser, verbose):
    # confirm the unit is alive by watching for the ODU prompt after orientation
    send(ser, "\x1b")
    send(ser, "q")
    send(ser, "\r")
    reply = read_until(ser, "ODU>", 5.0, verbose)
    if "ODU>" not in reply:
        sys.exit("no ODU> prompt within 5s of orientation; other serial session open, unit may be off, cable disconnected, or wrong port")

def probe_motors(ser, verbose):
    send(ser, "motorcon")
    read_until(ser, "MCON>", 2.0, verbose)
    send(ser, "run")
    read_until(ser, "MB OPEN COMMS SUCCESS", 3.0, verbose)
    send(ser, "i")
    dump = read_until(ser, "Skew:", 3.0, verbose)
    if verbose:
        sys.stdout.write("[PROBE RAW] %r\n" % dump)
    send(ser, "\x1b")
    send(ser, "q")
    sections = {"az": (re.search(r"Azimuth:(.*?)(?=Elevation:|$)", dump, re.S)),
                "el": (re.search(r"Elevation:(.*?)(?=Skew:|$)", dump, re.S)),
                "sk": (re.search(r"Skew:(.*?)$", dump, re.S))}
    spr = {}
    for mo, mt in sections.items():
        if mt is None:
            sys.stderr.write("[PROBE DUMP] %r\n" % dump)
            sys.exit("probe failed: no '%s' section in dcmc 'i' output (raw dump above)" % mo)
        found = SPR_RE.search(mt.group(1))
        if found is None:
            sys.exit("probe failed: no steps/rev found for motor '%s'" % mo)
        spr[mo] = int(found.group(1))
    if spr["az"] != AZ_STEPS or spr["sk"] != SK_STEPS:
        sys.exit("probe sanity failed: az/sk steps/rev expected 6600/2400, measured %d/%d (possible parse desync)" % (spr["az"], spr["sk"]))
    if spr["el"] not in EL_STEPS_TO_PKG:
        sys.exit("unrecognized el steps/rev %d; known values: %s" % (spr["el"], ", ".join("%d=%s" % kv for kv in EL_STEPS_TO_PKG.items())))
    pkg = EL_STEPS_TO_PKG[spr["el"]]
    print("probe: az=%d el=%d sk=%d -> package=%s (units: lg=LG-2112, sm=SM-0022)" % (spr["az"], spr["el"], spr["sk"], pkg))
    return pkg

def query_atos(ser, motor, angle, retries, timeout, verbose):
    cmd = "atos %d %s" % (MOTOR_INDEX[motor], fmt_angle(angle))
    for _ in range(retries + 1):
        ser.reset_input_buffer()
        ser.write((cmd + "\r").encode("ascii"))
        buf = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = ser.read(256).decode("ascii", "replace")
            if chunk:
                buf += chunk
                if verbose:
                    sys.stdout.write("[RAW] %r\n" % buf)
                mt = STEPS_RE.search(buf)
                if mt:
                    return int(mt.group(1))
    return None

# ---------------- [WRITERS] ----------------
def write_rows(path, args, pkg, rows, motors):
    with open(path, "w") as fh:
        if args.format == "md":
            step_disp = ("%g" % args.step) if args.step is not None else "spot"
            fh.write("# ATOS Tables: %s package (step %s)\n\n" % (pkg.upper(), step_disp))
            for mo in motors:
                mrows = [r for r in rows if r["motor"] == mo]
                if not mrows:
                    continue
                fh.write("## %s\n\n" % mo.upper())
                fh.write("| Angle | Steps |\n")
                fh.write("|---:|---:|\n")
                for r in mrows:
                    fh.write("| %s | %d |\n" % (r["angle"], r["steps"]))
                fh.write("\n")
        else:
            w = csv.DictWriter(fh, fieldnames=["package", "motor", "angle", "steps"])
            w.writeheader()
            w.writerows(rows)
    print("wrote %s (%d rows, format=%s)" % (path, len(rows), args.format))

# ---------------- [MAIN SWEEP] ----------------
def main():
    args = parse_args()
    validate(args)
    step_disp = ("%g" % args.step) if args.step is not None else "n/a"
    t0 = time.time()
    rows = []
    failures = []
    ser = serial.Serial(args.device, args.baud, timeout=0.1)
    try:
        ser = serial.Serial(args.device, args.baud, timeout=0.1)
    except serial.SerialException as exc:
        sys.exit("cannot open %s: %s" % (args.device, exc))
    verify_link(ser, args.verbose)
    try:
        pkg = probe_motors(ser, args.verbose)
        plans = [build_plan(args, mo) for mo in args.motors]
        if args.out:
            paths = {"_all": args.out}
        else:
            paths = inferred_paths(args, pkg, plans)
        send(ser, "mot")
        for plan in plans:
            mo = plan["motor"]
            lo, hi = MOTOR_RANGES[mo]
            if plan["skipped"]:
                print("skipped out-of-range angles for %s: %s" % (mo, ", ".join(fmt_angle(a) for a in plan["skipped"])))
            if plan.get("tok"):
                print("envelope: %s" % plan["tok"])
            if plan["risky"]:
                slo, shi = MOTOR_RANGES_SAFE[mo]
                print("WARNING: %s requests outside safe envelope %g..%g; proceeding under user responsibility" % (mo, slo, shi))
            if plan["clamped"]:
                print("WARNING: %s range %s..%s clamped to %s..%s at motor limits" % (
                    mo, fmt_angle(args.range[0]), fmt_angle(args.range[1]), fmt_angle(plan["c_lo"]), fmt_angle(plan["c_hi"])))
            if not plan["angles"]:
                print("no valid angles for %s, continuing" % mo)
                continue
            print("=" * 72)
            print("PACKAGE=%s MOTOR=%s MODE=%s SAFE=%g..%g STEP=%s ROWS=%d" % (
                pkg, mo, "full" if args.full else "safe",
                MOTOR_RANGES_SAFE[mo][0], MOTOR_RANGES_SAFE[mo][1], step_disp, len(plan["angles"])))
            print("=" * 72)
            print("%12s %12s" % ("ANGLE", "STEPS"))
            smin = None
            smax = None
            for idx, a in enumerate(plan["angles"]):
                steps = query_atos(ser, mo, a, args.retries, args.timeout, args.verbose)
                if steps is None:
                    failures.append((mo, a))
                    print("%12s %12s" % (fmt_angle(a), "FAIL"))
                    continue
                rows.append({"package": pkg, "motor": mo, "angle": fmt_angle(a), "steps": steps})
                smin = steps if smin is None else min(smin, steps)
                smax = steps if smax is None else max(smax, steps)
                pct = (idx + 1) * 100 // len(plan["angles"])
                sys.stdout.write("\r%12s %12d  [%d%%]" % (fmt_angle(a), steps, pct))
                sys.stdout.flush()
            sys.stdout.write("\n")
            if smin is not None:
                print("observed steps range for %s: %d .. %d" % (mo, smin, smax))
        send(ser, "exit")
    finally:
        send(ser, "q")
        ser.close()
    if args.inferred:
        for mo in args.motors:
            if mo in paths:
                write_rows(paths[mo], args, pkg, [r for r in rows if r["motor"] == mo], [mo])
    else:
        write_rows(paths["_all"], args, pkg, rows, args.motors)
    if failures:
        print("FAILED QUERIES: %d" % len(failures))
        for mo, a in failures:
            print("  %s %s" % (mo, fmt_angle(a)))
    print("elapsed: %.1f s" % ( (time.time() - t0) * 1.0 ))

if __name__ == "__main__":
    main()
