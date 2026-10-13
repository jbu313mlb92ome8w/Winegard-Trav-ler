import serial, sys, struct, time, os

def print_help():
    help_text = """
ODU Flash Extraction Tool
=========================
Extracts MK64FN1M0VLQ12 flash memory contents via the ODU console 'dw' command.

USAGE:
    python3 odu_dump.py PORT BAUD OUTPUT [START] [END]

PARAMETERS:
    PORT      Serial device path          (e.g. /dev/ttyUSB2)
    BAUD      Baud rate                    (e.g. 57600)
    OUTPUT    Output binary file           (e.g. odu_full.bin)
    START     Optional start address       (hex 0x... or decimal, default 0x0)
    END       Optional end address         (hex 0x... or decimal, default 0x100000)

OPTIONS:
    -h, --help    Show this help message and exit

EXAMPLES:
    Full 1 MB extraction:
        python3 odu_dump.py /dev/ttyUSB0 57600 odu_full.bin

    Resume interrupted dump (auto-detected from file size):
        python3 odu_dump.py /dev/ttyUSB0 57600 odu_full.bin

    Start fresh (delete existing file first):
        rm odu_full.bin
        python3 odu_dump.py /dev/ttyUSB0 57600 odu_full.bin

    Dump specific range:
        python3 odu_dump.py /dev/ttyUSB0 57600 probe.bin 0x10000 0x10400

NOTES:
    * Addresses sent to ODU use bare hex (no 0x prefix) per console
      parser requirement; the ODU silently falls back to address 0
      when given '0x...' format.
    * Device TX buffer caps at 41 lines; script uses 32 lines
      (512-byte blocks) for safe margin.
    * Existing output file is auto-detected and appended to
      (resume). Delete the file to start fresh.
    * All commands are read-only ('dw'); no flash write risk.
    * Interrupted runs leave a valid prefix; realigns to the
      nearest 512-byte boundary on resume.

DEFAULT RANGE: 0x00000000 - 0x00100000 (full 1 MB MK64 flash)
"""
    print(help_text)

PORT = sys.argv[1]
BAUD = int(sys.argv[2])
OUT = sys.argv[3]
START = int(sys.argv[4], 0) if len(sys.argv) > 4 else 0x00000000
END = int(sys.argv[5], 0) if len(sys.argv) > 5 else 0x00100000
LINES_PER_CMD = 32
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
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print_help()
        return 0
    if len(sys.argv) < 4:
        print(f"Error: missing required arguments (need PORT BAUD OUTPUT)")
        print_help()
        return 1
    if os.path.exists(OUT):
        file_size = os.path.getsize(OUT)
        if file_size > 0:
            resume_addr = file_size
            if resume_addr % 512 != 0:
                print(f"WARNING: File not aligned ({file_size} bytes). Truncating to {resume_addr & ~511}.")
                resume_addr = resume_addr & ~511
            if START == 0:
                print(f"\n*** RESUME DETECTED ***")
                print(f"Existing file: {OUT}")
                print(f"Already dumped: {file_size // 1024} KB (0x{file_size:x})")
                print(f"Resuming from address: 0x{resume_addr:x}")
                print(f"Remaining: {(END - resume_addr) // 1024} KB")
                START = resume_addr
                append_mode = True
            elif START == resume_addr:
                print(f"\n*** CONTINUING FROM SPECIFIED OFFSET 0x{START:x} ***")
                append_mode = True
            else:
                print(f"\n*** WARNING: File exists but START={START:#x} differs from file size={file_size} ***")
                print(f"Either specify START=0 to auto-resume, or delete {OUT} to start fresh.")
                return 1
        else:
            append_mode = False
    else:
        print(f"\nStarting fresh extraction:")
        append_mode = False

    if START >= END:
        print(f"Start address 0x{START:x} >= end 0x{END:x}; nothing to do.")
        return 0

    print(f"Output: {OUT}")
    print(f"Range: 0x{START:x} - 0x{END:x} ({(END-START)//1024} KB)")

    ser = serial.Serial(PORT, BAUD, timeout=0.2)
    mode_str = "ab" if append_mode else "wb"
    with open(OUT, mode_str) as out:
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
            eta = (1-done) * (END-START)/1024 / 0.5
            print(f"Progress: {addr:#010x}  {done:6.1%}  (ETA ~{int(eta)}s)")
            block_num += 1
    print(f"\n=== COMPLETE: {block_num} blocks, {addr-START} total bytes written ===")
    print(f"Final file size: {os.path.getsize(OUT)} bytes")
    return 0

sys.exit(main())
