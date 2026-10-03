#!/usr/bin/env python3
"""FT9338 无手指基线: 0x30→0x1D→04FB 序列, 无ARM/无手指, 纯看数据结构"""
import ctypes, fcntl, struct, os, time, subprocess

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000
CAPTURE_SIZE = 7752

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "产物-无手指-" + time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(OUT, exist_ok=True)
def log(s=""): print(s, flush=True); open(os.path.join(OUT,"输出.txt"),"a").write(s+"\n")
def hx(b): return " ".join("%02X"%v for v in b)

fd = os.open("/dev/spidev0.0", os.O_RDWR)
fcntl.ioctl(fd, _IOC(1,MAGIC,1,1), struct.pack("<B",0))  # mode0
fcntl.ioctl(fd, _IOC(1,MAGIC,3,1), struct.pack("<B",8))  # bpw8
fcntl.ioctl(fd, _IOC(1,MAGIC,4,4), struct.pack("<I",SPEED))

def xf(tx, rl, pad=0):
    txb=bytes(tx)
    m=rl
    if pad and len(txb)<pad: txb=txb+b"\x00"*(pad-len(txb)); m=pad
    b=ctypes.create_string_buffer(txb,len(txb))
    rr=ctypes.create_string_buffer(m)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b),ctypes.addressof(rr),m,SPEED,0,8,0,0,0,0,0))
    return bytes(rr.raw[:rl])

log("### 无手指基线 ###")
log("uptime=%ds" % float(open("/proc/uptime").read().split()[0]))

# Wake
log("\n=== Wake ===")
subprocess.run(["gpioset","-c","gpiochip0","-t","20ms,0","85=0"],capture_output=True)
time.sleep(0.05)
for i in (1,2): xf([0x70],1,pad=4096); time.sleep(0.006)
time.sleep(0.01)
r=xf([0x10,0xEF,0x20,0x00,0x00,0x00],6)
log("MCU: %s alive=%s"%(hx(r),"YES" if r[4:6]==b"\xa5\x5a" else "NO"))

# Config init
log("\n=== Config init ===")
for reg,val in [(0x01,0x01),(0x41,0x0F),(0x30,0xBB),(0x22,0x00),(0x23,0x0E)]:
    xf([0x11,0xEE,reg,val,0x00],5); time.sleep(0.002)
r=xf([0x10,0xEF,0x30,0x00,0x00,0x00],6)
v30=r[4]
log("0x30=0x%02X %s"%(v30,"OK" if v30==0xBB else "FAIL"))

# ARM
log("\n=== ARM ===")
xf([0x11,0xEE,0x1F,0x01,0x00],5); time.sleep(0.002)
xf([0x11,0xEE,0x1E,0x01,0x00],5); time.sleep(0.010)

# 读0x1D状态(不检测手指，纯看当前值)
log("\n=== 当前状态 ===")
r5=xf([0x10,0xEF,0x1D,0x00,0x00],5)
r16=xf([0x10,0xEF,0x1D,0x00,0x00,0x00],16)
log("0x1D 5B: %s  RX[4]=0x%02X"%(hx(r5),r5[4]))
log("0x1D 16B: %s  RX[2:3]=%s"%(hx(r16)," ".join("%02X"%v for v in r16[2:4])))

# 关键段: 0x30→0x1D→04FB
log("\n=== 关键段 0x30→0x1D→04FB ===")
r30p=xf([0x10,0xEF,0x30,0x00,0x00,0x00],6)
r1d5p=xf([0x10,0xEF,0x1D,0x00,0x00],5)
r1d16p=xf([0x10,0xEF,0x1D,0x00,0x00,0x00],16)

cmd=bytes([0x04,0xFB,0x34,0x00,0x1E,0x48,0x00])
txb=cmd+b"\x00"*(CAPTURE_SIZE-len(cmd))
b=ctypes.create_string_buffer(txb,len(txb))
rr=ctypes.create_string_buffer(CAPTURE_SIZE)
t0=time.time()
fcntl.ioctl(fd,MSG(1),TR.pack(ctypes.addressof(b),ctypes.addressof(rr),CAPTURE_SIZE,SPEED,0,8,0,0,0,0,0))
dt=time.time()-t0
data=bytes(rr.raw[:CAPTURE_SIZE])

log("0x30=0x%02X  0x1D5B=0x%02X  0x1D16B=%s"%(r30p[4],r1d5p[4],
    " ".join("%02X"%v for v in r1d16p[2:4])))
log("大读: %.1fms  %dB"%(dt*1000,len(data)))
log("头8B: %s"%hx(data[:8]))

# 统计
img=data[8:8+7744]
uniq=len(set(img))
vals=sorted(set(img))
log("\n=== 7744B 数据统计 ===")
log("unique=%d  range=%d-%d"%(uniq,min(img),max(img)))
log("vals[:30]: %s"%" ".join("0x%02X"%v for v in vals[:30]))
log("vals[-30:]: %s"%" ".join("0x%02X"%v for v in vals[-30:]))

# 段统计
sa=img[:576]; sb=img[576:]
log("段A(0-575B): unique=%d" % len(set(sa)))
log("段B(576-7743B): unique=%d  vals[:20]=%s" % (len(set(sb)),
    " ".join("0x%02X"%v for v in sorted(set(sb))[:20])))

# 保存
with open(os.path.join(OUT,"capture-raw.bin"),"wb") as f: f.write(data)
inv=bytes(~b&0xFF for b in img)
with open(os.path.join(OUT,"capture-inv.pgm"),"wb") as f: f.write(b"P5\n88 88\n255\n"+inv)

# MCU after
r20=xf([0x10,0xEF,0x20,0x00,0x00,0x00],6)
log("MCU after: %s alive=%s"%(hx(r20),"YES" if r20[4:6]==b"\xa5\x5a" else "NO"))

os.close(fd)
log("\n产物: %s"%OUT)