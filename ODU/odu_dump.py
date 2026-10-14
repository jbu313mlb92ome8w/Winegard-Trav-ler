import serial, sys, struct, time, os, hashlib, re, random

def print_help():
    help_text = """
ODU Flash Extraction Tool
=========================
Extracts flash memory contents via the ODU console 'dw' command.

USAGE:
    python3 odu_dump.py PORT BAUD OUTPUT [START] [END] [FLAGS]

PARAMETERS:
    PORT      Serial device path       (e.g. /dev/ttyUSB0)
    BAUD      Baud rate                (e.g. 57600)
    FLAGS     See FLAGS for list.
    OUTPUT    Output binary file path. See EXAMPLES.
    START     Optional start address   (hex 0x... or decimal, default 0x0)
    END       Optional end address     (hex 0x... or decimal, default from 'id' command)

FLAGS:
    -v1               Target board variant 1: no 'header' command, TRK>
                      prompt, flash at non-zero base (e.g. OKI 512K at
                      0xC8000000). Range defaults to the 'id' flash
                      region; app magic 0xFACEC0DE is verified.
                      MCU is OKI L67Q5003A-NNN. Device TX buffer caps at
                      102 lines.
    -v3               Target board variant 3: 'header' command present,
                      ODU> prompt, flash at 0x0, ADBCCBDA app magic.
                      MCU is NXP MK64FN1M0VLK12. Device TX buffer caps at
                      40 lines.
    -a, --app-only    Extract only the application region using addresses
                      parsed from the 'id' command. File suffix '_app'
                      is added when -n/--auto-name is used.
    -n, --auto-name   Derive filename from ODU Software Version field
                      OUTPUT must be a directory path (not a filename).
                      Produces odu_v<version>_full.bin or _app.bin.
    -v, --verbose     Enable full per-line terminal output (debug,
                      per-block messages, raw response snippets).
                      Default is quiet mode: static header/ID panels
                      with a single-line progress bar and ETA.
    -h, --help        Show this help message and exit

AUTO FILE NAMING:
    Passing "-n" flag (with optional directory prefix) instructs
    the script to query the ODU 'header' command first and derive the
    filename from its Software Version field, producing a file like:
        odu_v1.23.45_full.bin
    With -a/--app-only flag:
        odu_v1.23.45_app.bin
    Auto naming works with directory paths:
        -n dir/     ->  dir/odu_v1.23.45_full.bin
        -a -n dir/  ->  dir/odu_v1.23.45_app.bin
    The full header and id blocks are printed before the extraction begins.
    Resume logic applies: if the file exists, it is appended from the
    file-size boundary (512-byte aligned).

MEMORY MAP:
    Dynamically extracted from the ODU 'id' command output. The
    Application Base, Bootloader Base, and sizes determine the
    default ranges if -a/--app-only is not specified. This ensures
    compatibility across firmware revisions with different partition
    boundaries.


EXAMPLES:
    Full extraction with (automatic filename with -n and _full suffix):
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# -n

    App-only extraction (automatic filename with -n and _app suffix):
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# -n -a

    Full extraction with explicit filename:
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# custom.bin

    Auto naming with directory creation:
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# -n dir/

    Explicit filename with directory:
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# dir/custom.bin

    Custom explicit range (overriding 'id' parsing):
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# custom.bin 0x10000 0x10001

    Resume interrupted extraction (auto-detected from file size and flag):
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# -n
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# -a -n
        python3 odu_dump.py /dev/ttyUSB0 57600 -v# custom.bin 0x10000 0x10001

NOTES:
    * All commands are read-only ('header', 'id', and 'dw'); no flash
      write risk.
    * The ODU 'header' and 'id' commands are issued at startup and
      their fields are printed for reference.
    * Parent directories in OUTPUT paths are created automatically.
    * Addresses sent to ODU use bare hex (no 0x prefix) per console
      parser requirement; the ODU silently falls back to address 0
      when given '0x...' format.
    * Existing output file is auto-verified before resume:
        1) Head check (first 16 bytes match device at base)
        2) 5 random spot checks (interior continuity)
        3) Tail check (boundary matches flash)
      For -a/--app-only, all checks are clamped to the application
      region. On failure, options are 512-byte truncation repair,
      delete-and-restart, or abort.
    * Interrupted runs leave the file as-written (complete blocks);
      resume-time verification gates continuation.
    * SHA256 and MD5 of the completed file are printed at the end,
      along with total elapsed time, enabling cross-device comparisons.
    * Sub-16-byte extraction is supported via final-block trimming.
      Custom ranges must align to 2-byte boundaries for safety.

DEFAULT RANGE: Full flash from 0x0 to module default 0x100000.
               Override with explicit START END arguments.
"""
    print(help_text)

