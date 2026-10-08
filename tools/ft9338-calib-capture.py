#!/usr/bin/env python3
"""FT9338 形态B 标定采样：采 ≥15 张图（同指10 + 异指5），存 PGM。

规则（来自仓库改造清单 §四.1 / 附篇三 B）：
- 每次抬起重按，不允许连续压着采；
- 全屏提示窗 + 声音 + 倒计时（ oma-control fullscreen-prompt 机制）；
- 输出 PGM 88×88，灰度= capture_once 同款取反（与驱动交框架方向一致）；
- 输出目录按同指/异指分组，文件名带序号+时间戳。

用法: sudo python3 ft9338-calib-capture.py <输出目录> [同指张数=10] [异指张数=5]
不指定输出目录 → 默认 ~/oc-tmp/calib-YYYYmmdd-HHMM/

前置（脚本自检并提示）：
- fprintd 停（systemctl stop fprintd）
- /etc/pam.d/omarchy-lock-fingerprint 换 pam_permit（防锁屏 PAM 抢 SPI）
- /tmp/fp-unlock.pid 清掉
"""
import os, sys, time, subprocess, ctypes, fcntl, struct

# ---------- SPI 层（与 fp-unlock.py 完全同源，生产判据） ----------
MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000
H, W = 88, 88
IMAGE_SIZE = 7744
CAPTURE_SIZE = 7752

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

def wr_only(buf):
    tx = bytes(buf)
    b = ctypes.create_string_buffer(tx, len(tx))
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b), 0, len(tx),
                                    SPEED, 0, 8, 0, 0, 0, 0, 0))

def soft_wake():
    xfer([0x70], 1, pad=4096); time.sleep(0.006)
    xfer([0x70], 1, pad=4096); time.sleep(0.006)

def _gpio_rst(v):
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
    g.cl = b"fpcap"; g.lines = 1
    fcntl.ioctl(chip, LGET, g)
    hfd = g.fd
    d = Data(); d.val[0] = v
    fcntl.ioctl(hfd, LSET, d)
    os.close(hfd); os.close(chip)

FW = "/usr/local/libexec/ft9338-firmware.bin"

def handshake_ok():
    r = xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    return r[4] == 0xA5 and r[5] == 0x5A

def rd10ef(reg):
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    return xfer([0x10, 0xEF, reg, 0x00, 0x00, 0x00], 6)

def cold_boot_sequence():
    """fp-unlock.py 同源冷序列（S5 自愈），此处仅摘要判据。"""
    blob = open(FW, "rb").read()
    assert len(blob) == 14136
    for i in range(5):
        _gpio_rst(0); time.sleep(0.010); _gpio_rst(1)
        wr_only([0x55, 0xAA]); time.sleep(0.001)
        r_cb = xfer([0x08, 0xF7, 0xCB, 0x00], 4)
        if r_cb[3] != 0:
            xfer([0x09, 0xF6, 0xCB, r_cb[3] | 0x20], 4)
            xfer([0x09, 0xF6, 0xFD, 0x11], 4)
            xfer([0x09, 0xF6, 0xFE, 0x11], 4)
            r_fe = xfer([0x08, 0xF7, 0xFE, 0x00], 4)
            if r_fe[1] == 0x08 and r_fe[2] == 0xF7:
                time.sleep(0.010); continue
            if r_fe[3] != 0:
                return True
        time.sleep(0.010)
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    for _ in range(4): wr_only([0x70])
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    ok_dl = False
    for i in range(3):
        _gpio_rst(0); time.sleep(0.010); _gpio_rst(1)
        wr_only([0x55, 0xAA]); time.sleep(0.001)
        xfer([0x09, 0xF6, 0xC2, 0x55], 4)
        r = xfer([0x08, 0xF7, 0xC2, 0x00], 4)
        if r[3] == 0x55:
            ok_dl = True; break
        time.sleep(0.010)
    if not ok_dl: return False
    for reg, val in [(0xC8, 0xFF), (0xCA, 0xFF), (0xCB, 0xFF),
                     (0xB9, 0xBF), (0xB9, 0xFF)]:
        xfer([0x09, 0xF6, reg, val], 4)
    frame = bytes([0x05, 0xFA, 0x00, 0x00, 0x37, 0x38]) + blob + b"\x00"
    wr_only(frame); time.sleep(0.5)
    rb = xfer([0x04, 0xFB, 0x00, 0x00, 0x37, 0x3A, 0x00], 14144)
    off = rb.find(blob[:64])
    if off < 0 or rb[off:off + 14136] != blob: return False
    time.sleep(0.002)
    _gpio_rst(0); time.sleep(0.007); _gpio_rst(1)
    time.sleep(0.010)
    _gpio_rst(0); time.sleep(0.007); _gpio_rst(1)
    time.sleep(0.180)
    for _ in range(4):
        if handshake_ok(): return True
        time.sleep(0.005)
    for _ in range(3):
        time.sleep(0.5)
        if handshake_ok(): return True
    return False

