#!/usr/bin/env python3
"""Paced S-record upload to the Winegard Trav'ler K64 ODU bootloader (BL 04.00.0A).

Behaviour taken from disassembling the bootloader:
  * 'ld' erases 0x10000-0xFEFFF (the last sector at 0xFF000 is kept), then
    prints "Please start S-REC/iHex download:".
  * Lines are parsed on CR (LF is ignored). After each record it prints
    '.'  record OK            '!'  address outside 0x10000-0xFEFFF, ignored
    Record-error / CS-error / Programming-error / Code hole  -> fatal
  * The first 8 bytes at 0x10000 (the ADBCCBDA signature) are held in RAM and
    written only when the S7/S8/S9 end record arrives. Then it prints
    "Terminated - restarting..." and reboots. That message means success.
  * Ctrl-C (0x03) aborts a download.

usage: odu_bl_upload.py PORT BAUD FILE.s19 [--no-ld]
       odu_bl_upload.py --make-s19 FULL_OR_APP.bin OUT.s19
"""
import struct, sys, time

FATAL = (b"Record-error", b"CS-error", b"Programming-error", b"Code hole", b"Aborted")


def make_s19(src, out):
    raw = open(src, "rb").read()
    base = 0x10000 if len(raw) > 0x10000 and raw[0x10000:0x10004] == bytes.fromhex("dacbbcad") else 0
    img = raw[base:]
    if img[:4] != bytes.fromhex("dacbbcad"):
        sys.exit("no ADBCCBDA header at the start of the app area")
    length = struct.unpack_from("<I", img, 0x30)[0]
    end = 0x100 + length

    def rec(t, addr, data, alen):
        body = bytes([len(data) + alen + 1]) + addr.to_bytes(alen, "big") + data
        return "S%d%s%02X" % (t, body.hex().upper(), (~sum(body)) & 0xFF)

    lines = [rec(0, 0, b"odu app", 2)]
    lines += [rec(3, 0x10000 + o, img[o:min(o + 16, end)], 4) for o in range(0, end, 16)]
    lines.append(rec(7, 0, b"", 4))
    open(out, "w").write("\r\n".join(lines) + "\r\n")
    print(f"wrote {out}: 0x10000-{0x10000 + end:#x}, {len(lines) - 2} data records")


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--make-s19":
        return make_s19(sys.argv[2], sys.argv[3])
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    import serial
    port, baud, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    records = [l.strip() for l in open(path) if l.strip()]
    ser = serial.Serial(port, baud, timeout=0.05, xonxoff=True)

    def wait_for(tokens, timeout):
        buf, t0 = b"", time.time()
        while time.time() - t0 < timeout:
            buf += ser.read(256)
            buf = buf.replace(b"\x11", b"").replace(b"\x13", b"")
            for tok in tokens:
                if tok in buf:
                    return tok, buf
        return None, buf

    if "--no-ld" not in sys.argv:
        ser.reset_input_buffer()
        ser.write(b"\r")
        time.sleep(0.3)
        ser.reset_input_buffer()
        ser.write(b"ld\r")
        tok, buf = wait_for([b"Please start S-REC"], 120)   # erase can take a while
        sys.stdout.write(buf.decode("ascii", "replace"))
        if not tok:
            sys.exit("\nno download prompt; is the board at the bootloader '>' prompt?")

    bangs, pages, t0 = 0, 0, time.time()
    for i, line in enumerate(records):
        ser.write(line.encode() + b"\r")
        last = line[1] in "789"
        expect = [b"Terminated"] if last else [b".", b"!", b"*"]
        tok, buf = wait_for(expect + list(FATAL), 30 if last else 15)
        if tok in FATAL or tok is None:
            ser.write(b"\x03")
            sys.exit(f"\nrecord {i} failed ({tok or 'timeout'}): {line}\n"
                     f"device said: {buf.decode('ascii', 'replace')!r}")
        if tok == b"!":
            bangs += 1
        if tok == b"*":
            pages += 1
        if i % 500 == 0 or last:
            pct = 100 * (i + 1) / len(records)
            sys.stdout.write(f"\r  {i + 1}/{len(records)} ({pct:.0f}%) {time.time() - t0:.0f}s ")
            sys.stdout.flush()
    print(f"\ndone: bootloader reported 'Terminated - restarting...' (= success). "
          f"{bangs} out-of-range records ignored, {pages} page commits.")
    print(wait_for([b"ODU>", b"TRK>"], 60)[1].decode("ascii", "replace"))


if __name__ == "__main__":
    main()
