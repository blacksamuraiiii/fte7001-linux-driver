#!/usr/bin/env python3
"""Cold-boot full sequence v5 — ★加入 05FA 后的芯片重启序列（反汇编 0x001665-0x0016AB 证实）
Windows DownLoadFirewareInternal 在 04FB 校验通过后:
  Sleep(2) → rst脉冲#1(low 7ms→high) → Sleep(10) → rst脉冲#2(low 7ms→high)
  → chip_type==1(FT9338): Sleep(180) → 10EF20 探活 → 10EF14/15 → config → ARM
"""
import ctypes, fcntl, struct, os, time

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1_000_000
FW_PATH = os.environ.get("FT9338_FW", "ft9338-firmware.bin")
hx = lambda b: " ".join("%02X" % v for v in b)

fd = os.open("/dev/spidev0.0", os.O_RDWR)
fcntl.ioctl(fd, _IOC(1, MAGIC, 1, 1), struct.pack("<B", 0))
fcntl.ioctl(fd, _IOC(1, MAGIC, 3, 1), struct.pack("<B", 8))
fcntl.ioctl(fd, _IOC(1, MAGIC, 4, 4), struct.pack("<I", SPEED))

def wr_only(buf, speed=SPEED):
    tx = bytes(buf)
    b = ctypes.create_string_buffer(tx, len(tx))
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), 0, len(tx), speed, 0, 8, 0, 0, 0, 0, 0))

def xfer(tx, rlen, speed=SPEED):
    tx = bytes(tx)
    b = ctypes.create_string_buffer(tx, len(tx))
    rr = ctypes.create_string_buffer(rlen)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), ctypes.addressof(rr), rlen, speed, 0, 8, 0, 0, 0, 0, 0))
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
g = Req(); g.lo[0] = 85; g.flags = OUT; g.dv[0] = 1; g.cl = b"v5"; g.lines = 1
fcntl.ioctl(chipfd, LGET, g); gpio_fd = g.fd
def rst(v):
    d = Data(); d.val[0] = v; fcntl.ioctl(gpio_fd, LSET, d)
def rst_pulse(ms=7):
    """fn 0x29A0: rst(0) → Sleep(7ms) → rst(1)"""
    rst(0); time.sleep(ms / 1000); rst(1)
def cold_reset():
    rst(0); time.sleep(0.010); rst(1)

print("[0] 前置: 状态探活")
r = xfer([0x10, 0xEF, 0x20, 0, 0, 0], 6)
r_acut = xfer([0x90, 0, 0], 3)
print("    10EF20=%s  90 00 00=%s" % (hx(r), hx(r_acut)))

# ---- FE 检查 5 轮（每轮 rst10ms+55AA，P9 时序）----
print("[1] FE 检查 x5")
fw_present = False
for i in range(5):
    cold_reset()
    wr_only([0x55, 0xAA]); time.sleep(0.001)
    r_cb = xfer([0x08, 0xF7, 0xCB, 0x00], 4)
    if r_cb[3] != 0:
        xfer([0x09, 0xF6, 0xCB, r_cb[3] | 0x20], 4)
        xfer([0x09, 0xF6, 0xFD, 0x11], 4)
        xfer([0x09, 0xF6, 0xFE, 0x11], 4)
        r_fe = xfer([0x08, 0xF7, 0xFE, 0x00], 4)
        echo = (r_fe[1] == 0x08 and r_fe[2] == 0xF7)
        print("    轮%d CB=%02X FE-rd=%s %s" % (i + 1, r_cb[3], hx(r_fe), "(回显)" if echo else "FE=0x%02X" % r_fe[3]))
        if not echo and r_fe[3]:
            fw_present = True; break
        time.sleep(0.010)
print("    判: %s" % ("有固件→暖路径" if fw_present else "无固件→下载"))

# ---- IC_EnterDownloadMode 复刻（0x1CE4 else 分支 ≤3 轮）----
print("[2] IC_EnterDownloadMode（≤3 轮: rst10 + 55AA + C2写读）")
ok_dl = False
for i in range(3):
    cold_reset()
    wr_only([0x55, 0xAA]); time.sleep(0.001)
    xfer([0x09, 0xF6, 0xC2, 0x55], 4)
    r = xfer([0x08, 0xF7, 0xC2, 0x00], 4)
    print("    轮%d 08F7 C2 -> %s (rx[3]=0x%02X)" % (i + 1, hx(r), r[3]))
    if r[3] == 0x55:
        ok_dl = True; break
    time.sleep(0.010)
