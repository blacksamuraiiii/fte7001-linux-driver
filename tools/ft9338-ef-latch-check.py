#!/usr/bin/env python3
"""0xEF 复现规律验证 (纯只读, 零风险): 90 00 00 连续多次 / 带 10EF20 前缀 / 中间穿插"""
import ctypes, fcntl, struct, os, time

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000

fd = os.open("/dev/spidev0.0", os.O_RDWR)
fcntl.ioctl(fd, _IOC(1, MAGIC, 1, 1), struct.pack("<B", 0))
fcntl.ioctl(fd, _IOC(1, MAGIC, 3, 1), struct.pack("<B", 8))
fcntl.ioctl(fd, _IOC(1, MAGIC, 4, 4), struct.pack("<I", SPEED))

def xf(tx, rl):
    txb = bytes(tx)
    b = ctypes.create_string_buffer(txb, len(txb))
    rr = ctypes.create_string_buffer(rl)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), ctypes.addressof(rr),
                                    rl, SPEED, 0, 8, 0, 0, 0, 0, 0))
    return bytes(rr.raw[:rl])

def hx(b):
    return " ".join("%02X" % v for v in b)

print("uptime = %.0f s" % float(open("/proc/uptime").read().split()[0]))
print()

print("--- A. 90 00 00 连续 3 次 (t=0 间隔 10ms) ---")
for i in range(3):
    r = xf([0x90, 0x00, 0x00], 3)
    print("  #%d  RX=%s  RX[2]=0x%02X" % (i + 1, hx(r), r[2]))
    time.sleep(0.01)

print()
print("--- B. 10 EF 20 (开窗口) 之后接 90 00 00 ---")
print("  10 EF 20 RX=%s" % hx(xf([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)))
r = xf([0x90, 0x00, 0x00], 3)
print("  90 00 00  RX=%s  RX[2]=0x%02X" % (hx(r), r[2]))

print()
print("--- C. 90 00 00 x2, 中间插 10 EF 16 ---")
r = xf([0x90, 0x00, 0x00], 3)
print("  #1 RX=%s  RX[2]=0x%02X" % (hx(r), r[2]))
r16 = xf([0x10, 0xEF, 0x16, 0x00, 0x00, 0x00], 6)
print("  10 EF 16 RX=%s" % hx(r16))
r = xf([0x90, 0x00, 0x00], 3)
print("  #2 RX=%s  RX[2]=0x%02X" % (hx(r), r[2]))

print()
print("--- D. 90 00 00 单独重发 x2 远离其他帧 ---")
time.sleep(0.5)
r = xf([0x90, 0x00, 0x00], 3)
print("  x1 RX=%s  RX[2]=0x%02X" % (hx(r), r[2]))
time.sleep(0.5)
r = xf([0x90, 0x00, 0x00], 3)
print("  x2 RX=%s  RX[2]=0x%02X" % (hx(r), r[2]))

os.close(fd)
print()
print("done")
