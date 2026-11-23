#!/usr/bin/env python3
# ============ [MODULE: dcmc_stress.py | paired az/el motor stress matrix via dcmc menu; angle-domain moves using rsafe atos tables; compares simultaneous vs sequential large moves with per-poll logging | logging: stdout only] ============
# WARNING: 'v' and 'pwm' verbs are forbidden on this hardware (melted photoMOSFET); never add them to DCMC_ALLOWED.
import argparse
import bisect
import csv
import glob
import os
import re
import sys
import time
from datetime import datetime, timezone

# ---------------- [CONFIG] ----------------
DCMC_ALLOWED = {"p", "s", "i", "t"}
MOTOR_AZ = 0
MOTOR_EL = 1
EL_STEPS_TO_PKG = {20800: "lg", 10750: "sm"}
SETTLE = 0.1
AZ_MIN_DEG = 5.0
AZ_MAX_DEG = 450.0
EL_MIN_DEG = -5.0
EL_MAX_DEG = 94.0
STATUS_RE = re.compile(r"command status\s*=\s*([A-Za-z ]+)")

def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

def log(msg, raw=None):
    print("[%s] %s" % (stamp(), msg), flush=True)
    if raw is not None:
        print("[RAW] %r" % raw, flush=True)

# ---------------- [ARG PARSER] ----------------
def parse_args():
    p = argparse.ArgumentParser(
        description="Stress-test dcmc motor pairs in the ANGLE domain using rsafe atos tables: sweep az-angle x el-angle "
                    "combinations, moving both motors simultaneously or sequentially, and record which produce Mtr Stalled.\n"
                    "All targets are ABSOLUTE mount-frame angles (degrees), clamped to safe limits before any serial command.\n"
                    "Example: --az-angles 60,120,167.7 --el-angles 10,30,50.6 --mode both --mode seq")
    p.add_argument("-D", "--device", required=True, help="serial device path, e.g. /dev/ttyUSB2")
    p.add_argument("-b", "--baud", default=57600, type=int, help="serial baud rate (default 57600)")
    p.add_argument("--tables-dir", default="~/winegard/odu", help="directory holding the rsafe CSV tables (default ~/winegard/odu)")
    p.add_argument("--pre-az", type=float, help="stage absolute az angle before the matrix runs; staged el-first for clearance")
    p.add_argument("--pre-el", type=float, help="stage absolute el angle before the matrix runs; staged last after az")
    p.add_argument("--az-angles", required=True, help="comma-separated absolute az angles in degrees, e.g. 60,120,167.7")
    p.add_argument("--el-angles", required=True, help="comma-separated absolute el angles in degrees, e.g. 10,30,50.6")
    p.add_argument("--mode", action="append", required=True, choices=["both", "seq"],
                   help="both = az and el commanded together; seq = az settles before el starts; repeatable")
    p.add_argument("--poll-interval", default=0.3, type=float, help="seconds between status polls (default 0.3)")
    p.add_argument("--poll-max", default=60, type=int, help="max polls per move before declaring timeout (default 60)")
    p.add_argument("--cooldown", default=2.0, type=float, help="seconds between test pairs (default 2.0)")
    p.add_argument("--keep-last", action="store_true", help="skip baseline unwind between pairs (NOT recommended; positions accumulate)")
    p.add_argument("--start-gap", type=float, default=0.0, help="seconds to sleep between commanding multiple motors in one move (default 0.0 = simultaneous)")
    p.add_argument("--order", choices=["el", "az"], default="el",
                   help="motor write order in 'both' mode (default el)")
    p.add_argument("--pre-rest", type=float, default=0.0, help="seconds to rest after staging before the matrix runs")
    p.add_argument("-v", "--verbose", action="store_true", help="echo raw serial traffic")
    return p.parse_args()

# ---------------- [TABLES] ----------------
class MotorTable:
    def __init__(self, motor, rows):
        self.motor = motor
        self.angles = [r[0] for r in rows]
        self.steps = [r[1] for r in rows]
        mono = all(self.steps[i] < self.steps[i + 1] for i in range(len(self.steps) - 1))
        if not mono or len(rows) < 2:
            sys.exit("table audit failed for '%s': not strictly monotonic or too short" % motor)

    def angle_to_steps(self, angle):
        lo, hi = self.angles[0], self.angles[-1]
        if angle < lo or angle > hi:
            return None
        idx = bisect.bisect_right(self.angles, angle) - 1
        if idx >= len(self.angles) - 1:
            return self.steps[-1]
        a0, a1 = self.angles[idx], self.angles[idx + 1]
        s0, s1 = self.steps[idx], self.steps[idx + 1]
        if a1 == a0:
            return s0
        return int(round(s0 + (angle - a0) * (s1 - s0) / (a1 - a0)))

