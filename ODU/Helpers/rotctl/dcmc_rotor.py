#!/usr/bin/env python3
# ============ [MODULE: dcmc_rotor.py | rotctld-protocol daemon driving a Winegard Trav'ler via the firmware dcmc menu | logging: stdout only] ============
import argparse
import bisect
import csv
import glob
import os
import re
import socket
import sys
import time
from datetime import datetime, timezone

# ---------------- [CONFIG] ----------------
# WARNING: 'v' and 'pwm' commands melted a photoMOSFET on a unit; never add them to DCMC_ALLOWED.
DCMC_ALLOWED = {"p", "s", "i", "t"}
# true north heading.
HEADING_DEG = 5.0
AZ_MIN_DEG = 5.0
AZ_MAX_DEG = 445.0
EL_MIN_DEG = -5.0
EL_MAX_DEG = 94.0
DEADBAND_DEG = 0.5
QC_TOLERANCE_DEG = 3.0
# 14.9 is tested max that can be used; probably firmware limited to trigger stall on 15+.
EL_CONCURRENT_MAX_DEG = 14.5
# 1.1 is tested min that can be used. increase if stalling occurs.
START_GAP_SEC = 1.1
SETTLE = 0.1
PARK_AZ_DEG = 0
PARK_EL_DEG = 0
IDLE_PARK_SEC = 300
MOTOR_AZ = 0
MOTOR_EL = 1
MOTOR_SK = 2
SK_ZERO_ANGLE = 45
ZERO_SK_ON_START = True
EL_STEPS_TO_PKG = {20800: "lg", 10750: "sm"}
INFO_STRING = "dcmc_rotor Winegard Trav'ler via dcmc (rotctld emulation)"

def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]

def log(msg, raw=None):
    line = "[%s] %s" % (stamp(), msg)
    print(line, flush=True)
    if raw is not None:
        print("[RAW] %r" % raw, flush=True)

# ---------------- [ARG PARSER] ----------------
def parse_args():
    p = argparse.ArgumentParser(
        description="rotctld-protocol daemon that drives a Winegard Trav'ler ODU through the firmware dcmc menu.\n"
                    "Speaks the hamlib netrotctl protocol on TCP so SatDump or GPredict can steer the dish.\n"
                    "Supported verbs: P (set pos), p (get pos), S (stop), _ (info), q (close). Unknown verbs get RPRT -1.\n"
                    "Hardware layers of protection: CONFIG limits + rsafe CSV table domain + dcmc firmware limits.\n"
                    "Park: --park slews to PARK_AT_START at launch; idle parking engages after IDLE_PARK_SEC of client silence.")
    p.add_argument("-D", "--device", required=True, help="serial device path, e.g. /dev/ttyUSB2")
    p.add_argument("-b", "--baud", default=57600, type=int, help="serial baud rate (default 57600)")
    p.add_argument("-a", "--address", default="127.0.0.1", help="IP address to listen on (default 127.0.0.1)")
    p.add_argument("-p", "--port", default=4533, type=int, help="TCP port to listen on (default 4533)")
    p.add_argument("--tables-dir", default="~/winegard/odu", help="directory holding the rsafe CSV tables (default ~/winegard/odu)")
    p.add_argument("--park", action="store_true", help="slew to the park position at startup before listening")
    p.add_argument("--no-idle-park", action="store_true", help="disable automatic parking after client silence")
    p.add_argument("-v", "--verbose", action="store_true", help="echo raw serial traffic and dumps")
    return p.parse_args()

