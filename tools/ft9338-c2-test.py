#!/usr/bin/env python3
"""C2=0x02 卡点变体实验
v3 教训: 55AA#6 无 GPIO 复位 → C2 读回 00 00 00 55（写入值）
假设: Windows 的 C2 交换走 IC_GetRegFile(带 reset→55AA), 或需 FE 轮先行
VA: cold_reset → 55AA → 09F6 C2 55 → 08F7 C2      (新鲜复位后直接 C2)
VB: cold_reset → 55AA → FE一轮 → 09F6 C2 55 → 08F7 C2 (FE 轮后不重复位)
VC: cold_reset → 55AA → 09F6 C2 55 → 读C2×3          (首读 vs 重读)
每个变体独立隔离（变体间 cold_reset 恢复 bootloader 会话）
"""
import ctypes, fcntl, struct, os, time

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1_000_000
hx = lambda b: " ".join("%02X" % v for v in b)

fd = os.open("/dev/spidev0.0", os.O_RDWR)
fcntl.ioctl(fd, _IOC(1, MAGIC, 1, 1), struct.pack("<B", 0))
fcntl.ioctl(fd, _IOC(1, MAGIC, 3, 1), struct.pack("<B", 8))
fcntl.ioctl(fd, _IOC(1, MAGIC, 4, 4), struct.pack("<I", SPEED))

def wr_only(buf):
    tx = bytes(buf)
    b = ctypes.create_string_buffer(tx, len(tx))
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), 0, len(tx), SPEED, 0, 8, 0, 0, 0, 0, 0))

def xfer(tx, rlen):
    tx = bytes(tx)
    b = ctypes.create_string_buffer(tx, len(tx))
    rr = ctypes.create_string_buffer(rlen)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), ctypes.addressof(rr), rlen, SPEED, 0, 8, 0, 0, 0, 0, 0))
    return bytes(rr.raw[:rlen])

class Req(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_uint32 * 64), ("flags", ctypes.c_uint32),
                ("dv", ctypes.c_uint8 * 64), ("cl", ctypes.c_char * 32),
                ("lines", ctypes.c_uint32), ("fd", ctypes.c_int)]
class Data(ctypes.Structure):
    _fields_ = [("val", ctypes.c_uint8 * 64)]
OUT = 1 << 1
LGET = (3 << 30) | (0xB4 << 8) | 3 | (ctypes.sizeof(Req) << 16)
LSET = (3 << 30) | (0xB4 << 8) | 9 | (ctypes.sizeof(Data) << 16)
chipfd = os.open("/dev/gpiochip0", os.O_RDWR)
g = Req(); g.lo[0] = 85; g.flags = OUT; g.dv[0] = 1; g.cl = b"c2v"; g.lines = 1
fcntl.ioctl(chipfd, LGET, g); gpio_fd = g.fd
def rst(v):
    d = Data(); d.val[0] = v; fcntl.ioctl(gpio_fd, LSET, d)
def cold_reset():
    rst(0); time.sleep(0.010); rst(1)

def c2_exchange():
    r = xfer([0x09, 0xF6, 0xC2, 0x55], 4)
    time.sleep(0.001)
    r2 = xfer([0x08, 0xF7, 0xC2, 0x00], 4)
    return r, r2

def fe_one_round():
    r_cb = xfer([0x08, 0xF7, 0xCB, 0x00], 4)
    xfer([0x09, 0xF6, 0xCB, 0x20], 4)
    xfer([0x09, 0xF6, 0xFD, 0x11], 4)
    xfer([0x09, 0xF6, 0xFE, 0x11], 4)
    r_fe = xfer([0x08, 0xF7, 0xFE, 0x00], 4)
    return r_cb, r_fe

print("=== VA: cold_reset → 55AA → C2 ===")
cold_reset()
wr_only([0x55, 0xAA]); time.sleep(0.001)
rw, r2 = c2_exchange()
print("  09F6 C2 55 -> %s" % hx(rw))
print("  08F7 C2    -> %s   rx[0]=0x%02X" % (hx(r2), r2[0]))

print("=== VB: cold_reset → 55AA → FE一轮 → C2（不重复位）===")
cold_reset()
wr_only([0x55, 0xAA]); time.sleep(0.001)
r_cb, r_fe = fe_one_round()
print("  FE轮: CB=%s FE=%s" % (hx(r_cb), hx(r_fe)))
rw, r2 = c2_exchange()
print("  09F6 C2 55 -> %s" % hx(rw))
print("  08F7 C2    -> %s   rx[0]=0x%02X" % (hx(r2), r2[0]))

print("=== VC: cold_reset → 55AA → C2 → 读×3 ===")
cold_reset()
wr_only([0x55, 0xAA]); time.sleep(0.001)
r = xfer([0x09, 0xF6, 0xC2, 0x55], 4)
print("  09F6 C2 55 -> %s" % hx(r))
for i in range(3):
    r2 = xfer([0x08, 0xF7, 0xC2, 0x00], 4)
    print("  读#%d: %s   rx[0]=0x%02X rx[3]=0x%02X" % (i + 1, hx(r2), r2[0], r2[3]))
    time.sleep(0.002)

print("=== VD: 09F6 C2 写其他值(0x02) → 读 ===")
cold_reset()
wr_only([0x55, 0xAA]); time.sleep(0.001)
r = xfer([0x09, 0xF6, 0xC2, 0x02], 4)
time.sleep(0.001)
r2 = xfer([0x08, 0xF7, 0xC2, 0x00], 4)
print("  写0x02后读: %s" % hx(r2))

os.close(fd); os.close(chipfd); os.close(gpio_fd)
print("done")