def load_tables(tables_dir, pkg):
    pattern = os.path.join(os.path.expanduser(tables_dir), "%s_*rsafe*.csv" % pkg)
    matches = sorted(glob.glob(pattern))
    if not matches:
        sys.exit("no table file matching %s" % pattern)
    if len(matches) > 1:
        log("WARNING: multiple table files found, using first of %s" % matches)
    rows = {"az": [], "el": []}
    with open(matches[0]) as fh:
        for rec in csv.DictReader(fh):
            if rec["motor"] in rows:
                rows[rec["motor"]].append((float(rec["angle"]), int(rec["steps"])))
    return {mo: MotorTable(mo, recs) for mo, recs in rows.items()}, matches[0]

# ---------------- [SERIAL] ----------------
def make_serial(device, baud):
    try:
        import serial
        ser = serial.Serial()
        ser.port = device
        ser.baudrate = baud
        ser.timeout = 0.1
        ser.dtr = False
        ser.rts = False
        ser.open()
        ser.exclusive = True
        return ser
    except Exception as exc:
        sys.exit("cannot open %s: %s" % (device, exc))

def send(ser, cmd, verbose):
    # every outbound dcmc write passes the verb whitelist gate before touching the port
    verb = cmd.split()[0] if cmd.split() else ""
    if verb not in DCMC_ALLOWED:
        sys.exit("FATAL: attempted to emit forbidden dcmc verb %r" % verb)
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    time.sleep(SETTLE)

def read_until(ser, marker, window, verbose):
    buf = ""
    end = time.time() + window
    while time.time() < end:
        chunk = ser.read(256).decode("ascii", "replace")
        if chunk:
            buf += chunk
            if verbose:
                log("raw", raw=chunk)
            if marker in buf:
                return buf
    return buf

# ---------------- [SESSION] ----------------
def enter_dcmc(ser, verbose):
    # orient to root prompt then descend into dcmc, retrying up to 3 times
    for attempt in range(1, 4):
        ser.reset_input_buffer()
        ser.write(b"\x1b")
        time.sleep(SETTLE)
        ser.write(b"q\r")
        time.sleep(SETTLE)
        ser.write(b"\r")
        buf = read_until(ser, "ODU>", 5.0, verbose)
        if "ODU>" in buf:
            break
        log("orientation attempt %d: no ODU> prompt, retrying" % attempt)
    else:
        sys.exit("no ODU> prompt in 3 attempts")
    ser.write(b"motorcon\r")
    read_until(ser, "MCON>", 3.0, verbose)
    ser.write(b"run\r")
    buf = read_until(ser, "MB OPEN COMMS SUCCESS", 5.0, verbose)
    if "MB OPEN COMMS SUCCESS" not in buf:
        sys.exit("dcmc bridge did not open")
    read_until(ser, "dcmc >", 2.0, verbose)
    log("session established at dcmc > prompt")

def leave_dcmc(ser):
    try:
        ser.reset_input_buffer()
        ser.write(b"\x1b")
        time.sleep(SETTLE)
        ser.write(b"q\r")
        time.sleep(SETTLE)
    except Exception:
        pass

def probe_package(ser, verbose):
    # one 'i' dump identifies the el steps/rev and thereby the satellite package
    send(ser, "i", verbose)
    buf = read_until(ser, "dcmc >", 3.0, verbose)
    mt = re.search(r"Elevation:(.*?)(?=Skew:|$)", buf, re.S)
    if not mt:
        sys.exit("probe failed: no Elevation section in 'i' output")
    spr = re.search(r"steps/rev\s*=\s*(\d+)", mt.group(1))
    if not spr:
        sys.exit("probe failed: no steps/rev in Elevation section")
    el_spr = int(spr.group(1))
    if el_spr not in EL_STEPS_TO_PKG:
        sys.exit("unrecognized el steps/rev %d; known: %s" % (el_spr, EL_STEPS_TO_PKG))
    pkg = EL_STEPS_TO_PKG[el_spr]
    log("probe: el steps/rev=%d -> package=%s" % (el_spr, pkg))
    return pkg

