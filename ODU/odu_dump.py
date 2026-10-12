# ===================================================================== odu_dump_verbose
import serial, sys, struct, time

PORT = sys.argv[1]
BAUD = int(sys.argv[2])
OUT = sys.argv[3]
START = int(sys.argv[4], 0) if len(sys.argv) > 4 else 0x00000000
END = int(sys.argv[5], 0) if len(sys.argv) > 5 else 0x00100000
# Safe
LINES_PER_CMD = 32
# Max
#LINES_PER_CMD = 40
VERBOSE = True

def send_cmd(ser, cmd):
    ser.reset_input_buffer()
    if VERBOSE:
        print(f"SEND: {cmd}")
    ser.write((cmd + "\r\n").encode())
    time.sleep(0.15)

def read_until_prompt(ser, timeout=4.0):
    buf = b""
    t0 = time.time()
    while time.time() - t0 < timeout:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
        if buf.endswith(b"ODU>") or buf.rstrip().endswith(b"ODU>"):
            break
        if not chunk:
            time.sleep(0.02)
    return buf.decode("ascii", "replace")

def parse_dump(text, expect_addr, expect_bytes):
    blob = bytearray()
    got_lines = 0
    for line in text.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        head, _, payload = line.partition(":")
        if _ != ":":
            continue
        addr = int(head.strip(), 16)
        if addr != expect_addr + got_lines * 16:
            raise ValueError(f"addr mismatch: got {addr:#010x}, expected {expect_addr + got_lines * 16:#010x}")
        fields = payload.split()
        if len(fields) != 8:
            raise ValueError(f"bad field count at {addr:#010x}: {payload!r}")
        line_bytes = bytearray()
        for f in fields:
            val = int(f, 16)
            line_bytes += struct.pack("<H", val)
        if VERBOSE:
            print(f"  LINE {got_lines}: @ {addr:#010x} | {fields}")
            print(f"       bytes: {line_bytes.hex(' ')}")
        blob.extend(line_bytes)
        got_lines += 1
    if got_lines * 16 != expect_bytes:
        raise ValueError(f"short dump: {got_lines} lines, expected {expect_bytes // 16}")
    return bytes(blob)

def main():
    print(f"Opening {PORT} @ {BAUD} baud")
    print(f"Output: {OUT}")
    print(f"Range: 0x{START:x} - 0x{END:x} ({(END-START)//1024} KB)")
    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    with open(OUT, "wb") as out:
        addr = START
        block_num = 0
        while addr < END:
            n = min(LINES_PER_CMD, (END - addr + 15) // 16)
            cmd = f"dw {addr:x} {n}"
            print(f"\n=== Block {block_num}: {addr:#010x} for {n*16} bytes ===")
            for attempt in range(3):
                try:
                    if VERBOSE:
                        print(f"Flushing buffer...")
                    text = read_until_prompt(ser)
                    send_cmd(ser, cmd)
                    if VERBOSE:
                        print("Waiting for response...")
                    text = read_until_prompt(ser)
                    if VERBOSE:
                        print(f"Raw response snippet:\n{text[:500]}...")
                    blob = parse_dump(text, addr, n * 16)
                    break
                except ValueError as e:
                    print(f"RETRY {attempt+1}/3 at {addr:#x}: {e}")
            else:
                print(f"FAILED at {addr:#x} after 3 retries; aborting")
                return 1
            out.write(blob)
            out.flush()
            block_size = len(blob)
            print(f"BLOCK OK: wrote {block_size} bytes to {OUT}")
            addr += n * 16
            done = (addr - START) / (END - START)
            eta = (1-done) * (END-START)/1024 / 0.5  # rough estimate at ~0.5KB/s
            print(f"Progress: {addr:#010x}  {done:6.1%}  (ETA ~{int(eta)}s)")
            block_num += 1
    print(f"\n=== COMPLETE: {block_num} blocks, {addr-START} total bytes written ===")
    return 0

sys.exit(main())
# ================================================================== end odu_dump_verbose