# ---------------- [TABLES] ----------------
class MotorTable:
    def __init__(self, motor, rows):
        self.motor = motor
        self.angles = [r[0] for r in rows]
        self.steps = [r[1] for r in rows]
        angles_mono = all(self.angles[i] < self.angles[i + 1] for i in range(len(self.angles) - 1))
        steps_mono = all(self.steps[i] <= self.steps[i + 1] for i in range(len(self.steps) - 1))
        if not angles_mono or not steps_mono or len(rows) < 2:
            sys.exit("table audit failed for '%s': angles not strictly monotonic, steps not non-decreasing, or too short" % motor)

    def angle_to_steps(self, angle):
        # forward conversion with linear interpolation between table rows
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

    def steps_to_angle(self, step):
        # inverse conversion for position reporting
        if step < self.steps[0] or step > self.steps[-1]:
            return None
        idx = bisect.bisect_right(self.steps, step) - 1
        if idx >= len(self.steps) - 1:
            return self.angles[-1]
        s0, s1 = self.steps[idx], self.steps[idx + 1]
        a0, a1 = self.angles[idx], self.angles[idx + 1]
        if s1 == s0:
            return a0
        return a0 + (step - s0) * (a1 - a0) / (s1 - s0)

def load_tables(tables_dir, pkg, verbose):
    pattern = os.path.join(os.path.expanduser(tables_dir), "%s_*rsafe*.csv" % pkg)
    matches = sorted(glob.glob(pattern))
    if not matches:
        sys.exit("no table file matching %s" % pattern)
    if len(matches) > 1:
        log("WARNING: multiple table files found, using first of %s" % matches)
    rows = {"az": [], "el": [], "sk": []}
    with open(matches[0]) as fh:
        for rec in csv.DictReader(fh):
            if rec["motor"] in rows:
                rows[rec["motor"]].append((float(rec["angle"]), int(rec["steps"])))
    tables = {}
    for motor, recs in rows.items():
        tables[motor] = MotorTable(motor, recs)
    log("tables loaded from %s: az %d rows (%g..%g), el %d rows (%g..%g), sk %d rows (%g..%g)" % (
        matches[0],
        len(tables["az"].angles), tables["az"].angles[0], tables["az"].angles[-1],
        len(tables["el"].angles), tables["el"].angles[0], tables["el"].angles[-1],
        len(tables["sk"].angles), tables["sk"].angles[0], tables["sk"].angles[-1]))
    return tables

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

def send(ser, cmd):
    # every outbound dcmc write passes the verb whitelist gate before touching the port
    verb = cmd.split()[0] if cmd.split() else ""
    if verb not in DCMC_ALLOWED:
        sys.exit("FATAL: attempted to emit forbidden dcmc verb %r" % verb)
    ser.reset_input_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    time.sleep(SETTLE)

def read_until(ser, marker, window):
    # collect output until the expected prompt marker or window expiry
    buf = ""
    end = time.time() + window
    while time.time() < end:
        chunk = ser.read(256).decode("ascii", "replace")
        if chunk:
            buf += chunk
            if marker in buf:
                return buf
    return buf

# ---------------- [SESSION] ----------------
def enter_dcmc(ser, verbose):
    # orient from any stale menu state up to the root prompt, then descend into dcmc
    for attempt in range(1, 4):
        ser.reset_input_buffer()
        ser.write(b"\x1b")
        time.sleep(SETTLE)
        ser.write(b"q\r")
        time.sleep(SETTLE)
        ser.write(b"\r")
        buf = read_until(ser, "ODU>", 5.0)
        if verbose:
            log("orientation attempt %d raw" % attempt, raw=buf)
        if "ODU>" in buf:
            break
        log("orientation attempt %d: no ODU> prompt, retrying" % attempt)
    else:
        sys.exit("no ODU> prompt in 3 attempts; other session open, unit off, cable, or wrong port")
    ser.write(b"motorcon\r")
    buf = read_until(ser, "MCON>", 3.0)
    if "MCON>" not in buf:
        sys.exit("motorcon did not reach MCON>: %r" % buf[-200:])
    ser.write(b"run\r")
    buf = read_until(ser, "MB OPEN COMMS SUCCESS", 5.0)
    if "MB OPEN COMMS SUCCESS" not in buf:
        sys.exit("dcmc bridge did not open: %r" % buf[-200:])
    buf = read_until(ser, "dcmc >", 2.0)
    if "dcmc >" not in buf:
        sys.exit("no dcmc > prompt after bridge open: %r" % buf[-200:])
    log("session established at dcmc > prompt")

