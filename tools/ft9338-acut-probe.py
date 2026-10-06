#!/usr/bin/env python3
"""FT9338 read-only state probes (2026-10-06)
严格只读: 不 GPIO85 复位 / 不发 0x70 / 不写任何配置帧。
帧 = Windows 侧给的原始指令卡, 全双工, 记录 RX 全字节 (不预设判据位置)。
实测结论 (2026-10-06): 90 00 00 首次回 RX[2]=0xEF 是 A-CUT boot 一次性 latch,
重读回到回显态; 08F7 短读在 boot 态是命令回显 (RX[n]=TX[n-1]), 不能判固件有无。
"""
import ctypes, fcntl, struct, os, time

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000

fd = os.open("/dev/spidev0.0", os.O_RDWR)
fcntl.ioctl(fd, _IOC(1, MAGIC, 1, 1), struct.pack("<B", 0))  # mode0
fcntl.ioctl(fd, _IOC(1, MAGIC, 3, 1), struct.pack("<B", 8))  # bpw8
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
print("uname  =", os.uname().nodename)
print()

# 探针1: 90 00 00 (3B 全双工) — A-CUT boot 判定
r1 = xf([0x90, 0x00, 0x00], 3)
print("[1] 90 00 00  (3B full-duplex)")
print("    RX = %s   RX[2]=0x%02X" % (hx(r1), r1[2]))
print("    判据: RX[2]==0xEF => A-CUT boot (正常 S5 后态, 可救); 0x00 => 需另查")
print()

# 探针2: 08 F7 FE 00 (4B) — 芯片内固件判定
r2 = xf([0x08, 0xF7, 0xFE, 0x00], 4)
print("[2] 08 F7 FE 00 (4B)")
print("    RX = %s   RX[3]=0x%02X" % (hx(r2), r2[3]))
print("    判据: RX[3]!=0 => 芯片内有固件; ==0 => 需下载")
print()

# 探针3: 08 F7 C2 00 (4B) — 型号应答判定
r3 = xf([0x08, 0xF7, 0xC2, 0x00], 4)
print("[3] 08 F7 C2 00 (4B)")
print("    RX = %s   RX[0]=0x%02X  RX[3]=0x%02X" % (hx(r3), r3[0], r3[3]))
print("    行动单判据: RX[3]==0x02 => 芯片能应答型号 (通信有效)")
print("    !! 注意: skill 记录 08F7-C2 值在 RX[0], CB 在 RX[3] — 两侧都列出供核对")
print()

os.close(fd)
print("done")
