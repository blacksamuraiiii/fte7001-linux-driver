# FTE7001 / FT9338 Linux 指纹驱动

FocalTech **FT9338**（ACPI `_HID`: **FTE7001**）的 Linux 指纹传感器驱动，面向 One Mix 3 等 Cherry Trail / Braswell 平板。

**状态：✅ 采图链路完整打通，自研解锁方案已在 Omarchy 锁屏中实际运行。**

88×88 像素 match-on-host 传感器（非 match-on-chip），508 DPI，有效面积约 4.4×4.4 mm。通过 SPI 通信（Intel LPSS，Mode 0，1 MHz），GPIO85 复位，GPIO86 在 Linux 下无中断（改用轮询兜底）。

---

## 快速上手

```bash
# 克隆带 SPI 基础设施的 libfprint fork
git clone https://github.com/SamSeven777/libfprint-fte3600
cd libfprint-fte3600

# 复制驱动文件
cp ../fte7001-linux-driver/driver/fte7001.{c,h} libfprint/drivers/

# 应用构建集成 patch
git apply ../fte7001-linux-driver/driver/libfprint-integration.patch

# 构建
meson setup builddir -Ddrivers=fte7001 -Dudev_rules=disabled -Dintrospection=false
ninja -C builddir

# 测试
sudo LD_LIBRARY_PATH=builddir/libfprint builddir/examples/img-capture
```

**依赖**: `glib2-devel`, `gobject-introspection`, `meson`, `ninja`, `libgpiod >= 2.0`

---

## 仓库结构

```
├── README.md
├── driver/
│   ├── fte7001.c                  # libfprint 驱动
│   ├── fte7001.h                  # 驱动头文件
│   └── libfprint-integration.patch # 构建系统集成
├── tools/
│   ├── fte7001-diag-p6.py         # 完整采图诊断（0x30 门闸 + 交替读）
│   ├── fte7001-expA-rearm.py      # ReturnAutoPower 重挂验证
│   ├── fte7001-expB-match.py      # 多帧采集 + 匹配验证
│   └── fte7001-baseline-nofinger.py # 无手指基线
└── LICENSE
```

---

## 关键技术发现

### 0x1D 寄存器读会杀死 MCU（关键）

FT9338 的 0x1D 寄存器有破坏性副作用：单一格式（5B 短帧或 16B 长帧单独轮询）读 2-3 次后 MCU 永久死锁（响应全零，需 GPIO85 复位恢复）。

**解法：5B 短帧 + 16B 长帧交替读，间隔 50ms。** 交替读保持 MCU 存活，实测 10 轮无死锁。500ms 间隔不够——MCU 在长间隔中会超时退出采集等待态。

### 就绪判据是 RX[4]==0x01，不是 11 11

- `11 11`（16B 长帧 RX[2:3]）只是「手指物理在位」的早期瞬态，此时采图得到空图（unique≈32）
- `RX[4]==0x01`（5B 短帧，Windows ISR 判据）才是「图像 RAM 已就绪」信号，此时采图得到真实指纹（unique 222-256）
- `01 01`（RX[2:3]）也不可靠——ARM 后无手指时默认就是 01 01

### "0x30 门闸"

04FB 大读命令被门控：必须在 04FB **之前**立即读寄存器 0x30（验证返回 0xBB），否则芯片不执行图像读出。这个发现在 Linux 侧把数周的死区捕获变成了真实指纹图像。

### 采图流程

```
唤醒: GPIO85 20ms 复位 → 0x70×2（4096B 填充时钟脉冲）
Config init: 11EE 01 01 → 41 0F → 30 BB → 22 00 → 23 0E（2ms 间隔）
ARM: 11EE 1F 01 → 11EE 1E 01（2ms 间隔, 10ms settle）
手指检测: 5B+16B 交替读 0x1D，50ms 间隔，检测 5B RX[4]∈{0x01,0xA0}
门闸:     读 0x30 → 验证 0xBB
采图:     04 FB 34 00 1E 48 00 → 7752B 全双工 SPI
解码:     XOR 0xFF on RX[8:7752] → 88×88 8-bit 灰度
重挂:     10 EF 20 → 11 EE 54 01 → 10 EF 20
```

### 图像格式

- 88 × 88 像素，8-bit 灰度，行主序，88 字节跨距
- 7744 字节图像数据 + 8 字节头 = **7752 字节**总计
- 解码：`pixels[i] = ~RX[8 + i]`（按位取反 / XOR 0xFF）
- 脊线周期：约 9 像素 = 0.45 mm @ 508 DPI

---

## 芯片身份

- **ACPI _HID**: FTE7001（DSDT 别名，真实芯片为 FT9338）
- **OTP ID**: 0xBEBE（SPI 读：`04 FB 85 C0 00 00 00`）
- **Sensor ID**: 0x58 / 0x58（寄存器 0x14 / 0x15）
- **固件**: ROM 型，无需上传，版本 0x3D，AGC 0x10
- **家族**: FocalTech 93xx 系列（与 FT9361 高度同构；关键差异：无 0x76 CAPTURE_MODE 寄存器）

---

## 致谢

本驱动基于 **[SamSeven777/libfprint-fte3600](https://github.com/SamSeven777/libfprint-fte3600)** 的 FT9361 SPI 驱动开发，继承了其 init 状态机、寄存器布局和 SPI 传输模型。

---

## 许可证

LGPL-2.1-or-later（与 libfprint 兼容）