def leave_dcmc(ser):
    # exit the dcmc app, then back out of MCON so the port is clean for other tools
    try:
        ser.reset_input_buffer()
        ser.write(b"\x1b")
        time.sleep(SETTLE)
        ser.write(b"q\r")
        time.sleep(SETTLE)
    except Exception:
        pass

def probe_package(ser, verbose):
    # one 'i' dump identifies the el steps/rev (package) and skew steps/rev (skew math)
    send(ser, "i")
    buf = read_until(ser, "dcmc >", 3.0)
    if verbose:
        log("probe raw", raw=buf)
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
    mt = re.search(r"Skew:(.*)", buf, re.S)
    if not mt:
        sys.exit("probe failed: no Skew section in 'i' output")
    spr = re.search(r"steps/rev\s*=\s*(\d+)", mt.group(1))
    if not spr:
        sys.exit("probe failed: no steps/rev in Skew section")
    sk_spr = int(spr.group(1))
    log("probe: el steps/rev=%d -> package=%s, skew steps/rev=%d" % (el_spr, pkg, sk_spr))
    return pkg, sk_spr

# ---------------- [DCMC_OPS] ----------------
class SessionLost(Exception):
    pass

BOOT_MARKERS = ("Unknown command", "Board Rev ID", "App Version:")

def parse_i_positions(dump):
    # line-based parse avoids matching 'target position' when grabbing 'position'
    positions = {}
    section = None
    for line in dump.splitlines():
        if "Azimuth:" in line:
            section = "az"
        elif "Elevation:" in line:
            section = "el"
        elif "Skew:" in line:
            section = "sk"
        elif "position" in line and "target" not in line:
            mt = re.search(r"position\s*=\s*(-?\d+)", line)
            if mt and section:
                positions[section] = int(mt.group(1))
    return positions

def query_positions(ser, verbose):
    # one 'i' round trip yields both az and el step counters
    send(ser, "i")
    buf = read_until(ser, "dcmc >", 3.0)
    if verbose:
        log("query raw", raw=buf)
    for marker in BOOT_MARKERS:
        if marker in buf:
            raise SessionLost("dcmc session lost; ODU rebooted or at root prompt")
    if "dcmc >" not in buf:
        raise SessionLost("no dcmc > prompt in reply; session presumed dead")
    pos = parse_i_positions(buf)
    stalls = set()
    if "Azimuth:" in buf:
        sect = buf.split("Azimuth:", 1)[1].split("Elevation:", 1)[0]
        if "Mtr Stalled" in sect:
            stalls.add("az")
    if "Elevation:" in buf:
        sect = buf.split("Elevation:", 1)[1].split("Skew:", 1)[0]
        if "Mtr Stalled" in sect:
            stalls.add("el")
    if stalls:
        ser.__dict__.setdefault("_stall_queue", []).extend(stalls)
    if "az" not in pos or "el" not in pos:
        log("WARNING: position parse incomplete: %r" % buf[-200:])
        return None
    return pos

STATUS_ENTRY_RE = re.compile(r"\[(\d)\]\s*Cmd\s+(Complete|In Progress)")

def query_motor_status(ser, verbose):
    # one 't' round trip yields per-motor Complete / In Progress, entries ordered az then el
    send(ser, "t")
    buf = read_until(ser, "dcmc >", 3.0)
    if verbose:
        log("status raw", raw=buf)
    for marker in BOOT_MARKERS:
        if marker in buf:
            raise SessionLost("dcmc session lost; ODU rebooted or at root prompt")
    found = STATUS_ENTRY_RE.findall(buf)
    return {"az": found[0][1] if len(found) > 0 else None,
            "el": found[1][1] if len(found) > 1 else None}

def goto_steps(ser, motor, steps, verbose):
    # dcmc accepts one motor per command; az and el are issued as separate writes
    send(ser, "p %d %d" % (motor, steps))
    if verbose:
        log("sent p %d %d" % (motor, steps))

def stop_all(ser, verbose):
    for motor in (MOTOR_AZ, MOTOR_EL):
        send(ser, "s %d" % motor)
    log("stop issued for az and el")

