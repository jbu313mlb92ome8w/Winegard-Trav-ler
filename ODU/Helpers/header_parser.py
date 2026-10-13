import struct, sys, os

# Check argument count
if len(sys.argv) != 2:
    print("Usage: python3 header_parser.py DUMP_FILE")
    print("Example: python3 header_parser.py 1/odu_1_v1.23.45_full.bin")
    sys.exit(1)

# Check for .bin extension
if not sys.argv[1].lower().endswith(".bin"):
    print("Error: Input file must have a .bin extension")
    print("Usage: python3 header_parser.py DUMP_FILE")
    print("Example: python3 header_parser.py 1/odu_1_v1.23.45_full.bin")
    sys.exit(1)

# Check file exists
if not os.path.isfile(sys.argv[1]):
    print(f"Error: File not found: {sys.argv[1]}")
    sys.exit(1)

raw = open(sys.argv[1], "rb").read()

base = 0x10000
hdr = raw[base:base + 0x100]
print(f"Header region 0x{base:x}-0x{base+0x100:x}, {len(hdr)} bytes")
print(f"Magic check: {raw[base:base+4].hex()} (want dacbbcad LE)")
print()
for off in range(0, len(hdr), 4):
    word = struct.unpack_from("<I", hdr, off)[0]
    ascii_hint = "".join(chr(b) if 32 <= b < 127 else "." for b in hdr[off:off+4])
    print(f"  +0x{off:03x}  {word:08x}  {ascii_hint}")
print()
for off in range(0, len(hdr), 2):
    half = struct.unpack_from("<H", hdr, off)[0]
    if 0x10000 < half < 0x100000:
        print(f"  possible word-sized pointer at +0x{off:03x}: {half:#06x}")
