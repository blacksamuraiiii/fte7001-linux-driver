#!/usr/bin/env python3
"""自研指纹解锁（纯 stdlib，无 numpy，可在 PAM/root 环境直接跑）。
用法: sudo python3 fp-unlock.py [阈值默认0.18]
返回码: 0=匹配成功 1=不匹配 2=无模板/超时
init_and_arm 按线上抓取补齐：0x30 回读硬判据 + 回读确认，实现长断电自武装。
"""
import ctypes, fcntl, struct, os, time, sys, subprocess, math

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000
H, W = 88, 88
IMAGE_SIZE = 7744
CAPTURE_SIZE = 7752
import os as _os
TPL = _os.path.expanduser("~/.local/share/fp-unlock/template.pgm")
SND = "/usr/share/sounds/freedesktop/stereo"
LOG = _os.path.expanduser("~/.local/share/fp-unlock/unlock.log")
LOCK = "/tmp/fp-unlock.pid"

def log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")
    except:
        pass

def acquire_lock():
    """返回 True 如果成功获取锁。已有实例运行则返回 False。"""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # 检查进程是否还活着
        try:
            with open(LOCK) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)  # 检查进程是否存在
            return False  # 还在运行
        except (ValueError, ProcessLookupError, OSError):
            # 进程不存在，删除旧锁重试
            os.unlink(LOCK)
            return acquire_lock()
        except:
            return False

def release_lock():
    try:
        os.unlink(LOCK)
    except:
        pass

fd = None
def setup_spi():
    global fd
    fd = os.open("/dev/spidev0.0", os.O_RDWR)
    fcntl.ioctl(fd, _IOC(1, MAGIC, 1, 1), struct.pack("<B", 0))
    fcntl.ioctl(fd, _IOC(1, MAGIC, 3, 1), struct.pack("<B", 8))
    fcntl.ioctl(fd, _IOC(1, MAGIC, 4, 4), struct.pack("<I", SPEED))

def xfer(tx, rlen, pad=0):
    tx = bytes(tx)
    m = rlen
    if pad and len(tx) < pad:
        tx = tx + b"\x00" * (pad - len(tx)); m = pad
    b = ctypes.create_string_buffer(tx, len(tx))
    rr = ctypes.create_string_buffer(m)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), ctypes.addressof(rr),
                                    m, SPEED, 0, 8, 0, 0, 0, 0, 0))
    return bytes(rr.raw[:rlen])

def soft_wake():
    """0x70×2 软唤醒（write-only，不碰 GPIO85）。芯片已活时零副作用。
    GPIO85 复位会把已活芯片打回死态，绝不能在 init 路径里碰它（2026-10-06 实测）。"""
    xfer([0x70], 1, pad=4096); time.sleep(0.006)
    xfer([0x70], 1, pad=4096); time.sleep(0.006)

def rd10ef(reg):
    """10EF 标准读：wire-capture-verified 先发 10EF 20 窗口前缀，再读；值在 RX[4:6]。"""
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    return xfer([0x10, 0xEF, reg, 0x00, 0x00, 0x00], 6)

def handshake_ok():
    """MCU 握手：10EF 20，RX[4]==0xA5 && RX[5]==0x5A。"""
    r = xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    return r[4] == 0xA5 and r[5] == 0x5A

def _gpio_rst(v):
    """GPIO85 nRST 电平控制（libgpiod v2 uAPI 原生 ioctl，不依赖 CLI）。"""
    import ctypes as ct
    class Req(ct.Structure):
        _fields_ = [("lo", ct.c_uint32 * 64), ("flags", ct.c_uint32),
                    ("dv", ct.c_uint8 * 64), ("cl", ct.c_char * 32),
                    ("lines", ct.c_uint32), ("fd", ct.c_int)]
    class Data(ct.Structure):
        _fields_ = [("val", ct.c_uint8 * 64)]
    OUT = 1 << 1
    LGET = (3 << 30) | (0xB4 << 8) | 3 | (ct.sizeof(Req) << 16)
    LSET = (3 << 30) | (0xB4 << 8) | 9 | (ct.sizeof(Data) << 16)
    chip = os.open("/dev/gpiochip0", os.O_RDWR)
    g = Req(); g.lo[0] = 85; g.flags = OUT; g.dv[0] = 1
    g.cl = b"fpul"; g.lines = 1
    fcntl.ioctl(chip, LGET, g)
    hfd = g.fd
    d = Data(); d.val[0] = v
    fcntl.ioctl(hfd, LSET, d)
    os.close(hfd); os.close(chip)