# ---------------- [UNWRAP] ----------------
def unwrap_az(client_az, last_mount_az):
    # pick the winding of client azimuth that stays inside the mechanical envelope nearest the last aim
    best = None
    for k in (-1, 0, 1):
        cand = client_az + 360.0 * k - HEADING_DEG
        if AZ_MIN_DEG <= cand <= AZ_MAX_DEG:
            if best is None or abs(cand - last_mount_az) < abs(best - last_mount_az):
                best = cand
    return best

# ---------------- [ROTOR_PROTOCOL] ----------------
class RotorServer:
    def __init__(self, ser, tables, verbose, park_az=None, park_el=None, idle_park_sec=IDLE_PARK_SEC):
        self.ser = ser
        self.tables = tables
        self.verbose = verbose
        self.park_az = park_az
        self.park_el = park_el
        self.idle_park_sec = idle_park_sec
        self.last_cmd_az = None
        self.last_cmd_el = None
        self.last_mount_az = None
        self.last_client_activity = time.time()
        self.parked = False
        self._pending_az = None
        self._defer_time = 0.0
        self._stall_flags = set()

    def seed_position(self):
        pos = query_positions(self.ser, self.verbose)
        if pos is None:
            log("WARNING: cannot seed position, assuming az table minimum")
            self.last_mount_az = AZ_MIN_DEG
            return
        ang_az = self.tables["az"].steps_to_angle(pos["az"])
        ang_el = self.tables["el"].steps_to_angle(pos["el"])
        self.last_mount_az = ang_az if ang_az is not None else AZ_MIN_DEG
        log("seed: az steps=%d (%.2f deg), el steps=%d (%.2f deg)" % (
            pos["az"], ang_az if ang_az is not None else float("nan"),
            pos["el"], ang_el if ang_el is not None else float("nan")))

    def _issue_dual(self, steps_az, steps_el, target_el, el_start):
        # el first; defer az only for bottom-start climbs exceeding the concurrent band
        goto_steps(self.ser, MOTOR_EL, steps_el, self.verbose)
        if el_start <= EL_CONCURRENT_MAX_DEG and (target_el - el_start) > EL_CONCURRENT_MAX_DEG:
            self._pending_az = steps_az
            self._defer_time = time.time()
            log("DEFER: az write held %.1f s (el climb %.1f -> %.1f deg exceeds concurrent band)"
                % (START_GAP_SEC, el_start, target_el))
        else:
            goto_steps(self.ser, MOTOR_AZ, steps_az, self.verbose)

    def _release_pending_az(self):
        if self._pending_az is not None and time.time() - self._defer_time >= START_GAP_SEC:
            goto_steps(self.ser, MOTOR_AZ, self._pending_az, self.verbose)
            self.last_cmd_az = self.tables["az"].steps_to_angle(self._pending_az)
            self._pending_az = None
            log("RELEASE: deferred az write issued after %.1f s" % START_GAP_SEC)

    def park_now(self, reason):
        # slew to the configured park position through the same clamps as tracking moves
        if self.park_az is None or self.park_el is None:
            log("PARK: requested (%s) but no park position configured" % reason)
            return
        steps_az = self.tables["az"].angle_to_steps(min(max(self.park_az, AZ_MIN_DEG), AZ_MAX_DEG))
        steps_el = self.tables["el"].angle_to_steps(min(max(self.park_el, EL_MIN_DEG), EL_MAX_DEG))
        if steps_az is None or steps_el is None:
            log("PARK: requested (%s) but park position outside table domain" % reason)
            return
        self._issue_dual(steps_az, steps_el, self.park_el, self._last_el_deg())
        self.last_cmd_az = self.park_az
        self.last_cmd_el = self.park_el
        self.parked = True
        log("PARK (%s): az %.2f -> steps %d, el %.2f -> steps %d" % (reason, self.park_az, steps_az, self.park_el, steps_el))

    def check_idle(self):
        # idle option: park after a configurable window of client silence
        if not self.parked and self.park_az is not None and self.idle_park_sec > 0:
            if time.time() - self.last_client_activity > self.idle_park_sec:
                self.park_now("idle %ds" % self.idle_park_sec)

    def handle_line(self, line):
        parts = line.strip().split(" ")
        verb = parts[0] if parts else ""
        if verb == "P":
            return self.cmd_set_pos(parts)
        elif verb == "p":
            return self.cmd_get_pos()
        elif verb == "S":
            stop_all(self.ser, self.verbose)
            return "RPRT 0\n"
        elif verb == "_":
            return INFO_STRING + "\n"
        elif verb == "q":
            return "RPRT 0\n", True
        log("UNKNOWN CMD=%r" % line)
        return "RPRT -1\n"

    def cmd_set_pos(self, parts):
        self.last_client_activity = time.time()
        self.parked = False
        if len(parts) < 3:
            return "RPRT -1\n"
        try:
            client_az = float(parts[1])
            client_el = float(parts[2])
        except ValueError:
            return "RPRT -1\n"
        mount_az = unwrap_az(client_az, self.last_mount_az if self.last_mount_az is not None else AZ_MIN_DEG)
        if mount_az is None:
            log("HOLD: az %.2f has no winding inside az limits %g..%g" % (client_az, AZ_MIN_DEG, AZ_MAX_DEG))
            return "RPRT 0\n"
        self.last_mount_az = mount_az
        mount_el = client_el
        if mount_el < EL_MIN_DEG or mount_el > EL_MAX_DEG:
            mount_el = min(max(mount_el, EL_MIN_DEG), EL_MAX_DEG)
            log("CLAMP: el %.2f to %.2f" % (client_el, mount_el))
        if self.last_cmd_az is not None and abs(mount_az - self.last_cmd_az) < DEADBAND_DEG \
                and abs(mount_el - (self.last_cmd_el if self.last_cmd_el is not None else mount_el)) < DEADBAND_DEG:
            return "RPRT 0\n"
        steps_az = self.tables["az"].angle_to_steps(mount_az)
        steps_el = self.tables["el"].angle_to_steps(mount_el)
        if steps_az is None or steps_el is None:
            log("HOLD: angle outside table domain (az %.2f el %.2f)" % (mount_az, mount_el))
            return "RPRT 0\n"
        self._issue_dual(steps_az, steps_el, mount_el, self._last_el_deg())
        self.last_cmd_az = mount_az
        self.last_cmd_el = mount_el
        log("MOVE: az %.2f -> steps %d, el %.2f -> steps %d" % (mount_az, steps_az, mount_el, steps_el))
        return "RPRT 0\n"

    def _last_el_deg(self):
        return self.last_cmd_el if self.last_cmd_el is not None else 0.0

    def cmd_get_pos(self):
        self.last_client_activity = time.time()
        pos = query_positions(self.ser, self.verbose)
        self._stall_flags |= set(getattr(self.ser, "_stall_queue", []))
        self.ser.__dict__["_stall_queue"] = []
        if pos is None:
            return "RPRT -1\n"
        self._release_pending_az()
        if "Mtr Stalled" in self._stall_flags:
            log("WARNING: %s motor stalled (last known position az=%s el=%s); halting az, clearing deadband"
                % (self._stall_flags, self.last_cmd_az, self.last_cmd_el))
            stop_all(self.ser, self.verbose)
            self.last_cmd_az = None
            self.last_cmd_el = None
            self._stall_flags = set()
        ang_az = self.tables["az"].steps_to_angle(pos["az"])
        ang_el = self.tables["el"].steps_to_angle(pos["el"])
        if ang_az is None:
            ang_az = self.last_mount_az if self.last_mount_az is not None else 0.0
        if ang_el is None:
            ang_el = self.last_cmd_el if self.last_cmd_el is not None else 0.0
        if self.last_cmd_az is not None and self._pending_az is None:
            dev_az = abs(ang_az - self.last_cmd_az)
            dev_el = abs((self.last_cmd_el if self.last_cmd_el is not None else ang_el) - ang_el)
            if max(dev_az, dev_el) > QC_TOLERANCE_DEG:
                mstat = query_motor_status(self.ser, self.verbose)
                if mstat.get("az") == "Complete" and mstat.get("el") == "Complete":
                    log("QC FAIL: cmd az %.2f el %.2f vs actual az %.2f el %.2f; re-issuing"
                        % (self.last_cmd_az, self.last_cmd_el, ang_az, ang_el))
                    steps_az = self.tables["az"].angle_to_steps(self.last_cmd_az)
                    steps_el = self.tables["el"].angle_to_steps(self.last_cmd_el)
                    if steps_az is not None and steps_el is not None:
                        self._issue_dual(steps_az, steps_el, self.last_cmd_el, ang_el)
        client_az = (ang_az + HEADING_DEG) % 360.0
        return "%.3f\n%.3f\n" % (client_az, ang_el)

