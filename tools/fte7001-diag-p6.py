#!/usr/bin/env python3
"""FT9338 P6诊断: 钉死04FB锁门闸 — 最小间隙0x30+0x1D→04FB
用法: sudo python3 fte7001-diag-p6.py
"""
import ctypes, fcntl, struct, os, time, sys, subprocess, hashlib

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000
IMAGE_SIZE = 7744
CAPTURE_SIZE = IMAGE_SIZE + 8  # 7752

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "产物-P6诊断-" + time.strftime("%Y%m%d-%H%M%S"))
os.makedirs(OUT, exist_ok=True)

def log(s=""):
    print(s, flush=True)
    with open(os.path.join(OUT, "输出.txt"), "a") as f:
        f.write(s + "\n")

def hx(b): return " ".join("%02X" % v for v in b)

fd = None
def setup_spi():
    global fd
    fd = os.open("/dev/spidev0.0", os.O_RDWR)
    fcntl.ioctl(fd, _IOC(1, MAGIC, 1, 1), struct.pack("<B", 0))  # mode 0
    fcntl.ioctl(fd, _IOC(1, MAGIC, 3, 1), struct.pack("<B", 8))  # bpw 8
    fcntl.ioctl(fd, _IOC(1, MAGIC, 4, 4), struct.pack("<I", SPEED))

def xfer(tx, rlen, pad=0):
    tx = bytes(tx)
    m = rlen
    if pad and len(tx) < pad:
        tx = tx + b"\x00" * (pad - len(tx))
        m = pad
    b = ctypes.create_string_buffer(tx, len(tx))
    rr = ctypes.create_string_buffer(m)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), ctypes.addressof(rr),
                                    m, SPEED, 0, 8, 0, 0, 0, 0, 0))
    return bytes(rr.raw[:rlen])

def read_register_6b(reg):
    """标准6B寄存器读: 10 EF <reg> 00 00 00"""
    return xfer([0x10, 0xEF, reg, 0x00, 0x00, 0x00], 6)

def read_0x1d_5b():
    """Windows兼容5B短帧: 10 EF 1D 00 00"""
    return xfer([0x10, 0xEF, 0x1D, 0x00, 0x00], 5)

def read_0x1d_16b():
    """长帧: 10 EF 1D 00 00 00 (6B TX, 16B RX)"""
    return xfer([0x10, 0xEF, 0x1D, 0x00, 0x00, 0x00], 16)

def capture_04fb():
    """单事务全双工 04FB 大读 — 与0x1D检查零间隙"""
    cmd = bytes([0x04, 0xFB, 0x34, 0x00, 0x1E, 0x48, 0x00])
    txbuf = cmd + b"\x00" * (CAPTURE_SIZE - len(cmd))
    b = ctypes.create_string_buffer(txbuf, len(txbuf))
    rr = ctypes.create_string_buffer(CAPTURE_SIZE)
    t0 = time.time()
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), ctypes.addressof(rr),
                                    CAPTURE_SIZE, SPEED, 0, 8, 0, 0, 0, 0, 0))
    dt = time.time() - t0
    return bytes(rr.raw[:CAPTURE_SIZE]), dt

# ===== 全屏提示 =====
PROMPT_PATH = "/tmp/fp-prompt.txt"
PROM_SH = os.path.expanduser("~/Documents/one-mix3/omarchy/fingerprint/09-可行性实验/工具/提示窗-v3-常驻.sh")

def _hypr_sig():
    try:
        d = "/run/user/1000/hypr"
        sigs = [x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x))]
        return sigs[0] if sigs else ""
    except:
        return ""

def as_user(*cmd, timeout=15):
    env = {"XDG_RUNTIME_DIR": "/run/user/1000",
           "WAYLAND_DISPLAY": "wayland-1",
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
           "HYPRLAND_INSTANCE_SIGNATURE": _hypr_sig()}
    try:
        if os.geteuid() == 0:
            p = subprocess.run(["runuser", "-u", os.getlogin(), "--", "env",
                *["%s=%s" % (k, v) for k, v in env.items() if v], *cmd],
                timeout=timeout, capture_output=True)
        else:
            p = subprocess.run(cmd, env={**os.environ, **env},
                              timeout=timeout, capture_output=True)
        return p.returncode, (p.stdout or b"").decode(errors="replace")
    except:
        return -1, ""