def cold_boot_sequence():
    """FT9338 冷启动全序列（2026-10-06 v5 实测通过, S5 后自愈）。
    依据: Windows cold-boot timeline + 反汇编 0x29A0/0x1CE4/0x001665。
    前提: 芯片握手失败（非 A5 5A）→ 芯片在 A-CUT boot。
    返回 True=固件已启动（后续走 init_and_arm 暖路径）。
    """
    FW = "/usr/local/libexec/ft9338-firmware.bin"
    try:
        blob = open(FW, "rb").read()
    except OSError:
        log("cold_boot: 固件文件缺失 %s" % FW)
        return False
    if len(blob) != 14136:
        log("cold_boot: 固件尺寸异常 %d" % len(blob))
        return False

    def wr_only(buf):
        tx = bytes(buf)
        b = ctypes.create_string_buffer(tx, len(tx))
        fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), 0, len(tx),
                                        SPEED, 0, 8, 0, 0, 0, 0, 0))

    # ① FE 存在性检查 5 轮（每轮: rst10ms + 55AA + CB/FD/FE + 10ms 停顿）
    for i in range(5):
        _gpio_rst(0); time.sleep(0.010); _gpio_rst(1)
        wr_only([0x55, 0xAA]); time.sleep(0.001)
        r_cb = xfer([0x08, 0xF7, 0xCB, 0x00], 4)
        if r_cb[3] != 0:  # 命令模式已开（真值非回显）
            xfer([0x09, 0xF6, 0xCB, r_cb[3] | 0x20], 4)
            xfer([0x09, 0xF6, 0xFD, 0x11], 4)
            xfer([0x09, 0xF6, 0xFE, 0x11], 4)
            r_fe = xfer([0x08, 0xF7, 0xFE, 0x00], 4)
            if r_fe[1] == 0x08 and r_fe[2] == 0xF7:
                time.sleep(0.010); continue  # 回显, 下一轮
            if r_fe[3] != 0:
                log("cold_boot: FE=0x%02X 芯片已有固件, 跳过下载" % r_fe[3])
                return True
        time.sleep(0.010)

    # ② 退 sensormode 尝试（10EF20 → 70×4 → 10EF20, 失败属预期）
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    for _ in range(4): wr_only([0x70])
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)

    # ③ IC_EnterDownloadMode ≤3 轮（rst10ms + 55AA + C2 写读, 成功判据读回 0x55）
    ok_dl = False
    for i in range(3):
        _gpio_rst(0); time.sleep(0.010); _gpio_rst(1)
        wr_only([0x55, 0xAA]); time.sleep(0.001)
        xfer([0x09, 0xF6, 0xC2, 0x55], 4)
        r = xfer([0x08, 0xF7, 0xC2, 0x00], 4)
        if r[3] == 0x55:
            ok_dl = True; break
        time.sleep(0.010)
    if not ok_dl:
        log("cold_boot: IC_EnterDownloadMode 失败 (C2 != 0x55)")
        return False

    # ④ 建立下载模式: 09F6 C8/CA/CB/B9×2
    for reg, val in [(0xC8, 0xFF), (0xCA, 0xFF), (0xCB, 0xFF),
                     (0xB9, 0xBF), (0xB9, 0xFF)]:
        xfer([0x09, 0xF6, reg, val], 4)

    # ⑤ 05FA 固件块写（14143B = 6 头 + 14136 blob + 1 尾）
    frame = bytes([0x05, 0xFA, 0x00, 0x00, 0x37, 0x38]) + blob + b"\x00"
    wr_only(frame)
    time.sleep(0.5)

    # ⑥ 04FB 回读校验
    rb = xfer([0x04, 0xFB, 0x00, 0x00, 0x37, 0x3A, 0x00], 14144)
    off = rb.find(blob[:64])
    if off < 0 or rb[off:off + 14136] != blob:
        log("cold_boot: 04FB 回读校验失败")
        return False

    # ⑦ ★ 芯片重启序列（反汇编 0x001665 证实, 缺它固件永不执行）:
    #    Sleep(2) → rst 低7ms→高 → Sleep(10) → rst 低7ms→高 → Sleep(180)
    time.sleep(0.002)
    _gpio_rst(0); time.sleep(0.007); _gpio_rst(1)
    time.sleep(0.010)
    _gpio_rst(0); time.sleep(0.007); _gpio_rst(1)
    time.sleep(0.180)

    # ⑧ 探活（≤4 轮 × 5ms, 然后 3×0.5s 宽限）
    for _ in range(4):
        r = xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
        if r[4] == 0xA5 and r[5] == 0x5A:
            log("cold_boot: 固件启动成功 (A5 5A)")
            return True
        time.sleep(0.005)
    for _ in range(3):
        time.sleep(0.5)
        r = xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
        if r[4] == 0xA5 and r[5] == 0x5A:
            log("cold_boot: 固件启动成功 (A5 5A, 延迟轮)")
            return True
    log("cold_boot: 重启后固件未应答")
    return False