# ---------------- [OPS] ----------------
def parse_i_state(dump):
    # per-motor state: position, command status, pwm from one 'i' dump
    state = {}
    section = None
    for line in dump.splitlines():
        if "Azimuth:" in line:
            section = MOTOR_AZ
        elif "Elevation:" in line:
            section = MOTOR_EL
        elif "Skew:" in line:
            section = 2
        elif section is not None:
            pm = re.search(r"^\s*position\s*=\s*(-?\d+)", line)
            sm = STATUS_RE.search(line)
            wm = re.search(r"pwm\s*=\s*(-?\d+)", line)
            if pm:
                state.setdefault(section, {})["position"] = int(pm.group(1))
            if sm:
                state.setdefault(section, {})["status"] = sm.group(1).strip()
            if wm:
                state.setdefault(section, {})["pwm"] = int(wm.group(1))
    return state

def query_state(ser, verbose):
    send(ser, "i", verbose)
    buf = read_until(ser, "dcmc >", 3.0, verbose)
    return parse_i_state(buf)

def goto_steps(ser, motor, steps, verbose):
    send(ser, "p %d %d" % (motor, steps), verbose)

def stop_motor(ser, motor, verbose):
    send(ser, "s %d" % motor, verbose)

# ---------------- [SAFE MOVE] ----------------
def angle_target(ser, tables, motor_name, angle, verbose):
    # clamp angle to safe limits, convert through the rsafe table, refuse outside table domain
    lo, hi = (AZ_MIN_DEG, AZ_MAX_DEG) if motor_name == "az" else (EL_MIN_DEG, EL_MAX_DEG)
    clamped = min(max(angle, lo), hi)
    if clamped != angle:
        log("CLAMP: %s %.2f -> %.2f" % (motor_name, angle, clamped))
    steps = tables[motor_name].angle_to_steps(clamped)
    if steps is None:
        sys.exit("angle %.2f for %s outside table domain after clamp" % (clamped, motor_name))
    return steps

# ---------------- [RUNNER] ----------------
def run_move(ser, tables, moves, verbose, poll_interval, poll_max, label, start_gap=0.0):
    # moves: list of (motor_name, motor_index, angle); command all listed motors then poll to completion
    cmds = []
    for name, idx, angle in moves:
        steps = angle_target(ser, tables, name, angle, verbose)
        cmds.append((name, idx, angle, steps))
    for n, (name, idx, angle, steps) in enumerate(cmds):
        goto_steps(ser, idx, steps, verbose)
        log("%s CMD %s %.2f deg -> steps %d" % (label, name, angle, steps))
        if n + 1 < len(cmds):
            log("%s STAGGER: %.2f s before next motor" % (label, start_gap))
            time.sleep(start_gap)
    results = {name: None for name, _, _ in moves}
    maxpwm = {name: 0 for name, _, _ in moves}
    reached = {name: None for name, _, _ in moves}
    idxmap = {name: idx for name, idx, _ in moves}
    for poll in range(poll_max):
        time.sleep(poll_interval)
        st = query_state(ser, verbose)
        if not st:
            continue
        for name, _, _ in moves:
            s = st.get(idxmap[name], {})
            maxpwm[name] = max(maxpwm[name], abs(s.get("pwm", 0)))
            reached[name] = s.get("position")
            status = s.get("status", "")
            if "Stalled" in status:
                results[name] = "STALLED@%d(pwm=%d,maxpwm=%d)" % (s.get("position", -1), s.get("pwm", 0), maxpwm[name])
            elif "Complete" in status:
                results[name] = "COMPLETE(maxpwm=%d)" % maxpwm[name]
        if all(v is not None for v in results.values()):
            break
    for name in results:
        if results[name] is None:
            results[name] = "TIMEOUT(last=%d,maxpwm=%d)" % (reached[name] if reached[name] is not None else -1, maxpwm[name])
    return results, maxpwm, reached

