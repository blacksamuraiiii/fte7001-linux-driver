#!/usr/bin/env python3
"""实验A: 验证 ReturnAutoPower 重挂 — 同 session 连续采图是否可行
无手指，看数据结构 + MCU 是否存活。若 capture 2/3 仍是~31 unique(非全零) => 重挂有效
"""
import ctypes, fcntl, struct, os, time, subprocess

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000
CAPTURE_SIZE = 7752

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "产物-实验A重挂-" + time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(OUT, exist_ok=True)
def log(s=""): print(s, flush=True); open(os.path.join(OUT,"输出.txt"),"a").write(s+"\n")
def hx(b): return " ".join("%02X"%v for v in b)

fd = os.open("/dev/spidev0.0", os.O_RDWR)
fcntl.ioctl(fd, _IOC(1,MAGIC,1,1), struct.pack("<B",0))
fcntl.ioctl(fd, _IOC(1,MAGIC,3,1), struct.pack("<B",8))
fcntl.ioctl(fd, _IOC(1,MAGIC,4,4), struct.pack("<I",SPEED))

def xf(tx, rl, pad=0):
    txb=bytes(tx); m=rl
    if pad and len(txb)<pad: txb=txb+b"\x00"*(pad-len(txb)); m=pad
    b=ctypes.create_string_buffer(txb,len(txb))
    rr=ctypes.create_string_buffer(m)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b),ctypes.addressof(rr),m,SPEED,0,8,0,0,0,0,0))
    return bytes(rr.raw[:rl])

def mcu():
    r=xf([0x10,0xEF,0x20,0x00,0x00,0x00],6)
    return r[4:6]==b"\xa5\x5a", hx(r)

def rd(reg, n=6):
    return xf([0x10,0xEF,reg,0x00,0x00,0x00], n)

def capture():
    """0x30门闸 → 0x1D → 04FB (零间隙)"""
    r30 = rd(0x30, 6)
    r1d5 = xf([0x10,0xEF,0x1D,0x00,0x00], 5)
    cmd = bytes([0x04,0xFB,0x34,0x00,0x1E,0x48,0x00])
    txb = cmd + b"\x00"*(CAPTURE_SIZE-len(cmd))
    b = ctypes.create_string_buffer(txb,len(txb))
    rr = ctypes.create_string_buffer(CAPTURE_SIZE)
    t0=time.time()
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b),ctypes.addressof(rr),CAPTURE_SIZE,SPEED,0,8,0,0,0,0,0))
    dt=time.time()-t0
    data=bytes(rr.raw[:CAPTURE_SIZE])
    return data, dt, r30[4], r1d5[4]

def return_autopower():
    """Windows 采图后重挂序列: 10 EF 20 → 11 EE 54 01 00 → 10 EF 20"""
    xf([0x10,0xEF,0x20,0x00,0x00,0x00], 6)
    xf([0x11,0xEE,0x54,0x01,0x00], 5)
    time.sleep(0.002)
    r = xf([0x10,0xEF,0x20,0x00,0x00,0x00], 6)
    return r

log("### 实验A: ReturnAutoPower 重挂验证")
log("uptime=%ds" % float(open("/proc/uptime").read().split()[0]))

# Wake
log("\n=== Wake ===")
subprocess.run(["gpioset","-c","gpiochip0","-t","20ms,0","85=0"],capture_output=True)
time.sleep(0.05)
for i in (1,2): xf([0x70],1,pad=4096); time.sleep(0.006)
time.sleep(0.01)
a,s = mcu()
log("MCU: %s alive=%s" % (s, a))

# Config init
log("\n=== Config init ===")
for reg,val in [(0x01,0x01),(0x41,0x0F),(0x30,0xBB),(0x22,0x00),(0x23,0x0E)]:
    xf([0x11,0xEE,reg,val,0x00],5); time.sleep(0.002)
r = rd(0x30, 6)
log("0x30=0x%02X %s" % (r[4], "OK" if r[4]==0xBB else "FAIL"))

# ARM
log("\n=== ARM ===")
xf([0x11,0xEE,0x1F,0x01,0x00],5); time.sleep(0.002)
xf([0x11,0xEE,0x1E,0x01,0x00],5); time.sleep(0.010)
a,s = mcu()
log("MCU after ARM: %s alive=%s" % (s, a))

# 连续采图 3 次
log("\n=== 连续采图 x3 (ReturnAutoPower 重挂) ===")
for i in range(1, 4):
    data, dt, v30, v1d = capture()
    img = data[8:8+7744]
    uniq = len(set(img))
    nonzero = sum(1 for b in img if b != 0)
    seg_b_uniq = len(set(img[576:]))
    a, s = mcu()
    log("\n  capture #%d: %.1fms  头=%s" % (i, dt*1000, hx(data[:8])))
    log("    0x30=%02X 0x1D[4]=%02X  raw: unique=%d nonzero=%d/7744  段B unique=%d" % (
        v30, v1d, uniq, nonzero, seg_b_uniq))
    log("    MCU after: %s alive=%s" % (s, a))
    with open(os.path.join(OUT, "capture-%d.bin" % i), "wb") as f:
        f.write(data)
    # ReturnAutoPower 重挂 (第3次后不需要)
    if i < 3:
        log("    >> ReturnAutoPower: 10 EF 20 → 11 EE 54 01 00 → 10 EF 20")
        r = return_autopower()
        a2, s2 = mcu()
        log("    >> 重挂后 MCU: %s alive=%s" % (s2, a2))
        time.sleep(0.01)

# 判定
log("\n=== 判定 ===")
log("若 capture 2/3 的 unique 仍~31(非全零)且 MCU alive => ReturnAutoPower 重挂有效, 可连续采图")
log("若 capture 2/3 全零且 MCU dead => 重挂无效, 每幅需全新 session")

os.close(fd)
log("\n产物: %s" % OUT)