def init_and_arm():
    """完整初始化 + 武装（线上抓取验证）。
    固件在跑时缺的只是初始化状态标志 0x30=0xBB。
    唯一硬判据：10EF 30 回读 RX[4]==0xBB。
    返回 True=武装成功，False=初始化未完成。
    """
    soft_wake()

    if not handshake_ok():
        # 2026-10-06: 芯片在 A-CUT boot → 走冷启动全序列（S5 后自愈）
        log("init_and_arm: 握手失败, 尝试冷启动全序列 …")
        if not cold_boot_sequence():
            log("init_and_arm: 冷启动序列失败 (需 S5 关机或检查固件文件)")
            return False
        if not handshake_ok():
            log("init_and_arm: 冷启动后握手仍失败")
            return False

    # 版本校验 (0x16/0x17 → 0x9338)
    r16 = xfer([0x10, 0xEF, 0x16, 0x00, 0x00], 5)
    r17 = xfer([0x10, 0xEF, 0x17, 0x00, 0x00], 5)
    ver = (r16[4] << 8) | r17[4]
    if ver != 0x9338:
        log("init_and_arm: 版本不匹配 0x%04X (期望 0x9338)" % ver)
        return False

    # 读 0x30 判断冷/暖
    r = rd10ef(0x30)
    pre = r[4]
    log("init_and_arm: 0x30 读回 = 0x%02X" % pre)

    if pre != 0xBB:
        # 未武装：固件在跑（调用方先验 ID/FW），只缺置初始化标志
        # capture notes：11EE 01 01 → 11EE 30 BB(不读回执) → 10EF 30 回读确认
        xfer([0x11, 0xEE, 0x01, 0x01, 0x00], 5); time.sleep(0.002)
        xfer([0x11, 0xEE, 0x30, 0xBB, 0x00], 5)   # 不读回执（#8: 写帧驱动不读）
        time.sleep(0.002)
        r = rd10ef(0x30)
        post = r[4]
        log("init_and_arm: 写 30BB 后回读 0x30 = 0x%02X" % post)
        if post != 0xBB:
            log("init_and_arm: 0x30 硬判据失败 (≠0xBB)，初始化未完成")
            return False

    # ARM 收尾（顺序不可换，the verified ARM order）
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    xfer([0x11, 0xEE, 0x1F, 0x01, 0x00], 5); time.sleep(0.002)
    xfer([0x11, 0xEE, 0x1E, 0x01, 0x00], 5); time.sleep(0.010)
    log("init_and_arm: armed 成功 (0x30=0xBB + 1F/1E)")
    return True

