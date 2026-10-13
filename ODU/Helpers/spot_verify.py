import serial, sys, struct, random, time

if len(sys.argv) < 4:
    print("Usage: python3 spot_verify.py PORT BAUD DUMP_FILE")
    print("Example: python3 spot_verify.py /dev/ttyUSB0 57600 odu_v1.23.45.bin")
    sys.exit(1)
PORT, BAUD, DUMP = sys.argv[1], int(sys.argv[2]), sys.argv[3]
raw = open(DUMP, "rb").read()
#random.seed(1234)
random.seed(int(time.time() * 1000))

ser = serial.Serial(PORT, BAUD, timeout=0.2)
time.sleep(0.3)
ser.reset_input_buffer()

def dw(addr, lines):
    ser.write(f"dw {addr:x} {lines}\r\n".encode())
    time.sleep(0.4)
    buf = b""
    t0 = time.time()
    while time.time() - t0 < 3:
        c = ser.read(4096)
        if c:
            buf += c
        if buf.rstrip().endswith(b"ODU>"):
            break
    out = bytearray()
    for line in buf.decode("ascii", "replace").splitlines():
        line = line.strip()
        if line and line[0].isdigit() and ":" in line:
            fields = line.partition(":")[2].split()
            if len(fields) == 8:
                for f in fields:
                    out += struct.pack("<H", int(f, 16))
    return bytes(out)

for trial in range(5):
    addr = random.randrange(0x10100, 0x80400, 16)
    n = 32
    dev = dw(addr, n)
    disk = raw[addr:addr + n * 16]
    status = "MATCH" if dev == disk else "MISMATCH"
    print(f"{addr:#08x} [{len(dev)} vs {len(disk)} bytes]: {status}")
    if status == "MISMATCH":
        print(f"  device: {dev[:32].hex()}")
        print(f"  dump:   {disk[:32].hex()}")