# ---------------- [LISTENER] ----------------
def serve_forever(srv_sock, ser, tables, verbose, server):
    log("listening for rotctld clients")
    srv_sock.settimeout(5.0)
    while True:
        try:
            conn, addr = srv_sock.accept()
        except socket.timeout:
            server.check_idle()
            continue
        log("CONNECT from %s:%d" % (addr[0], addr[1]))
        buf = ""
        close = False
        try:
            while not close:
                data = conn.recv(1024)
                if not data:
                    break
                buf += data.decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        result = server.handle_line(line)
                    except SessionLost as exc:
                        log("SESSION RESET: %s" % exc)
                        recovered = False
                        for attempt in range(3):
                            time.sleep(2.0)
                            try:
                                enter_dcmc(ser, verbose)
                                server.seed_position()
                                recovered = True
                                break
                            except (SystemExit, SessionLost):
                                log("re-entry attempt %d failed" % (attempt + 1))
                        if not recovered:
                            log("could not re-enter dcmc after 3 attempts; exiting daemon")
                            raise
                        result = "RPRT -1\n"
                    if isinstance(result, tuple):
                        response, close = result
                    else:
                        response, close = result, False
                    if response:
                        conn.sendall(response.encode("utf-8"))
                    if close:
                        break
        except ConnectionResetError:
            log("RESET: client dropped connection without q")
        finally:
            conn.close()
            log("DISCONNECT from %s:%d" % (addr[0], addr[1]))

