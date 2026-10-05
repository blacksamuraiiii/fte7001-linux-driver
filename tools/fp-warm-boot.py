#!/usr/bin/env python3
"""FTE7001/FT9338 开机暖路径武装 — 死态自愈方案
S5 冷启动后芯片自然进工作态(A5 5A + 0x9338)，此脚本负责探活并武装。
暖路径(芯片已活): 直接 init_and_arm → 0x30=BB → 1F/1E ARM
冷路径(芯片未活): 仅记录状态，等下次 S5
纯 stdlib，可在 systemd/root 环境直接跑。
"""
import ctypes, fcntl, struct, os, time, sys

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def main():
    # 1. 等待 spidev 就绪
    for _ in range(30):
        if os.path.exists("/dev/spidev0.0"):
            break
        time.sleep(1)
    else:
        log("spidev0.0 未就绪, 退出")
        return 1

    fd = os.open("/dev/spidev0.0", os.O_RDWR)
    fcntl.ioctl(fd, _IOC(1, MAGIC, 1, 1), struct.pack("<B", 0))
    fcntl.ioctl(fd, _IOC(1, MAGIC, 3, 1), struct.pack("<B", 8))
    fcntl.ioctl(fd, _IOC(1, MAGIC, 4, 4), struct.pack("<I", SPEED))

    def hs(r): return " ".join("%02X" % x for x in r)

    def wr_only(buf):
        tx = bytes(buf)
        b = ctypes.create_string_buffer(tx, len(tx))
        fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), 0,
                     len(tx), SPEED, 0, 8, 0, 0, 0, 0, 0))

    def xfer(tx, rlen, pad=0):
        tx = bytes(tx); m = rlen
        if pad and len(tx) < pad:
            tx = tx + b"\x00" * (pad - len(tx)); m = pad
        b = ctypes.create_string_buffer(tx, len(tx))
        rr = ctypes.create_string_buffer(m)
        fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b),
                    ctypes.addressof(rr), m, SPEED, 0, 8, 0, 0, 0, 0, 0))
        return bytes(rr.raw[:rlen])

    # 2. 探活 (warm-path probe)
    wr_only(b"\x70"); wr_only(b"\x70"); time.sleep(0.002)
    r20 = xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    alive = r20[4] == 0xA5 and r20[5] == 0x5A
    r16 = xfer([0x10, 0xEF, 0x16, 0x00, 0x00], 5)
    r17 = xfer([0x10, 0xEF, 0x17, 0x00, 0x00], 5)
    ver = (r16[4] << 8) | r17[4]
    log(f"探活: A5_5A={alive} 版本=0x{ver:04X}")

    if not alive or ver != 0x9338:
        log(f"芯片未活或版本不匹配(期望0x9338), 跳过武装 — 检查是否需S5")
        os.close(fd)
        return 0

    # 3. 暖路径: init_and_arm
    log("暖路径: 直接 init_and_arm")
    xfer([0x11, 0xEE, 0x01, 0x01, 0x00], 5)
    time.sleep(0.002)
    xfer([0x11, 0xEE, 0x30, 0xBB, 0x00], 5)
    time.sleep(0.002)
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    r30 = xfer([0x10, 0xEF, 0x30, 0x00, 0x00], 5)
    log(f"0x30 = {r30[4]:02X} {'✓' if r30[4] == 0xBB else '✗ FAIL'}")

    xfer([0x11, 0xEE, 0x1F, 0x01, 0x00], 5)
    time.sleep(0.002)
    xfer([0x11, 0xEE, 0x1E, 0x01, 0x00], 5)
    time.sleep(0.010)
    log("ARM done — 锁屏指纹解锁就绪")

    os.close(fd)
    return 0

if __name__ == "__main__":
    sys.exit(main())