def as_user_bg(*cmd):
    env = {"XDG_RUNTIME_DIR": "/run/user/1000",
           "WAYLAND_DISPLAY": "wayland-1",
           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
           "HYPRLAND_INSTANCE_SIGNATURE": _hypr_sig()}
    try:
        if os.geteuid() == 0:
            return subprocess.Popen(["runuser", "-u", os.getlogin(), "--", "env",
                *["%s=%s" % (k, v) for k, v in env.items() if v], *cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            return subprocess.Popen(cmd, env={**os.environ, **env},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except:
        return None

def sound(name, n=1):
    for _ in range(n):
        as_user("pw-play", "/usr/share/sounds/freedesktop/stereo/" + name)
        time.sleep(0.15)

def prompt(*lines):
    with open(PROMPT_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")

def start_prompt():
    as_user("pkill", "-f", "foot.*fp-prompt")
    time.sleep(0.5)
    with open(PROMPT_PATH, "w") as f:
        f.write("启动中...\n")
    p = as_user_bg("foot", "--app-id=fp-prompt", "--fullscreen",
                   "--font=monospace:size=30", "--title=P6诊断",
                   "bash", PROM_SH)
    if p is None:
        raise RuntimeError("foot 启动失败")
    time.sleep(1.2)
    rc, out = as_user("hyprctl", "clients", "-j")
    if rc != 0 or "fp-prompt" not in out:
        raise RuntimeError("全屏窗未出现 rc=%d" % rc)

def close_prompt():
    as_user("pkill", "-f", "foot.*fp-prompt")

# ===== main =====
log("### P6诊断: 04FB锁门闸  %s" % time.strftime("%F %T"))
log("### uptime=%ds" % float(open("/proc/uptime").read().split()[0]))
log("### 产物: %s\n" % OUT)

setup_spi()

# 1. Wake
log("=== 1. 唤醒 ===")
subprocess.run(["gpioset", "-c", "gpiochip0", "-t", "20ms,0", "85=0"],
               capture_output=True)
time.sleep(0.05)
for i in (1, 2):
    xfer([0x70], 1, pad=4096)
    time.sleep(0.006)
time.sleep(0.01)
r20 = read_register_6b(0x20)
alive = r20[4:6] == b"\xa5\x5a"
log("  MCU: %s alive=%s" % (hx(r20), "YES" if alive else "NO"))
log("  0x20 全6B: %s" % hx(r20))

# 2. Config init
log("\n=== 2. Config init ===")
for reg, val, name in [(0x01, 0x01, "01"), (0x41, 0x0F, "41"),
                       (0x30, 0xBB, "30"), (0x22, 0x00, "22"), (0x23, 0x0E, "23")]:
    xfer([0x11, 0xEE, reg, val, 0x00], 5)
    time.sleep(0.002)
r = read_register_6b(0x30)
v30 = r[4]
log("  0x30 = 0x%02X %s" % (v30, "OK" if v30 == 0xBB else "FAIL"))

# 3. ARM
log("\n=== 3. ARM ===")
xfer([0x11, 0xEE, 0x1F, 0x01, 0x00], 5)
time.sleep(0.002)
xfer([0x11, 0xEE, 0x1E, 0x01, 0x00], 5)
time.sleep(0.010)
r20 = read_register_6b(0x20)
log("  MCU after ARM: %s %s" % (hx(r20), "alive" if r20[4:6] == b"\xa5\x5a" else "DEAD/BUSY"))

# 4. 全屏提示 + 0x1D 轮询
log("\n=== 4. 等待手指(全屏提示) ===")
start_prompt()
for i in range(4, 0, -1):
    prompt("准备", "", "叮声后 按住手指", "剩余 %d 秒" % i)
    if i <= 2:
        sound("bell.oga")
    time.sleep(1)
sound("dialog-warning.oga", 3)
prompt("按住手指!", "", "正在检测...", "")

t0 = time.time()
detected_5b = False
detected_16b = False
last_5b = b""
last_16b = b""
while time.time() - t0 < 25.0:
    r5 = read_0x1d_5b()
    r16 = read_0x1d_16b()
    rx4_5b = r5[4]
    rx23_16b = r16[2:4]
    last_5b = r5
    last_16b = r16

    if rx4_5b in (0x01, 0xA0):
        detected_5b = True
    if rx23_16b == b"\x11\x11":
        detected_16b = True

    if detected_5b or detected_16b:
        dt = time.time() - t0
        log("  ★ 检出 @ %.2fs  5B[4]=0x%02X  16B[2:3]=%s" % (dt, rx4_5b,
            " ".join("%02X" % v for v in rx23_16b)))
        log("  5B全: %s" % hx(r5))
        log("  16B全: %s" % hx(r16))

        # === 关键段：零间隙 0x30 → 0x1D → 04FB ===
        # 这一段不能有任何 I/O——不能写文件、不能 subprocess、不能 print
        t_crit = time.time()

        # 前置A: 读0x30
        r30_pre = read_register_6b(0x30)
        v30_pre = r30_pre[4]

        # 前置B: 读0x1D (双格式)
        r1d_5b_pre = read_0x1d_5b()
        r1d_16b_pre = read_0x1d_16b()

        # 大读
        data, cap_dt = capture_04fb()

        t_crit_end = time.time()
        crit_ms = (t_crit_end - t_crit) * 1000
        # === 关键段结束，现在可以 I/O 了 ===

        log("\n=== 5. 关键段结果 (%.1fms) ===" % crit_ms)
        log("  前置 0x30 = 0x%02X  %s" % (v30_pre, "OK" if v30_pre == 0xBB else "FAIL"))
        log("  前置 0x1D 5B: %s  RX[4]=0x%02X" % (hx(r1d_5b_pre), r1d_5b_pre[4]))
        log("  前置 0x1D 16B: %s  RX[2:3]=%s" % (hx(r1d_16b_pre),
            " ".join("%02X" % v for v in r1d_16b_pre[2:4])))
        log("  大读耗时: %.1fms  收到: %dB" % (cap_dt * 1000, len(data)))
        log("  头16B: %s" % hx(data[:16]))
        log("  头8B:  %s" % hx(data[:8]))

        # 变化区统计
        img = data[8:8+IMAGE_SIZE]
        raw_unique = len(set(img))
        inv = bytes(~b & 0xFF for b in img)
        inv_unique = len(set(inv))

        # 按576B边界分段统计
        seg_a = img[:576]
        seg_b = img[576:]
        uniq_a = len(set(seg_a))
        uniq_b = len(set(seg_b))
        vals_b = sorted(set(seg_b))
        log("  图像段A (0-575B): unique=%d" % uniq_a)
        log("  图像段B (576-7743B): unique=%d  values=%s" % (uniq_b,
            ", ".join("0x%02X" % v for v in vals_b[:20])))

        # 整帧统计
        log("  raw unique=%d  inv unique=%d" % (raw_unique, inv_unique))

        # 保存
        fn_raw = os.path.join(OUT, "capture-raw.bin")
        fn_inv = os.path.join(OUT, "capture-inv.pgm")
        with open(fn_raw, "wb") as f:
            f.write(data)
        with open(fn_inv, "wb") as f:
            f.write(b"P5\n88 88\n255\n" + inv)
        log("  保存: %s" % fn_raw)

        # MCU后态
        r20_after = read_register_6b(0x20)
        log("  MCU after capture: %s" % hx(r20_after))

        # 响应头判定
        hdr = data[:8]
        if hdr[:2] == b"\x04\xFB" or hdr[1:3] == b"\x04\xFB" or hdr[:3] == b"\x00\x04\xFB":
            log("\n  ⚠ 诊断: 响应头含命令回显 (04 FB) → 芯片未执行04FB")
        elif hdr[:2] == b"\x50\x01":
            log("\n  ✓ 响应头匹配Windows (50 01) → 芯片正常执行04FB")
        else:
            log("\n  ? 响应头未知格式: %s" % hx(hdr))

        if uniq_b <= 10 and uniq_a > 30:
            log("  ⚠ 模式: 死区(段B<%d unique) → 传输中断/芯片未输出全帧" % uniq_b)
        else:
            log("  ? 模式未定: 段A=%d 段B=%d" % (uniq_a, uniq_b))

        # 统计哪些字节位置有变化 (采样每行开头)
        log("\n  === 逐行采样(每行首4B) ===")
        for row in range(88):
            off = row * 88
            if off + 4 <= IMAGE_SIZE:
                log("  row%2d [%4d-%4d]: %s" % (row, off, off+3, hx(img[off:off+4])))

        sound("complete.oga", 1)
        prompt("完成!", "", "请松开手指", "", "产物: %s" % os.path.basename(OUT))
        time.sleep(2)
        close_prompt()
        os.close(fd)
        sys.exit(0)

    # 每2s更新一次提示（减少I/O）
    if int(time.time() - t0) % 2 == 0:
        prompt("按住手指!", "", "正在检测...",
               "5B[4]=0x%02X  16B=%s" % (last_5b[4] if len(last_5b) > 4 else 0,
                   " ".join("%02X"%v for v in last_16b[2:4]) if len(last_16b) > 3 else "?"))

    time.sleep(0.05)

log("\n=== 超时: 25s内未检出 ===")
log("  最后5B: %s  RX[4]=0x%02X" % (hx(last_5b), last_5b[4] if len(last_5b) >= 5 else 0))
log("  最后16B: %s  RX[2:3]=%s" % (hx(last_16b),
    " ".join("%02X" % v for v in last_16b[2:4]) if len(last_16b) >= 4 else "?"))
sound("bell.oga", 2)
prompt("未检测到手指", "", "松开重试", "")
time.sleep(2)
close_prompt()
os.close(fd)
sys.exit(3)