PORT = None
BAUD = None
OUT = None
START = 0x0
END = 0x100000
LINES_PER_CMD = 40
VERBOSE = False
APP_ONLY = False
PROMPT = b"ODU>"      # console prompt, set by -v1/-v3
HAS_HEADER = True     # -v1 boards lack the 'header' command

def ensure_output_dir(path):
    # Ensure the parent directory of 'path' exists; create if missing.
    sep = '/' if '/' in path else '\\'
    if sep not in path:
        return  # No directory component in bare filename
    idx = path.rfind(sep)
    dirname = path[:idx] or '.'
    if os.path.isdir(dirname):
        return  # Exists, no action needed
    try:
        os.makedirs(dirname)
        if VERBOSE:
            print(f"Created directory: {dirname}")
    except OSError as e:
        raise RuntimeError(f"cannot create directory {dirname}: {e}")

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
        if buf.endswith(PROMPT) or buf.rstrip().endswith(PROMPT):
            break
        if not chunk:
            time.sleep(0.02)
    return buf.decode("ascii", "replace")

def fetch_header(ser):
    ser.reset_input_buffer()
    ser.write(b"header\r\n")
    time.sleep(0.2)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 3.0:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
        if buf.rstrip().endswith(PROMPT):
            break
        if not chunk:
            time.sleep(0.02)
    text = buf.decode("ascii", "replace")
    fields = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
    return fields, text

MEM_KEYS = ("Flash Base Address", "Flash Size", "Bootloader Address",
            "Bootloader Size", "Application Address", "Application Size",
            "NVV Address", "NVV Size", "NVS Address", "NVS Size")

def fetch_id(ser):
    ser.reset_input_buffer()
    ser.write(b"id\r\n")
    time.sleep(0.2)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 3.0:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
        if buf.rstrip().endswith(PROMPT):
            break
        if not chunk:
            time.sleep(0.02)
    text = buf.decode("ascii", "replace")
    fields = {}
    # Identity fields kept as strings
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val and key not in fields:
                fields[key] = val
    # Memory map fields parsed as bare hexadecimal (console convention)
    for line in text.splitlines():
        line = line.strip()
        for key in MEM_KEYS:
            if line.startswith(key + ":"):
                try:
                    fields[key] = int(line.split(":", 1)[1].strip(), 16)
                except ValueError:
                    fields[key] = line.split(":", 1)[1].strip()
                break
    return fields, text

def fetch_flash_words(ser, addr, count=1):
    ser.reset_input_buffer()
    ser.write(f"dw {addr:x} {count * 2}\r\n".encode())
    time.sleep(0.25)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 3.0:
        chunk = ser.read(4096)
        if chunk:
            buf += chunk
        if buf.rstrip().endswith(PROMPT):
            break
        if not chunk:
            time.sleep(0.02)
    halves = []
    for line in buf.decode("ascii", "replace").splitlines():
        line = line.strip()
        if is_data_line(line):
            fields = line.partition(":")[2].split()
            if len(fields) == 8:
                halves += [int(f, 16) for f in fields]
    words = []
    for i in range(count):
        if i * 2 + 1 < len(halves):
            words.append((halves[i * 2 + 1] << 16) | halves[i * 2])
    return words

def ff_wall_corroborates(ser, boundary, words_after=2):
    # Verify erased flash (0xFFFFFFFF) begins at the computed image boundary
    after = fetch_flash_words(ser, boundary, words_after)
    if not after or len(after) < words_after:
        return False
    return all(w == 0xFFFFFFFF for w in after)

