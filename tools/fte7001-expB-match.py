#!/usr/bin/env python3
"""实验B: 同指纹连采3幅 + 匹配验证
用法: sudo python3 fte7001-expB-match.py
全屏提示按3次手指, 每次 ReturnAutoPower 重挂, 存 PGM + bin
"""
import ctypes, fcntl, struct, os, time, subprocess, sys

MAGIC = 0x6B
def _IOC(d, t, nr, sz): return (d << 30) | (sz << 16) | (t << 8) | nr
def MSG(n): return _IOC(1, MAGIC, 0, 32 * n)
TR = struct.Struct("<QQIIHBBBBBB")
SPEED = 1000000
CAPTURE_SIZE = 7752
N_CAPTURES = 3

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "产物-实验B匹配-" + time.strftime("%Y%m%d-%H%M%S"))
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

def rd(reg, n=6): return xf([0x10,0xEF,reg,0x00,0x00,0x00], n)

def capture_with_gate():
    rd(0x30, 6)
    xf([0x10,0xEF,0x1D,0x00,0x00], 5)
    cmd = bytes([0x04,0xFB,0x34,0x00,0x1E,0x48,0x00])
    txb = cmd + b"\x00"*(CAPTURE_SIZE-len(cmd))
    b = ctypes.create_string_buffer(txb,len(txb))
    rr = ctypes.create_string_buffer(CAPTURE_SIZE)
    fcntl.ioctl(fd, MSG(1), TR.pack(ctypes.addressof(b),ctypes.addressof(rr),CAPTURE_SIZE,SPEED,0,8,0,0,0,0,0))
    return bytes(rr.raw[:CAPTURE_SIZE])

def return_autopower():
    xf([0x10,0xEF,0x20,0x00,0x00,0x00], 6)
    xf([0x11,0xEE,0x54,0x01,0x00], 5)
    time.sleep(0.002)
    xf([0x10,0xEF,0x20,0x00,0x00,0x00], 6)

# ===== 全屏提示 =====
PROMPT_PATH = "/tmp/fp-prompt.txt"
PROM_SH = os.path.expanduser("~/Documents/one-mix3/omarchy/fingerprint/09-可行性实验/工具/提示窗-v3-常驻.sh")

def _hypr_sig():
    try:
        d="/run/user/1000/hypr"
        sigs=[x for x in os.listdir(d) if os.path.isdir(os.path.join(d,x))]
        return sigs[0] if sigs else ""
    except: return ""

def as_user(*cmd, timeout=15):
    env={"XDG_RUNTIME_DIR":"/run/user/1000","WAYLAND_DISPLAY":"wayland-1",
         "DBUS_SESSION_BUS_ADDRESS":"unix:path=/run/user/1000/bus",
         "HYPRLAND_INSTANCE_SIGNATURE":_hypr_sig()}
    try:
        if os.geteuid()==0:
            p=subprocess.run(["runuser","-u",os.getlogin(),"--","env",
                *["%s=%s"%(k,v) for k,v in env.items() if v],*cmd],
                timeout=timeout, capture_output=True)
        else:
            p=subprocess.run(cmd, env={**os.environ,**env}, timeout=timeout, capture_output=True)
        return p.returncode,(p.stdout or b"").decode(errors="replace")
    except: return -1,""

