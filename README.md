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

## 死态自愈（S5 冷启动后暖路径武装）

FT9338 固件烧在内部 ROM，上电自动加载。芯片死态的本质是「误码进回显循环」，nRST 热复位只复位 CPU 逻辑、不重新触发 ROM→RAM 固件加载，所以 GPIO 复位救不了死态——**只有真上电复位（POR = 断电再上电）能救**。

**纯 Linux 自愈 = S5 关机 → 开机等芯片自然启动（~60s）→ 暖路径武装**：

```bash
# 开机服务已固化 (fte7001-warm-boot.service)
# 等价手动流程:
sudo python3 tools/fp-warm-boot.py
```

`fp-warm-boot.py` 做两件事：
1. **探活**：`70`×2（write-only）+ `10 EF 20`，判 `RX[4:5]==A5 5A` 且版本 `0x16/0x17 == 0x9338`
2. **暖路径武装**：芯片已活 → 直接 `11EE 01 01 → 30 BB → 1F/1E`，置 0x30=0xBB（硬判据）

关键结论：
- **芯片在 Linux S5 后自然进工作态**（A5 5A + 0x9338），不需要任何冷启动激活、不需要 GPIO 复位、不需要 05 FA 写固件、不需要借 Windows
- **开机服务绝不能做 GPIO 复位**——那会打断芯片「上电 → ROM 固件加载 → 工作态」的自然过程。旧版 `fte7001-dead-wake.service`（boot 25s 做 GPIO 复位）就是「长时间不用必死态」的真凶
- 判据只看 `RX[4:5]==A5 5A`，`RX[0]`（0x70/0x80）无任何消费者，别拿它做判据
- 路径 2（write-only）帧：`55 AA`、`70` 走 WDF write-only，不是 full-duplex

## 版本更新

### v0.2 (2026-10-05)
- 新增 `tools/fp-warm-boot.py`：死态自愈临时脚本。S5 冷启动后探活芯片（`70`×2 write-only + `10 EF 20`），若芯片已在工作态（A5 5A + 版本 0x9338），直接 `init_and_arm` 武装；若未活则静默跳过
- 配套 `fte7001-warm-boot.service`：boot ~60s 自动执行脚本
- 根因定位：旧版开机服务在 boot ~20s 对芯片做 GPIO85 硬复位，打断芯片 S5 上电后「ROM 固件加载 → 工作态」的自然过程，导致死态
- 确认芯片电源轨在 S3 深睡时不断电，只有 S5 真断电（POR）能让芯片从 ROM 重新加载固件

### v0.1 (2026-10-03)
- 初始发布：FTE7001/FT9338 libfprint 驱动（`driver/`）
- 完整采图链路（88×88，0x30 门闸 + 0x1D 交替读）
- 自研 PAM 解锁方案（NCC 零均值归一化互相关 + ±5px 配准）
- 采图诊断工具（`tools/fte7001-diag-p6.py` 等）

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