print("    IC_EnterDownloadMode: %s" % ("✓ (C2==0x55)" if ok_dl else "✗"))

# ---- 下载模式 5 写 ----
print("[3] 09F6 C8/CA/CB/B9x2")
for reg, val in [(0xC8, 0xFF), (0xCA, 0xFF), (0xCB, 0xFF), (0xB9, 0xBF), (0xB9, 0xFF)]:
    xfer([0x09, 0xF6, reg, val], 4)

# ---- 05FA 块写 + 04FB 校验 ----
blob = open(FW_PATH, "rb").read()
frame = bytes([0x05, 0xFA, 0, 0, 0x37, 0x38]) + blob + b"\x00"
print("[4] 05FA 块写 14143B …", end=" ")
t0 = time.time(); wr_only(frame)
print("%.0f ms" % ((time.time() - t0) * 1000))
time.sleep(0.5)
print("[5] 04FB 回读校验 …", end=" ")
rb = xfer([0x04, 0xFB, 0, 0, 0x37, 0x3A, 0x00], 14144)
off = rb.find(blob[:64])
full = off >= 0 and rb[off:off + 14136] == blob
print("blob偏移=%s 全量匹配=%s" % (off, full))

# ---- ★ 反启动序列（0x001665 证实, 此前 Linux 全缺）----
print("[6] ★ 芯片重启序列: Sleep(2) → rst#1(7ms) → Sleep(10) → rst#2(7ms) → Sleep(180)")
time.sleep(0.002)
rst_pulse(7)
time.sleep(0.010)
rst_pulse(7)
time.sleep(0.180)

# ---- 探活循环（fn 0x2900, ≤4 轮, 间隔 5ms）----
print("[7] 10EF20 探活循环（≤4 轮）")
alive = False
for i in range(4):
    r = xfer([0x10, 0xEF, 0x20, 0, 0, 0], 6)
    print("    try%d: %s" % (i + 1, hx(r)))
    if r[4:6] == bytes([0xA5, 0x5A]):
        alive = True; break
    time.sleep(0.005)

if not alive:
    # 扩大等待再试
    for i in range(6):
        time.sleep(0.5)
        r = xfer([0x10, 0xEF, 0x20, 0, 0, 0], 6)
        r14 = xfer([0x10, 0xEF, 0x14, 0, 0, 0], 6)
        print("    延迟try%d: 10EF20=%s 10EF14=%s" % (i + 1, hx(r), hx(r14)))
        if r[4:6] == bytes([0xA5, 0x5A]) or r14[4] == 0x58:
            alive = True; break

r14 = xfer([0x10, 0xEF, 0x14, 0, 0, 0], 6)
r15 = xfer([0x10, 0xEF, 0x15, 0, 0, 0], 6)
print("[8] 10EF14=%s  10EF15=%s" % (hx(r14), hx(r15)))

if alive or r14[4] == 0x58:
    print("[9] config init 11EE0101 → 30BB → 1F/1E ARM")
    wr_only([0x11, 0xEE, 0x01, 0x01, 0x00]); time.sleep(0.002)
    wr_only([0x11, 0xEE, 0x30, 0xBB, 0x00]); time.sleep(0.002)
    r30 = xfer([0x10, 0xEF, 0x30, 0, 0, 0], 6)
    print("    10EF30 -> %s (rx[4]=0x%02X)" % (hx(r30), r30[4]))
    wr_only([0x11, 0xEE, 0x1F, 0x01, 0x00]); time.sleep(0.005)
    wr_only([0x11, 0xEE, 0x1E, 0x01, 0x00]); time.sleep(0.005)
    r20 = xfer([0x10, 0xEF, 0x20, 0, 0, 0], 6)
    print("    ARM后 10EF20 -> %s" % hx(r20))
    if r20[4:6] == bytes([0xA5, 0x5A]) or r20[4] == 0xA5:
        print("=" * 60)
        print("★★★ 工作态达成（A5 5A）— FT9338 冷启动全程 Linux 复刻成功！")
        print("=" * 60)
    else:
        print("    仍未到 A5 5A")
else:
    print("    固件未启动")

os.close(fd); os.close(chipfd); os.close(gpio_fd)
print("done")