def as_user_bg(*cmd):
    env={"XDG_RUNTIME_DIR":"/run/user/1000","WAYLAND_DISPLAY":"wayland-1",
         "DBUS_SESSION_BUS_ADDRESS":"unix:path=/run/user/1000/bus",
         "HYPRLAND_INSTANCE_SIGNATURE":_hypr_sig()}
    try:
        if os.geteuid()==0:
            return subprocess.Popen(["runuser","-u",os.getlogin(),"--","env",
                *["%s=%s"%(k,v) for k,v in env.items() if v],*cmd],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except: return None

def sound(name,n=1):
    for _ in range(n): as_user("pw-play","/usr/share/sounds/freedesktop/stereo/"+name); time.sleep(0.15)

def prompt(*lines):
    with open(PROMPT_PATH,"w") as f: f.write("\n".join(lines)+"\n")

def start_prompt():
    as_user("pkill","-f","foot.*fp-prompt"); time.sleep(0.5)
    with open(PROMPT_PATH,"w") as f: f.write("启动中...\n")
    if as_user_bg("foot","--app-id=fp-prompt","--fullscreen","--font=monospace:size=30","--title=采图","bash",PROM_SH) is None:
        raise RuntimeError("foot 启动失败")
    time.sleep(1.2)
    rc,out=as_user("hyprctl","clients","-j")
    if rc!=0 or "fp-prompt" not in out: raise RuntimeError("全屏窗未出现")

def close_prompt(): as_user("pkill","-f","foot.*fp-prompt")

def wait_for_finger(timeout_s=20):
    t0=time.time()
    while time.time()-t0 < timeout_s:
        r5=xf([0x10,0xEF,0x1D,0x00,0x00],5)
        if r5[4] in (0x01,0xA0):
            return True, time.time()-t0, r5[4]
        time.sleep(0.03)
    return False, timeout_s, 0

# ===== main =====
log("### 实验B: 同指连采%d幅  %s" % (N_CAPTURES, time.strftime("%F %T")))
log("uptime=%ds" % float(open("/proc/uptime").read().split()[0]))

log("\n=== Wake ===")
subprocess.run(["gpioset","-c","gpiochip0","-t","20ms,0","85=0"],capture_output=True)
time.sleep(0.05)
for i in (1,2): xf([0x70],1,pad=4096); time.sleep(0.006)
time.sleep(0.01)
r=rd(0x20,6)
log("MCU: %s alive=%s" % (hx(r), "YES" if r[4:6]==b"\xa5\x5a" else "NO"))

log("\n=== Config init ===")
for reg,val in [(0x01,0x01),(0x41,0x0F),(0x30,0xBB),(0x22,0x00),(0x23,0x0E)]:
    xf([0x11,0xEE,reg,val,0x00],5); time.sleep(0.002)
r=rd(0x30,6)
log("0x30=0x%02X %s"%(r[4],"OK" if r[4]==0xBB else "FAIL"))

log("\n=== ARM ===")
xf([0x11,0xEE,0x1F,0x01,0x00],5); time.sleep(0.002)
xf([0x11,0xEE,0x1E,0x01,0x00],5); time.sleep(0.010)

start_prompt()

captures = []
for ci in range(1, N_CAPTURES+1):
    sound("bell.oga")
    prompt("采集 %d/%d"%(ci, N_CAPTURES), "", "请按住手指...", "")
    time.sleep(1.5)
    sound("dialog-warning.oga", 2)
    prompt("按住手指!", "", "采集 %d/%d"%(ci, N_CAPTURES), "正在检测...")

    found, dt_wait, rx4 = wait_for_finger(20)
    if not found:
        log("X 采集%d: 20s未检测到手指" % ci)
        sound("bell.oga",3)
        prompt("超时!", "", "采集 %d 未检测到手指" % ci)
        time.sleep(2)
        close_prompt(); os.close(fd); sys.exit(3)

    log("\n采集%d: 手指检出 @ %.2fs RX[4]=0x%02X" % (ci, dt_wait, rx4))
    prompt("检测到手指!", "", "采图 %d/%d..."%(ci, N_CAPTURES), "")

    data = capture_with_gate()
    img = data[8:8+7744]
    inv = bytes(~b & 0xFF for b in img)
    uniq = len(set(img))
    log("  unique=%d  nonzero=%d/7744" % (uniq, sum(1 for b in img if b != 0)))

    pgm_fn = os.path.join(OUT, "capture-%d.pgm"%ci)
    with open(pgm_fn, "wb") as f:
        f.write(b"P5\n88 88\n255\n" + inv)
    bin_fn = os.path.join(OUT, "capture-%d.bin"%ci)
    with open(bin_fn, "wb") as f:
        f.write(data)
    log("  保存: %s" % pgm_fn)
    captures.append({"idx": ci, "pgm": pgm_fn, "unique": uniq})

    sound("complete.oga")
    prompt("完成! %d/%d" % (ci, N_CAPTURES), "", "unique=%d" % uniq, "松开手指" if ci < N_CAPTURES else "完成!")
    time.sleep(1)

    if ci < N_CAPTURES:
        return_autopower()
        time.sleep(0.01)

close_prompt()
log("\n=== 汇总 ===")
for c in captures:
    log("  采集%d: %s  unique=%d" % (c["idx"], c["pgm"], c["unique"]))
os.close(fd)
log("\n产物: %s" % OUT)