def parse_hex_field(val):
    # Convert a console-reported hex string like '804F4A4B' to int, or None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int("".join(c for c in val if c.isalnum()), 16)
        except ValueError:
            return None
    return None

def is_data_line(line):
    # True when line begins with a hex address and a colon.
    head, sep, _ = line.partition(":")
    if sep != ":" or not head:
        return False
    return all(c in "0123456789abcdefABCDEF" for c in head.strip())

def derive_struct_length(ser, app_base, hdr, flash_size=0x100000):
    # Anchor-and-corroborate: locate the reported checksum word in the
    # struct region, then test known length-unit interpretations for the
    # following word, confirming the computed image end hits erased flash.
    raw_words = fetch_flash_words(ser, app_base, 16)
    if not raw_words or raw_words[0] is None:
        return None, "struct region unreadable"
    ck_val = parse_hex_field(hdr.get("Checksum"))
    if ck_val is None:
        return None, "'header' reported no usable Checksum field"
    if ck_val not in raw_words:
        return None, f"checksum word {ck_val:08X} not found in struct region"
    idx = raw_words.index(ck_val)
    if idx + 1 >= len(raw_words):
        return None, "checksum word is last word read; length not within region"
    candidate = raw_words[idx + 1]
    vector_base = parse_hex_field(hdr.get("App vector address")) or app_base
    # Interpretations ordered by observed likelihood: halfwords before bytes,
    # app_base before vector_base (halfword/app_base confirmed on this unit).
    interp = [
        ("halfwords", app_base, 2),
        ("halfwords", vector_base, 2),
        ("bytes", app_base, 1),
        ("bytes", vector_base, 1),
    ]
    for unit, base, mult in interp:
        length = candidate * mult
        if not (0 < length < flash_size):
            continue
        boundary = base + length
        if boundary > flash_size:
            continue
        if ff_wall_corroborates(ser, boundary):
            return length, (f"anchored on checksum @+0x{idx*4:03x}, {unit} from "
                            f"0x{base:x}, FF-wall corroborated at 0x{boundary:x}")
    return None, (f"no interpretation of length word {candidate:#x} produced a "
                  f"corroborated FF wall (tested 1x/2x from app/vector base)")

def validate_id_fields(id_fields):
    required = ["Application Address", "Bootloader Address", "Flash Base Address"]
    missing = [k for k in required if k not in id_fields or not isinstance(id_fields.get(k), int)]
    return missing

def print_panels(hdr, id_fields):
    COL_W = 42  # left column width
    left = [f"{k}: {v}" for k, v in hdr.items() if k not in ("Header Signature", "Header Version")]
    right = [f"{k}: {hex(v) if isinstance(v, int) else v}" for k, v in id_fields.items()]
    rows = max(len(left), len(right))
    print()
    print(f"=== ODU HEADER ===".ljust(COL_W) + "=== ODU ID / MEMORY MAP ===")
    print("-" * COL_W + "-" * 30)
    for i in range(rows):
        l = left[i] if i < len(left) else ""
        r = right[i] if i < len(right) else ""
        print(l[:COL_W - 1].ljust(COL_W) + r[:38])
    print()

def draw_progress(block, addr, start, end, t_start, msg=""):
    frac = (addr - start) / (end - start) if end > start else 1.0
    elapsed = time.time() - t_start
    eta = (elapsed / frac * (1 - frac)) if frac > 0.005 else 0
    width = 30
    filled = int(width * frac)
    bar = "#" * filled + "-" * (width - filled)
    line = f"\r  Blk {block:<5d} @{addr:#09x} [{bar}] {frac:5.1%}  ETA {time.strftime('%H:%M:%S', time.gmtime(eta))}  {msg}"
    sys.stdout.write(line[:79])
    sys.stdout.flush()