# ---------------- [MAIN] ----------------
def main():
    args = parse_args()
    az_angles = [float(x) for x in args.az_angles.split(",")]
    el_angles = [float(x) for x in args.el_angles.split(",")]
    for a in az_angles + el_angles:
        if a < EL_MIN_DEG or a > AZ_MAX_DEG:
            sys.exit("angle %.2f grossly out of range; check argument order (all angles in degrees)" % a)
    log("dcmc_stress: device=%s az_angles=%s el_angles=%s modes=%s start_gap=%.2f" % (args.device, az_angles, el_angles, args.mode, args.start_gap))
    ser = make_serial(args.device, args.baud)
    all_rows = []
    try:
        enter_dcmc(ser, args.verbose)
        pkg = probe_package(ser, args.verbose)
        tables, tblpath = load_tables(args.tables_dir, pkg)
        log("tables loaded: %s" % tblpath)
        st0 = query_state(ser, args.verbose)
        base_az_angle = tables["az"].steps and tables["az"].angles[0]
        log("baseline counters: az=%s el=%s" % (st0[MOTOR_AZ]["position"], st0[MOTOR_EL]["position"]))
        az0 = tables["az"].angles[0]
        if args.pre_az is not None or args.pre_el is not None:
            pre_az = AZ_MAX_DEG if args.pre_az is None else min(max(args.pre_az, AZ_MIN_DEG), AZ_MAX_DEG)
            pre_el = EL_MAX_DEG if args.pre_el is None else min(max(args.pre_el, EL_MIN_DEG), EL_MAX_DEG)
            clear_el = max([pre_el] + el_angles)
            log("staging posture: el->%.2f (clear), az->%.2f, el->%.2f" % (clear_el, pre_az, pre_el))
            run_move(ser, tables, [("el", MOTOR_EL, clear_el)],
                     args.verbose, args.poll_interval, args.poll_max, "STAGE-EL")
            run_move(ser, tables, [("az", MOTOR_AZ, pre_az)],
                     args.verbose, args.poll_interval, args.poll_max, "STAGE-AZ")
            if abs(pre_el - clear_el) > 0.01:
                run_move(ser, tables, [("el", MOTOR_EL, pre_el)],
                         args.verbose, args.poll_interval, args.poll_max, "STAGE-EL2")
            log("staging complete at az %.2f el %.2f" % (pre_az, pre_el))
        if args.pre_rest > 0:
            log("REST: %.1f s after staging, before matrix" % args.pre_rest)
            time.sleep(args.pre_rest)
        pair_no = 0
        for mode in args.mode:
            for aza in az_angles:
                for ela in el_angles:
                    pair_no += 1
                    label = "PAIR %02d" % pair_no
                    log("=" * 64)
                    log("%s mode=%s az=%.2f el=%.2f" % (label, mode, aza, ela))
                    if mode == "both":
                        moves = ([("az", MOTOR_AZ, aza), ("el", MOTOR_EL, ela)]
                                 if args.order == "az" else
                                 [("el", MOTOR_EL, ela), ("az", MOTOR_AZ, aza)])
                        results, maxpwm, reached = run_move(ser, tables, moves,
                                                           args.verbose, args.poll_interval, args.poll_max, label,
                                                           start_gap=args.start_gap)
                    else:
                        ra, _, _ = run_move(ser, tables, [("az", MOTOR_AZ, aza)],
                                            args.verbose, args.poll_interval, args.poll_max, label)
                        re_, _, _ = run_move(ser, tables, [("el", MOTOR_EL, ela)],
                                             args.verbose, args.poll_interval, args.poll_max, label)
                        results = {"az": ra["az"], "el": re_["el"]}
                    log("%s RESULT az=%s el=%s" % (label, results["az"], results["el"]))
                    all_rows.append((mode, aza, ela, results["az"], results["el"]))
                    if "STALLED" in results["el"]:
                        stop_motor(ser, MOTOR_EL, args.verbose)
                        log("el stalled; cooldown extended 3x")
                        time.sleep(args.cooldown * 3)
                    elif "STALLED" in results["az"]:
                        stop_motor(ser, MOTOR_AZ, args.verbose)
                        log("az stalled; cooldown extended 3x")
                        time.sleep(args.cooldown * 3)
                    else:
                        time.sleep(args.cooldown)
                    if not args.keep_last:
                        # unwind: raise el first, then az, so the dish never drags across the surface
                        run_move(ser, tables, [("el", MOTOR_EL, max(el_angles))],
                                 args.verbose, args.poll_interval, args.poll_max, label + "-UNWIND-EL")
                        run_move(ser, tables, [("az", MOTOR_AZ, az0)],
                                 args.verbose, args.poll_interval, args.poll_max, label + "-UNWIND-AZ")
                        log("%s unwound to safe posture (el high, az at %.2f)" % (label, az0))
        log("=" * 64)
        log("SUMMARY MATRIX (mode, az_angle, el_angle, az_result, el_result)")
        for row in all_rows:
            log("  mode=%s az=%.2f el=%.2f | az=%s | el=%s" % row)
    finally:
        stop_motor(ser, MOTOR_AZ, args.verbose)
        stop_motor(ser, MOTOR_EL, args.verbose)
        leave_dcmc(ser)
        ser.close()
        log("clean exit")

if __name__ == "__main__":
    main()