# ---------------- [MAIN] ----------------
def main():
    args = parse_args()
    log("dcmc_rotor starting: device=%s baud=%d listen=%s:%d" % (args.device, args.baud, args.address, args.port))
    ser = make_serial(args.device, args.baud)
    srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        enter_dcmc(ser, args.verbose)
        pkg, sk_spr = probe_package(ser, args.verbose)
        tables = load_tables(args.tables_dir, pkg, args.verbose)
        if ZERO_SK_ON_START:
            sk_steps = tables["sk"].angle_to_steps(SK_ZERO_ANGLE)
            if sk_steps is None:
                sys.exit("SK_ZERO_ANGLE %.2f outside skew table domain" % SK_ZERO_ANGLE)
            goto_steps(ser, MOTOR_SK, sk_steps, args.verbose)
            log("SKEW: %.2f deg -> motor %d steps %d issued at startup" % (SK_ZERO_ANGLE, MOTOR_SK, sk_steps))
        server = RotorServer(ser, tables, args.verbose, park_az=PARK_AZ_DEG, park_el=PARK_EL_DEG,
                             idle_park_sec=(0 if args.no_idle_park else IDLE_PARK_SEC))
        srv_sock.bind((args.address, args.port))
        srv_sock.listen(1)
        if args.park:
            server.park_now("at-start --park")
        serve_forever(srv_sock, ser, tables, args.verbose, server)
    except KeyboardInterrupt:
        log("Ctrl-C: shutting down")
        stop_all(ser, args.verbose)
    finally:
        leave_dcmc(ser)
        ser.close()
        srv_sock.close()
        log("clean exit; session left oriented toward ODU>")

if __name__ == "__main__":
    main()