def spot_verify(ser, dev_addr, file_obj, file_offset, nbytes=16):
    # Compare nbytes of device flash against a slice of the file.
    words = fetch_flash_words(ser, dev_addr, nbytes // 4)
    if len(words) < nbytes // 4:
        return False
    file_obj.seek(file_offset)
    expected = b"".join(struct.pack("<HH", w & 0xFFFF, w >> 16) for w in words)
    return file_obj.read(nbytes) == expected

def verify_random_spots(ser, base, file_obj, file_size, id_fields, app_only):
    # Perform 5 random spot checks inside the file, constrained to app region if -a.
    if file_size < 32:  # Too small for meaningful checks
        return True
    # Determine upper bound for random offsets
    limit = file_size
    if app_only:
        app_start = id_fields.get("Application Address")
        app_size = id_fields.get("Application Size")
        if isinstance(app_start, int) and isinstance(app_size, int):
            app_end = app_start + app_size
            # Clamp limit to app_end - base
            max_valid_len = max(0, app_end - base)
            if max_valid_len < limit:
                limit = max_valid_len
                print(f"    Clamping spot-check region to {limit} bytes (app boundary).")

    if limit < 32:
        return True

    # Pick 5 distinct offsets, skipping very start/end (already checked)
    safe_range = list(range(32, max(32, limit - 32)))
    if len(safe_range) < 5:
        return True  # Not enough room for 5 distinct mid-range spots

    random.seed(int(os.urandom(8).hex(), 16))  # Salt per run for reproducibility if desired
    offsets = sorted(random.sample(safe_range, 5))

    for i, off in enumerate(offsets):
        dev_addr = base + off
        if not spot_verify(ser, dev_addr, file_obj, off):
            print(f"\n*** SPOT CHECK {i+1}/5 FAILED ***")
            print(f"    Off {off:#x} (Addr 0x{dev_addr:x}) mismatch.")
            return False
    return True

def parse_dump(text, expect_addr, expect_bytes):
    blob = bytearray()
    got_lines = 0
    for line in text.splitlines():
        line = line.strip()
        if not is_data_line(line):
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
    global PORT, BAUD, OUT, START, END, APP_ONLY, PROMPT, HAS_HEADER, LINES_PER_CMD

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print_help()
        return 0
    if len(sys.argv) < 4:
        print(f"Error: missing required arguments (need PORT BAUD OUTPUT)")
        print_help()
        return 1

    global VERBOSE
    V1 = "-v1" in sys.argv
    V3 = "-v3" in sys.argv
    APP_ONLY = "-a" in sys.argv or "--app-only" in sys.argv
    AUTO_NAME = "-n" in sys.argv or "--auto-name" in sys.argv
    VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
    FLAGS = ("-a", "--app-only", "-h", "--help", "-v", "--verbose",
             "-n", "--auto-name", "-v1", "-v3")
    if V1 and V3:
        print("ERROR: -v1 and -v3 are mutually exclusive.")
        return 1
    if not V1 and not V3:
        print("ERROR: one of -v1 or -v3 is required.")
        print("       -v1: ODU without 'header' command (TRK> prompt)")
        print("       -v3: ODU with 'header' command (ODU> prompt)")
        return 1
    if V1:
        PROMPT = b"TRK>"
        HAS_HEADER = False
        LINES_PER_CMD = 102
    else:
        PROMPT = b"ODU>"
        HAS_HEADER = True
        LINES_PER_CMD = 40

    clean_args = [a for a in sys.argv if a not in FLAGS]
    if AUTO_NAME and len(clean_args) == 3:
        clean_args.append(".")
    if len(clean_args) < 4:
        print(f"Error: missing required arguments (need PORT BAUD OUTPUT)")
        print_help()
        return 1

    PORT = clean_args[1]
    BAUD = int(clean_args[2])
    OUT = clean_args[3]

    # -n/--auto-name requires OUTPUT to be a directory path, not a filename
    if AUTO_NAME:
        # -n mode: OUTPUT is the target directory, never a filename
        if OUT != ".":
            base = os.path.basename(OUT.rstrip("/"))
        else:
            base = ""
        if base and "." in base:
            print(f"ERROR: -n/--auto-name cannot be combined with a filename.")
            print(f"       '{OUT}' looks like a file. Pass a directory path only.")
            return 1
        out_dir = OUT if OUT != "." else ""
        if out_dir and not os.path.isdir(out_dir):
            try:
                os.makedirs(out_dir)
                if VERBOSE:
                    print(f"Created directory: {out_dir}")
            except OSError as e:
                print(f"ERROR: cannot create directory {out_dir}: {e}")
                return 4
    else:
        # Normal mode: OUT is a file path; create its parent directory
        ensure_output_dir(OUT)

    # Check for explicit START/END in clean_args after positional
    if len(clean_args) > 4:
        START = int(clean_args[4], 0)
    if len(clean_args) > 5:
        END = int(clean_args[5], 0)

    try:
        ser = serial.Serial(PORT, BAUD, timeout=0.2)
    except (serial.SerialException, OSError) as e:
        print(f"ERROR: cannot open {PORT} at {BAUD} baud: {e}")
        return 4
    except KeyboardInterrupt:
        print("\nAborted before port open.")
        return 130

    # Fetch and print header
    if HAS_HEADER:
        hdr, hdr_text = fetch_header(ser)
    else:
        hdr, hdr_text = {}, ""
        print("\n(-v1: 'header' command not available; using 'id' only)")
    id_fields, id_text = fetch_id(ser)

    sig_val = hdr.get("Header Signature")
    HEADER_MAGIC = int(sig_val, 16) if isinstance(sig_val, str) else None

    if VERBOSE:
        print("\n=== ODU HEADER ===")
        for k, v in hdr.items():
            print(f"  {k}: {v}")
        print(hdr_text)
        print("=== ODU ID / MEMORY MAP ===")
        for k, v in id_fields.items():
            if isinstance(v, int):
                print(f"  {k}: {v} (0x{v:x})")
            else:
                print(f"  {k}: {v}")
        print(id_text)
    else:
        print_panels(hdr, id_fields)

    missing_keys = validate_id_fields(id_fields)
    if missing_keys:
        print(f"\n*** WARNING: 'id' response incomplete or unparseable ***")
        print(f"    Missing/unparsed fields: {', '.join(missing_keys)}")
        print(f"    Falling back to default full-flash range 0x0-0x100000 (1 MB).")
        id_ok = False
    else:
        app_size = id_fields.get("Application Size")
        if not isinstance(app_size, int) or app_size >= 0x7F0000:
            app_base = id_fields.get("Application Address")
            HEADER_MAGIC = parse_hex_field(hdr.get("Header Signature"))
            if not isinstance(app_base, int):
                print(f"*** WARNING: no Application Address from 'id'; cannot auto-bound. ***")
                id_ok = False
            elif HEADER_MAGIC is None:
                print(f"*** WARNING: no Header Signature from 'header'; cannot auto-bound. ***")
                id_ok = False
            else:
                magic = fetch_flash_words(ser, app_base, 1)
                if not magic or magic[0] != HEADER_MAGIC:
                    print(f"*** WARNING: magic mismatch at 0x{app_base:x} "
                          f"(read {magic}, want {HEADER_MAGIC:08X}); refusing auto-bound. ***")
                    id_ok = False
                else:
                    length, why = derive_struct_length(
                        ser, app_base, hdr,
                        id_fields.get("Flash Size", 0x100000) if isinstance(id_fields.get("Flash Size"), int) else 0x100000)
                    if length is None:
                        print(f"*** WARNING: struct length derivation failed: {why}. ***")
                        id_ok = False
                    else:
                        id_fields["Application Size"] = length
                        print(f"    'Application Size' sentinel; using derived length "
                              f"0x{length:x} ({why}).")
                        id_ok = True
        else:
            id_ok = True

    # -v1 app headers carry 0xFACEC0DE at application base + 0x04.
    if V1 and id_ok:
        ab = id_fields.get("Application Address")
        if isinstance(ab, int):
            probe = fetch_flash_words(ser, ab, 2)
            if len(probe) >= 2 and probe[1] == 0xFACEC0DE:
                print(f"    App header magic 0xFACEC0DE verified at 0x{ab + 4:x}.")
            else:
                got = f"0x{probe[1]:08X}" if len(probe) >= 2 else "no data"
                print(f"*** WARNING: -v1 app magic not found at 0x{ab + 4:x} (read {got}). ***")

    # -v1 maps flash at a non-zero base; default range follows 'id'.
    explicit_range = len(clean_args) > 4
    if V1 and not APP_ONLY and not explicit_range:
        fb = id_fields.get("Flash Base Address")
        fs = id_fields.get("Flash Size")
        if isinstance(fb, int) and isinstance(fs, int):
            START = fb
            END = fb + fs
            print(f"\nDefault range from 'id': 0x{START:x} - 0x{END:x} ({(END-START)//1024} KB)")

    # Determine START/END from 'id' command if not overridden by -a or explicit args
    if APP_ONLY:
        # Use application region from id command
        if not id_ok:
            print(f"*** ERROR: -a/--app-only requires a valid 'id'/'header' memory map. ***")
            print(f"    Automatic bounding failed on this unit. Either:")
            print(f"    1) Retry (serial noise may have corrupted the response)")
            print(f"    2) Pass explicit START END arguments instead of -a")
            return 1
        START = id_fields["Application Address"]
        END = (START + id_fields["Application Size"] + 0x3FF) & ~0x3FF
        print(f"\nApp-only mode: range 0x{START:x} - 0x{END:x} ({(END-START)//1024} KB)")

    # Handle auto filename
    if AUTO_NAME:
        ver = hdr.get("Software Version")
        if not ver:
            ver = id_fields.get("App Version", "")
        if ver:
            safe = "".join(c for c in str(ver) if c.isalnum() or c in ".")
        else:
            safe = "unknown"
        fname = f"odu_v{safe}_app.bin" if APP_ONLY else f"odu_v{safe}_full.bin"
        OUT = os.path.join(OUT if OUT != "." else "", fname)
        print(f"\nOutput filename derived from version: {OUT}")

    if START >= END:
        print(f"Start address 0x{START:x} >= end 0x{END:x}; nothing to do.")
        return 0

    print(f"\nRange: 0x{START:x} - 0x{END:x} ({(END-START)//1024} KB)")

    t_start = time.time()

    resume_baseline = 0
    base = START  # invocation-determined base: 0x0, app address, or explicit
    append_mode = False

    def verify_file(size):
        # Head + 5 spots + tail against device flash; returns failure kind or None.
        with open(OUT, "rb") as vf:
            if not spot_verify(ser, base, vf, 0):
                return "head", 0
            if not verify_random_spots(ser, base, vf, size, id_fields, APP_ONLY):
                return "spot", 0
            tail_off = max(0, size - 16)
            if not spot_verify(ser, base + tail_off, vf, tail_off):
                return "tail", tail_off
        return None, 0

    if os.path.exists(OUT):
        file_size = os.path.getsize(OUT)
        if file_size > 0:
            fail_kind, fail_off = verify_file(file_size)
            while fail_kind is not None:
                print(f"\n*** RESUME VERIFICATION FAILED ({fail_kind} check at 0x{base + fail_off:x}) ***")
                print(f"    File may be corrupt, from a different base/mode, or the")
                print(f"    device contents changed since the dump was taken.")
                choice = input("    [t] truncate to 512-byte boundary and re-verify, "
                               "[y] delete and start fresh, [N] abort: ").strip().lower()
                if choice == "t":
                    trunc_to = file_size & ~511
                    if trunc_to == 0:
                        print(f"    File too small to truncate ({file_size} bytes); no salvage possible.")
                        continue
                    os.truncate(OUT, trunc_to)
                    file_size = trunc_to
                    print(f"    Truncated to {file_size} bytes; re-verifying...")
                    fail_kind, fail_off = verify_file(file_size)
                elif choice == "y":
                    os.remove(OUT)
                    file_size = 0
                    fail_kind = None
                    print(f"    File deleted; starting fresh.")
                else:
                    print(f"    Aborted; no changes made to {OUT}.")
                    ser.close()
                    return 1
            if file_size > 0:
                START = base + file_size
                append_mode = True
                resume_baseline = file_size
                print(f"\n*** VERIFICATION PASSED (head + 5 spots + tail) ***")
                print(f"    Existing file: {OUT} ({file_size // 1024} KB)")
                print(f"    Resuming from: 0x{START:x}")
                print(f"    Remaining:     {(max(0, END - START)) // 1024} KB")
            else:
                append_mode = False

    with open(OUT, "ab" if append_mode else "wb") as out:
        addr = START
        block_num = 0
        interrupted = False
        exit_code = None
        try:
            while addr < END:
                n_lines = min(LINES_PER_CMD, (END - addr + 15) // 16)
                remaining = END - addr
                cmd = f"dw {addr:x} {n_lines}"
                if VERBOSE:
                    print(f"\n=== Block {block_num}: {addr:#010x} for {n*16} bytes ===")
                for attempt in range(3):
                    try:
                        if not VERBOSE:
                            read_until_prompt(ser)
                        send_cmd(ser, cmd)
                        text = read_until_prompt(ser)
                        if VERBOSE:
                            print(f"Raw response snippet:\n{text[:500]}...")
                        blob = parse_dump(text, addr, n_lines * 16)
                        if remaining < len(blob):
                            blob = blob[:remaining]
                        break
                    except ValueError as e:
                        msg = f"retry {attempt+1}: {str(e)[:20]}"
                        if VERBOSE:
                            print(f"RETRY {attempt+1}/3 at {addr:#x}: {e}")
                        else:
                            draw_progress(block_num, addr, START, END, t_start, msg)
                        last_err = e
                else:
                    print(f"\nFAILED at {addr:#x} after 3 retries; aborting ({last_err})")
                    return 1
                out.write(blob)
                out.flush()
                addr += n_lines * 16
                block_num += 1
                if VERBOSE:
                    print(f"BLOCK OK: wrote {len(blob)} bytes to {OUT}")
                    done = (addr - START) / (END - START)
                    print(f"Progress: {addr:#010x}  {done:6.1%}")
                else:
                    draw_progress(block_num, addr, START, END, t_start)
        except KeyboardInterrupt:
            interrupted = True
            exit_code = 130
            exit_reason = "interrupted by user (Ctrl-C)"
        except (serial.SerialException, OSError) as e:
            interrupted = True
            exit_code = 2
            exit_reason = f"serial link lost ({type(e).__name__}: {e})"
        except Exception as e:
            interrupted = True
            exit_code = 3
            exit_reason = f"unexpected error ({type(e).__name__}: {e})"

    if interrupted:
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass
        try:
            size = os.path.getsize(OUT)
        except OSError:
            size = 0
        session_saved = size - resume_baseline
        print(f"\n{'='*60}")
        print(f"EXTRACTION ABORTED: {exit_reason}")
        print(f"{'='*60}")
        print(f"Partial file:    {OUT}")
        print(f"Session bytes:   {session_saved} ({session_saved // 1024} KB)")
        print(f"Total in file:  {size} ({size // 1024} KB)")
        if size:
            print(f"Stopped at:      0x{START + session_saved:x}")
        print(f"Blocks written: {block_num}")
        print(f"Resume with:    python3 odu_dump.py {PORT} {BAUD} {OUT}")
        print(f"{'='*60}")
        return exit_code

    if not VERBOSE:
        print()

    elapsed = time.time() - t_start
    elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    with open(OUT, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            sha256.update(chunk)
            md5.update(chunk)

    print(f"\n{'='*60}")
    print(f"EXTRACTION COMPLETE")
    print(f"{'='*60}")
    print(f"Total time:     {elapsed_str} ({elapsed:.1f} seconds)")
    print(f"Blocks written: {block_num}")
    print(f"Bytes written:  {addr-START}")
    print(f"Final file size: {os.path.getsize(OUT)} bytes")
    print(f"SHA256: {sha256.hexdigest()}")
    print(f"MD5:    {md5.hexdigest()}")
    print(f"Output: {OUT}")
    print(f"{'='*60}")

    return 0

sys.exit(main())