def wait_finger(timeout=10):
    """轮询 0x1D（10EF 5B 读，#8 值在 RX[4]）。0x01/0xA0=手指就绪。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r5 = xfer([0x10, 0xEF, 0x1D, 0x00, 0x00], 5)
        if r5[4] == 0x01 or r5[4] == 0xa0:
            return True
        time.sleep(0.05)
    return False

def capture_once():
    xfer([0x10, 0xEF, 0x30, 0x00, 0x00, 0x00], 6)
    xfer([0x10, 0xEF, 0x1D, 0x00, 0x00], 5)
    xfer([0x10, 0xEF, 0x1D, 0x00, 0x00, 0x00], 16)
    cmd = bytes([0x04, 0xFB, 0x34, 0x00, 0x1E, 0x48, 0x00])
    txbuf = cmd + b"\x00" * (CAPTURE_SIZE - len(cmd))
    b = ctypes.create_string_buffer(txbuf, len(txbuf))
    rr = ctypes.create_string_buffer(CAPTURE_SIZE)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), ctypes.addressof(rr),
                                    CAPTURE_SIZE, SPEED, 0, 8, 0, 0, 0, 0, 0))
    data = bytes(rr.raw[:CAPTURE_SIZE])
    return [~x & 0xff for x in data[8:8+IMAGE_SIZE]]

def read_pgm_pixels(path):
    with open(path, "rb") as f:
        d = f.read()
    assert d[:2] == b"P5", "非 P5 PGM"
    parts = []; i = 2
    while len(parts) < 3:
        while i < len(d) and d[i:i+1] in b" \t\r\n": i += 1
        j = i
        while j < len(d) and d[j:j+1] not in b" \t\r\n": j += 1
        parts.append(d[i:j]); i = j
    w, h = int(parts[0]), int(parts[1])
    i += 1
    return list(d[i:i + w*h]), w, h

def ncc(a, b):
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    dot = 0.0; na = 0.0; nb = 0.0
    for i in range(n):
        da = a[i] - ma; db = b[i] - mb
        dot += da * db; na += da * da; nb += db * db
    d = math.sqrt(na * nb)
    return dot / d if d > 0 else 0.0

def register_match(tpl, probe, max_shift=5):
    """在 ±5px 内搜索最大 NCC。tpl/probe 是 88x88 的 1D list。"""
    best = -1.0
    for dy in range(-max_shift, max_shift+1):
        for dx in range(-max_shift, max_shift+1):
            ay0, ay1 = max(0,-dy), min(H,H-dy)
            ax0, ax1 = max(0,-dx), min(W,W-dx)
            by0, by1 = max(0,dy), min(H,H+dy)
            bx0, bx1 = max(0,dx), min(W,W+dx)
            hh = ay1-ay0; ww = ax1-ax0
            if hh < 50 or ww < 50: continue
            # 提取重叠区域（行主序）
            pa = [tpl[(ay0+r)*W + ax0 + c] for r in range(hh) for c in range(ww)]
            pb = [probe[(by0+r)*W + bx0 + c] for r in range(hh) for c in range(ww)]
            c = ncc(pa, pb)
            if c > best: best = c
    return best

def notify(title, body):
    try:
        subprocess.run(["runuser", "-u", _os.environ.get("SUDO_USER", "nobody"), "--",
            "env", "XDG_RUNTIME_DIR=/run/user/1000", "WAYLAND_DISPLAY=wayland-1",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
            "omarchy", "notification", "send", "--app-name", "指纹解锁", title, body],
            capture_output=True, timeout=3)
    except:
        pass

def sound(name):
    try:
        subprocess.run(["runuser", "-u", _os.environ.get("SUDO_USER", "nobody"), "--",
            "env", "XDG_RUNTIME_DIR=/run/user/1000", "WAYLAND_DISPLAY=wayland-1",
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
            "pw-play", f"{SND}/{name}"], capture_output=True, timeout=3)
    except:
        pass

def main():
    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else 0.18
    if not acquire_lock():
        log("另一个实例运行中, 跳过")
        sys.exit(1)  # 已有实例在跑，立即退出
    try:
        if not os.path.exists(TPL):
            log("NO_TEMPLATE")
            print("NO_TEMPLATE"); sys.exit(2)
        tpl, tw, th = read_pgm_pixels(TPL)
        assert tw == W and th == H

        log("start threshold=%.2f" % threshold)
        sys.stdout.write("ready..."); sys.stdout.flush()

        setup_spi()

        # (可选) 读 ID/FW 探活确认固件在跑
        rid = rd10ef(0x14)
        if rid[4] != 0x58:
            log("ID 探活失败 0x14=0x%02X (全零则可能死态)" % rid[4])

        if not init_and_arm():
            log("init_and_arm 失败：0x30 未置 0xBB，锁屏解锁不可用")
            print("INIT_FAIL"); os.close(fd); sys.exit(3)

        log("init+ARM done, waiting finger (3s timeout)")
        if not wait_finger(3):
            log("TIMEOUT no finger")
            print("TIMEOUT"); os.close(fd); sys.exit(2)
        probe = capture_once()
        os.close(fd)
        log("captured, computing NCC")

        score = register_match(tpl, probe)
        ok = score >= threshold
        log("NCC=%.4f match=%s" % (score, "YES" if ok else "NO"))
        print(f"NCC={score:.4f}  threshold={threshold}  match={'YES' if ok else 'NO'}")
        sys.exit(0 if ok else 1)
    finally:
        release_lock()

if __name__ == "__main__":
    main()