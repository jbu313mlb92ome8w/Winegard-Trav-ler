import serial, sys, struct, time, os, hashlib, re

def print_help():
    help_text = """
ODU Flash Extraction Tool
=========================
Extracts MK64FN1M0VLK12 flash memory contents via the ODU console 'dw' command.

USAGE:
    python3 odu_dump.py PORT BAUD OUTPUT [START] [END] [FLAGS]

PARAMETERS:
    PORT      Serial device path          (e.g. /dev/ttyUSB0)
    BAUD      Baud rate                    (e.g. 57600)
    OUTPUT    Output binary file path, or 'auto' (see AUTO FILE NAMING)
              Parent directories are created automatically if missing.
    START     Optional start address       (hex 0x... or decimal, default 0x0)
    END       Optional end address         (hex 0x... or decimal, default from 'id' command)
    FLAGS     Optional flags: -a/--app-only, -v/--verbose, -h/--help

AUTO FILE NAMING:
    Passing OUTPUT as 'auto' (with optional directory prefix) instructs
    the script to query the ODU 'header' command first and derive the
    filename from its Software Version field, producing a file like:
        odu_v1.23.45.bin
    With -a/--app-only flag:
        odu_v1.23.45_app.bin
    Auto naming works with directory paths:
        dumps/auto   ->   dumps/odu_v1.23.45.bin
        /tmp/backup/auto  ->  /tmp/backup/odu_v1.23.45.bin
    The full header and id blocks are printed before the dump begins.
    Resume logic applies: if the file exists, it is appended from the
    file-size boundary (512-byte aligned).

MEMORY MAP:
    Dynamically extracted from the ODU 'id' command output. The
    Application Base, Bootloader Base, and sizes determine the
    default ranges if -a/--app-only is not specified. This ensures
    compatibility across firmware revisions with different partition
    boundaries.

OPTIONS:
    -a, --app-only    Extract only the application region using addresses
                      parsed from the 'id' command. File suffix '_app'
                      is added when OUTPUT=auto.
    -v, --verbose     Enable full per-line terminal output (debug,
                      per-block messages, raw response snippets).
                      Default is quiet mode: static header/ID panels
                      with a single-line progress bar and ETA.
    -h, --help        Show this help message and exit

EXAMPLES:
    Full extraction with automatic filename with _full suffix (uses 'id' command):
        python3 odu_dump.py /dev/ttyUSB0 57600 auto

    App-only extraction (automatic filename with _app suffix):
        python3 odu_dump.py /dev/ttyUSB0 57600 auto -a

    Full extraction with explicit filename:
        python3 odu_dump.py /dev/ttyUSB0 57600 odu_full.bin

    Auto naming with directory creation:
        python3 odu_dump.py /dev/ttyUSB0 57600 dumps/auto

    Explicit filename with directory:
        python3 odu_dump.py /dev/ttyUSB0 57600 /data/odubackups/dump.bin

    Resume interrupted dump (auto-detected from file size and flag):
        python3 odu_dump.py /dev/ttyUSB0 57600 odu_full.bin

    Custom explicit range (overriding 'id' parsing):
        python3 odu_dump.py /dev/ttyUSB0 57600 custom.bin 0x10000 0x10001

NOTES:
    * The ODU 'header' and 'id' commands are issued at startup and
      their fields are printed for reference.
    * Addresses sent to ODU use bare hex (no 0x prefix) per console
      parser requirement; the ODU silently falls back to address 0
      when given '0x...' format.
    * Device TX buffer caps at 41 lines; script uses 32 lines
      (512-byte blocks) for safe margin.
    * Existing output file is auto-detected and appended to
      (resume). Delete the file to start fresh.
    * All commands are read-only ('header', 'id', and 'dw'); no flash
      write risk.
    * Interrupted runs leave a valid prefix; realigns to the
      nearest 512-byte boundary on resume. Ctrl-C or serial disconnect
      preserves partial data with summary and resume command hint.
    * SHA256 and MD5 of the completed file are printed at the end,
      along with total elapsed time, enabling cross-device comparisons.
    * Parent directories in OUTPUT paths are created automatically.

DEFAULT RANGE: Full flash from 0x0 to module default 0x100000.
               Override with explicit START END arguments.
"""
    print(help_text)