def init_and_arm():
    soft_wake()
    if not handshake_ok():
        if not cold_boot_sequence(): return False
        if not handshake_ok(): return False
    r16 = xfer([0x10, 0xEF, 0x16, 0x00, 0x00], 5)
    r17 = xfer([0x10, 0xEF, 0x17, 0x00, 0x00], 5)
    if ((r16[4] << 8) | r17[4]) != 0x9338: return False
    r = rd10ef(0x30)
    if r[4] != 0xBB:
        xfer([0x11, 0xEE, 0x01, 0x01, 0x00], 5); time.sleep(0.002)
        xfer([0x11, 0xEE, 0x30, 0xBB, 0x00], 5); time.sleep(0.002)
        r = rd10ef(0x30)
        if r[4] != 0xBB: return False
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    xfer([0x11, 0xEE, 0x1F, 0x01, 0x00], 5); time.sleep(0.002)
    xfer([0x11, 0xEE, 0x1E, 0x01, 0x00], 5); time.sleep(0.010)
    return True

def wait_finger_1111(timeout=15):
    """判据 = RX[2:3]==11 11（主）或 RX[4]∈{0x01,0xA0}（辅），双帧确认 60ms。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r5 = xfer([0x10, 0xEF, 0x1D, 0x00, 0x00], 5)
        hit = (r5[2] == 0x11 and r5[3] == 0x11) or r5[4] in (0x01, 0xA0)
        if hit:
            time.sleep(0.06)
            r5b = xfer([0x10, 0xEF, 0x1D, 0x00, 0x00], 5)
            hit2 = (r5b[2] == 0x11 and r5b[3] == 0x11) or r5b[4] in (0x01, 0xA0)
            if hit2: return True
            continue
        # 挂起看门狗：连续全零约 1s → 软唤醒
        if r5 == b"\x00" * 5:
            zero_run[0] += 1
            if zero_run[0] >= 20:
                soft_wake(); zero_run[0] = 0
        else:
            zero_run[0] = 0
        time.sleep(0.05)
    return False
zero_run = [0]

def capture_once():
    """fp-unlock.py 生产序列逐帧同源（含 16B 帧唯一合法位置）。"""
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
    return [~x & 0xff for x in data[8:8 + IMAGE_SIZE]]

def wait_finger_off(timeout=6):
    """采后等真抬起：5B 连续 2 帧不在位才认为抬起，兜底 timeout。"""
    t0 = time.time(); off_run = 0
    while time.time() - t0 < timeout:
        r5 = xfer([0x10, 0xEF, 0x1D, 0x00, 0x00], 5)
        present = (r5[2] == 0x11 and r5[3] == 0x11) or r5[4] in (0x01, 0xA0)
        off_run = off_run + 1 if not present else 0
        if off_run >= 2: return True
        time.sleep(0.05)
    return False

def rearm_after_capture():
    """ReturnAutoPower + 重 ARM（驱动 CAPTURE 每轮从 ARM_1F 重启，journal 实证）。"""
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    xfer([0x11, 0xEE, 0x54, 0x01, 0x00], 5)
    xfer([0x10, 0xEF, 0x20, 0x00, 0x00, 0x00], 6)
    xfer([0x11, 0xEE, 0x1F, 0x01, 0x00], 5); time.sleep(0.002)
    xfer([0x11, 0xEE, 0x1E, 0x01, 0x00], 5); time.sleep(0.010)

# ---------- 全屏提示窗（oma-control fullscreen-prompt 同源机制） ----------
PROMPT = "/tmp/fp-prompt.txt"
_HOME = os.path.expanduser("~")
_UID = os.getuid()
UX = dict(XDG_RUNTIME_DIR=f"/run/user/{_UID}", WAYLAND_DISPLAY="wayland-1",
          DBUS_SESSION_BUS_ADDRESS=f"unix:path=/run/user/{_UID}/bus")
try:
    UX["HYPRLAND_INSTANCE_SIGNATURE"] = os.listdir(f"/run/user/{_UID}/hypr")[0]
except Exception:
    pass
SND = "/usr/share/sounds/freedesktop/stereo"

def as_user(*cmd, timeout=15):
    try:
        if os.geteuid() == 0:
            p = subprocess.run(["runuser", "-u", _HOME.split("/")[-1], "--", "env",
                                *[f"{k}={v}" for k, v in UX.items()], *cmd],
                               timeout=timeout, capture_output=True)
        else:
            env = {**os.environ, **UX}
            p = subprocess.run(cmd, env=env, timeout=timeout, capture_output=True)
        return p.returncode, (p.stdout or b"").decode(errors="replace")
    except Exception as e:
        return -1, str(e)

def as_user_bg(*cmd):
    try:
        if os.geteuid() == 0:
            return subprocess.Popen(["runuser", "-u", _HOME.split("/")[-1], "--", "env",
                                     *[f"{k}={v}" for k, v in UX.items()], *cmd],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env = {**os.environ, **UX}
        return subprocess.Popen(cmd, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return None

def sound(f, n=1):
    for _ in range(n):
        as_user("pw-play", f"{SND}/{f}")
        time.sleep(0.18)

def prompt(*lines):
    with open(PROMPT, "w") as f:
        f.write("\n".join(lines) + "\n")

def bar(frac, w=36):
    frac = max(0.0, min(1.0, frac))
    return "#" * int(w * frac) + "." * (w - int(w * frac))

def start_prompt_window():
    as_user("pkill", "-f", "app-id=fp-prompt"); time.sleep(0.6)
    with open(PROMPT, "w") as f:
        f.write("启动中...\n")
    popen = as_user_bg("foot", "--app-id=fp-prompt", "--fullscreen",
                       "--font=monospace:size=30", "--title=指纹采集",
                       "bash", PROM_SH)
    if popen is None:
        raise RuntimeError("foot 后台启动失败")
    rc = None; out = ""
    for _ in range(20):
        rc, out = as_user("hyprctl", "clients", "-j")
        if rc == 0 and "fp-prompt" in out:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError(f"全屏提示窗未出现（rc={rc}）: {out[:300]}")
    as_user("hyprctl", "dispatch", 'hl.dsp.focus({window="class:fp-prompt"})')

def close_prompt_window():
    as_user("pkill", "-f", "app-id=fp-prompt")

# ---------- 采集主流程 ----------
def save_pgm(path, pixels):
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n255\n" % (W, H))
        f.write(bytes(pixels))

def img_stats(pixels):
    n = len(pixels)
    mean = sum(pixels) / n
    lo = sum(1 for p in pixels if p < 20)
    hi = sum(1 for p in pixels if p > 235)
    return mean, lo, hi

def capture_one_round(idx, total, label):
    """一轮：倒计时 → 叮 → 等按 → 采图 → 等抬起。返回 pixels 或 None。"""
    for i in range(3, 0, -1):
        prompt(label, "", f"第 {idx} / {total} 张",
               "", "叮声后 按住手指", f"剩余 {i} 秒")
        if i <= 1:
            sound("bell.oga")
        time.sleep(1)
    sound("dialog-warning.oga", 2)
    prompt(label, "", f"第 {idx} / {total} 张", "", "现在按住手指（保持）", "")
    if not wait_finger_1111(15):
        prompt("超时未检测到手指", "", "本轮跳过", "")
        sound("dialog-error.oga")
        time.sleep(1.5)
        return None
    t0 = time.time()
    pixels = capture_once()
    rearm_after_capture()
    # 等真抬起（2 帧不在位），最多 6s
    prompt(label, "", f"第 {idx} / {total} 张 已采集", "", "请抬起手指", "")
    sound("complete.oga", 1)
    wait_finger_off(6)
    time.sleep(0.35)
    return pixels

def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else None
    n_same = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    n_diff = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    if not outdir:
        outdir = os.path.expanduser("~/oc-tmp/calib-" +
                                    time.strftime("%Y%m%d-%H%M"))
    same_dir = os.path.join(outdir, "same")
    diff_dir = os.path.join(outdir, "diff")
    os.makedirs(same_dir, exist_ok=True)
    os.makedirs(diff_dir, exist_ok=True)

    # 自检：fprintd 必须停（否则抢 SPI）
    _chk = subprocess.run(["systemctl", "is-active", "fprintd"],
                          capture_output=True, timeout=10).stdout.decode().strip()
    if _chk == "active":
        print("fprintd 还在跑！先: sudo systemctl stop fprintd")
        sys.exit(1)
    # 自检：锁屏 PAM 提示（不自动改，人工确认）
    if os.path.exists("/tmp/fp-unlock.pid"):
        os.unlink("/tmp/fp-unlock.pid")

    print("输出目录:", outdir)
    print(f"计划: 同指 {n_same} 张 + 异指 {n_diff} 张")

    setup_spi()
    print("init_and_arm ...")
    if not init_and_arm():
        print("INIT_FAIL（详见 fp-unlock 判据）")
        sys.exit(3)
    print("armed OK")

    results = []
    try:
        start_prompt_window()
    except RuntimeError as e:
        print("提示窗失败:", e)
        sys.exit(4)

    try:
        total = n_same + n_diff
        got_same = 0
        prompt("准备", "", f"共 {total} 张：同指 {n_same} + 异指 {n_diff}",
               "", "从下一屏开始", "倒计时 3 秒")
        time.sleep(2.5)

        for i in range(1, n_same + 1):
            px = capture_one_round(i, total, "同指（常用手指）")
            if px is None:
                continue
            mean, lo, hi = img_stats(px)
            name = os.path.join(same_dir, f"s{i:02d}-{time.strftime('%H%M%S')}.pgm")
            save_pgm(name, px)
            got_same += 1
            results.append(("same", name, mean, lo, hi))
            print(f"[same {i}/{n_same}] {name} mean={mean:.1f} dark={lo} bright={hi}")
            time.sleep(0.8)

        if n_diff > 0:
            prompt("换另一根手指", "", f"接下来 {n_diff} 张 异指", "", "准备好后看下一屏", "")
            sound("bell.oga")
            time.sleep(3)

        for j in range(1, n_diff + 1):
            idx = n_same + j
            px = capture_one_round(idx, total, "异指（另一根手指）")
            if px is None:
                continue
            mean, lo, hi = img_stats(px)
            name = os.path.join(diff_dir, f"d{j:02d}-{time.strftime('%H%M%S')}.pgm")
            save_pgm(name, px)
            results.append(("diff", name, mean, lo, hi))
            print(f"[diff {j}/{n_diff}] {name} mean={mean:.1f} dark={lo} bright={hi}")
            time.sleep(0.8)

        prompt("采集完成", "",
               f"同指 {got_same}/{n_same}  异指 "
               f"{sum(1 for r in results if r[0]=='diff')}/{n_diff}",
               "", "窗口即将关闭", "")
        sound("complete.oga", 2)
        time.sleep(2)
    finally:
        close_prompt_window()
        os.close(fd)
        # 落盘采集清单
        man = os.path.join(outdir, "manifest.tsv")
        with open(man, "w") as f:
            f.write("type\tpath\tmean\tdark<20\tbright>235\n")
            for t, p, m, lo, hi in results:
                f.write(f"{t}\t{p}\t{m:.1f}\t{lo}\t{hi}\n")
        print("清单:", man)

    bad = [r for r in results if r[2] < 25 or r[3] > IMAGE_SIZE * 0.85]
    if bad:
        print(f"⚠ {len(bad)} 张疑似空图/质量差（见清单），建议补采")
    print(f"完成：{len(results)} 张")

if __name__ == "__main__":
    main()