PORT = None
BAUD = None
OUT = None
START = 0x0
END = 0x100000
LINES_PER_CMD = 32
VERBOSE = False
APP_ONLY = False

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
        if buf.endswith(b"ODU>") or buf.rstrip().endswith(b"ODU>"):
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
        if buf.rstrip().endswith(b"ODU>"):
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
            "NVS Address", "NVS Size")

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
        if buf.rstrip().endswith(b"ODU>"):
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
        if buf.rstrip().endswith(b"ODU>"):
            break
        if not chunk:
            time.sleep(0.02)
    halves = []
    for line in buf.decode("ascii", "replace").splitlines():
        line = line.strip()
        if line and line[0].isdigit() and ":" in line:
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
    global PORT, BAUD, OUT, START, END, APP_ONLY

    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print_help()
        return 0
    if len(sys.argv) < 4:
        print(f"Error: missing required arguments (need PORT BAUD OUTPUT)")
        print_help()
        return 1

    global VERBOSE
    VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv
    APP_ONLY = "-a" in sys.argv or "--app-only" in sys.argv
    FLAGS = ("-a", "--app-only", "-h", "--help", "-v", "--verbose")
    clean_args = [a for a in sys.argv if a not in FLAGS]

    # Remove flags from args list for positional parsing
    clean_args = [a for a in sys.argv if a not in ("-a", "--app-only", "-h", "--help")]
    if len(clean_args) < 4:
        print(f"Error: missing required arguments (need PORT BAUD OUTPUT)")
        print_help()
        return 1

    PORT = clean_args[1]
    BAUD = int(clean_args[2])
    OUT = clean_args[3]
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
    hdr, hdr_text = fetch_header(ser)
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
    if OUT == "auto":
        ver = hdr.get("Software Version")
        if not ver:
            ver = id_fields.get("App Version", "unknown")
        if ver:
            safe = "".join(c for c in str(ver) if c.isalnum() or c in ".")
            if APP_ONLY:
                OUT = f"odu_v{safe}_app.bin"
            else:
                OUT = f"odu_v{safe}_full.bin"
            print(f"\nOutput filename derived from version: {OUT}")
        else:
            OUT = "odu_unknown.bin"
            print(f"\nOutput filename: {OUT}")

    if START >= END:
        print(f"Start address 0x{START:x} >= end 0x{END:x}; nothing to do.")
        return 0

    print(f"\nRange: 0x{START:x} - 0x{END:x} ({(END-START)//1024} KB)")

    t_start = time.time()

    # Check resume capability
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
        append_mode = False

    with open(OUT, "ab" if append_mode else "wb") as out:
        addr = START
        block_num = 0
        interrupted = False
        exit_code = None
        try:
            while addr < END:
                n = min(LINES_PER_CMD, (END - addr + 15) // 16)
                cmd = f"dw {addr:x} {n}"
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
                        blob = parse_dump(text, addr, n * 16)
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
                addr += n * 16
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
            size = os.path.getsize(OUT)
        except OSError:
            size = 0
        if size % 512 != 0:
            try:
                os.truncate(OUT, size & ~511)
                size = size & ~511
                print(f"\nTrimmed torn final block; file realigned to {size} bytes.")
            except OSError:
                pass
        try:
            ser.close()
        except (serial.SerialException, OSError):
            pass
        print(f"\n{'='*60}")
        print(f"EXTRACTION ABORTED: {exit_reason}")
        print(f"{'='*60}")
        print(f"Partial file:    {OUT}")
        print(f"Bytes saved:     {size} ({size // 1024} KB)")
        if size:
            print(f"Stopped at:      0x{START + size:x}")
        print(f"Blocks written: {block_num}")
        print(f"Resume with:    python3 odu_dump.py {PORT} {BAUD} {OUT} "
              f"{hex(START)} {hex(END)}")
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
