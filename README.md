# FTE7001 / FT9338 Linux 指纹驱动

FocalTech **FT9338**（ACPI `_HID`: **FTE7001**）的 Linux 指纹传感器驱动，面向 One Mix 3 等 Cherry Trail / Braswell 平板。

**版本 v1.0 —— 冷启动全序列打通：S5 关机后无需 Windows 交棒，Linux 独立完成芯片初始化。**

88×88 像素 match-on-host 传感器（非 match-on-chip），508 DPI，有效面积约 4.4×4.4 mm。SPI 通信（Intel LPSS，Mode 0，1 MHz），GPIO85 复位（nRST），GPIO86 中断在 Linux 下不触发（驱动用轮询兜底）。

---

## 快速上手

本仓库只含驱动源码，不含 libfprint 本体。构建时从上游 fork 拉取 SPI 基础设施（见下文「构建集成」）：

```bash
# 1) 克隆（libfprint-fte3600 作为 git submodule 一并拉取）
git clone --recurse-submodules https://github.com/blacksamuraiiii/fte7001-linux-driver
cd fte7001-linux-driver/libfprint-fte3600

# 2) 复制驱动文件（含冷启动状态机与固件 blob）
cp ../driver/fte7001.{c,h} ../driver/ft9338-firmware.inc libfprint/drivers/

# 3) 应用构建集成 patch（登记 fte7001 + gpiod 依赖）
git apply ../driver/libfprint-integration.patch

# 4) 构建。注意 -Ddrivers=all 不含 optional 驱动，必须显式 all,fte7001
meson setup build -Ddrivers=all,fte7001 -Dudev_rules=disabled \
  -Dintrospection=false -Ddoc=false --prefix=/usr
ninja -C build

# 5) 测试
sudo LD_LIBRARY_PATH=build/libfprint build/examples/img-capture
```

**依赖**: `glib2-devel`, `gobject-introspection`, `meson`, `ninja`, `libgpiod >= 2.0`

### 构建集成

驱动是 libfprint 的 image driver，必须编进 libfprint 本体，不是独立内核模块。
构建基座是 **git submodule** `libfprint-fte3600`（[SamSeven777/libfprint-fte3600](https://github.com/SamSeven777/libfprint-fte3600)，
携带上游尚未合入的 SPI 传输层与 gpiod 辅助设施）：

```bash
git clone --recurse-submodules <本仓库>
# 或已克隆后补拉:
git submodule update --init
```

- `driver/libfprint-integration.patch`：改两处 meson 登记 + gpiod 依赖解析，把 fte7001 登记为可选 SPI 驱动
- `driver/ft9338-firmware.inc`：**FT9338 冷启动固件 blob**（14136 字节，C 数组头文件，详见下文「冷启动序列」）

### 仓库结构

```
├── README.md
├── .gitignore                        # 排除 feat/（本地研究文档）
├── .gitmodules                       # submodule: libfprint-fte3600
├── libfprint-fte3600/                # [submodule] 构建基座（SamSeven777 fork）
├── driver/
│   ├── fte7001.c                     # libfprint 驱动（含冷启动状态机）
│   ├── fte7001.h                     # 驱动头文件
│   ├── ft9338-firmware.inc           # FT9338 冷启动固件 blob（14136 B）
│   └── libfprint-integration.patch   # 构建系统集成（meson 登记 + gpiod）
├── tools/
│   ├── fp-unlock.py                  # 自研 PAM 解锁脚本（含 S5 冷启动自愈）
│   ├── ft9338-coldboot.py            # 冷启动全序列独立复刻（验证通过版）
│   ├── ft9338-c2-test.py             # C2 认型号变体实验
│   ├── ft9338-acut-probe.py          # A-CUT boot 三探针（严格只读）
│   ├── ft9338-ef-latch-check.py      # 0xEF 一次性 latch 复现
│   ├── ft9338-capture-diag.py        # 完整采图诊断（0x30 门闸 + 交替读）
│   ├── ft9338-rearm-test.py          # ReturnAutoPower 重挂验证
│   ├── ft9338-match-test.py          # 多帧采集 + 匹配验证
│   └── ft9338-baseline.py            # 无手指基线
└── LICENSE
```

---

## 冷启动序列（v1.0 核心突破）

**问题**：S5 关机（真断电）后，FT9338 停在 A-CUT ROM bootloader，不响应常规寄存器命令。
此前只有从 Windows 热重启交棒（RAM 易失域保留工作态）才能用——纯 Linux 冷启动后芯片「死态」。

**根因**（Windows 侧线上抓取 + 驱动反汇编，2026-10-06）：
1. **必须下发固件**：`05 FA` 单块 14136 B（非 ROM 自举，此前认知错误）
2. **下载后必须硬重启**：`04 FB` 校验通过后，Windows 还做了
   `Sleep(2) → rst 低 7ms → Sleep(10) → rst 低 7ms → Sleep(180)` ——缺这步固件永不执行
   （反汇编 `DownLoadFirewareInternal` 0x001665 证实；线上抓取探针的 4 断点恰好全部错过，只留 728ms 空隙）

**完整冷启动序列**（驱动 `img_open()` 自动执行，总耗时 ~4 s）：

```
① 探活:     90 00 00 → RX[2]==0xEF? (A-CUT 判定, EF 是一次性 latch, 只读一次)
② FE 检查:  ×5 轮 [GPIO 复位 10ms → 55 AA → 08F7 CB → 09F6 CB|0x20/FD/FE → 08F7 FE]
            FE≠0 → 芯片已有固件, 走暖路径
③ 退 sensor 模式: 10EF20 → 70×4 → 10EF20 (失败属预期)
④ 进下载模式: ≤3 轮 [rst 10ms → 55 AA → 09F6 C2 55 → 08F7 C2 == 0x55]
⑤ 建立下载模式: 09F6 C8 FF / CA FF / CB FF / B9 BF / B9 FF
⑥ 固件块写: 05 FA 00 00 37 38 <14136 B> 00  (~114ms @1MHz)
⑦ 回读校验: 04 FB 00 00 37 3A 00 → 14144 B, blob 逐字节匹配
⑧ ★芯片重启: Sleep(2) → rst低7ms→高 → Sleep(10) → rst低7ms→高 → Sleep(180)
⑨ 探活:     10 EF 20 → A5 5A (固件启动)
⑩ config init: 11EE 01 01 → 30 BB → 1F/1E ARM
```

**关键判据**（别被回显骗）：
- A-CUT 态所有 `08 F7` 短读 = MOSI 1 字节延迟回显（RX[n]==TX[n-1]），不是数据
- `55 AA` 之后命令模式才开，`08F7 CB` 首次回真值 `00 00 00 20`
- C2 认型号：读回 `rx[3]==0x55`（==写入值 = 命令模式确认；Windows 抓取的 `02 00 00 00` 是探针 buffer 陈旧值）
- `10 EF rr` 读值在 `RX[4]`（两条读路径偏移约定不同）
- 固件 blob：14136 B，头 10 字节 `02 32 b0 02 34 99 c2 86 02 35` 是固件本体的一部分

---

## 关键技术发现

- **0x1D 寄存器读会杀死 MCU**：单一帧格式连续轮询 2-3 次即死锁。解法：5B 短帧 + 16B 长帧交替读，间隔 50ms。
- **手指就绪判据是 5B 短帧 `RX[4]∈{0x01,0xA0}`**（Windows ISR 同款）；`11 11` / `01 01` 都只是早期瞬态或默认值，不可用。
- **0x30 门闸**：04FB 大读前必须读 0x30（验证 0xBB），否则芯片不执行图像读出。
- **采图流程**（工作态）：

```
Config init: 11EE 01 01 → 41 0F → 30 BB → 22 00 → 23 0E（2ms 间隔）
ARM:         11EE 1F 01 → 11EE 1E 01（2ms 间隔, 10ms settle）
手指检测:     5B+16B 交替读 0x1D，50ms 间隔，检测 5B RX[4]∈{0x01,0xA0}
门闸:         读 0x30 → 验证 0xBB
采图:         04 FB 34 00 1E 48 00 → 7752B 全双工 SPI（单笔, 不能拆两次）
解码:         XOR 0xFF on RX[8:7752] → 88×88 8-bit 灰度
重挂:         10 EF 20 → 11 EE 54 01 → 10 EF 20
```

- **图像格式**：88×88 8-bit 灰度，行主序；7744 B 图像 + 8 B 头 = 7752 B；解码 `pixels[i] = ~RX[8+i]`。

---

## 双链路架构

| 链路 | 组成 | S5 冷启动后 |
|---|---|---|
| **fprintd 链路**（enroll/verify/GNOME 集成） | libfprint 驱动（`driver/`） | ✅ 驱动 `img_open()` 自动冷启动 |
| **锁屏解锁链路**（Omarchy 锁屏） | PAM `omarchy-lock-fingerprint` → `tools/fp-unlock.py` → raw SPI | ✅ 握手失败时自动走 `cold_boot_sequence()` |

两条链路**独立运行、互不依赖**（解锁不走 fprintd）。`fp-unlock.py` 冷启动自愈需要
`/usr/local/libexec/ft9338-firmware.bin`（与本仓库 `driver/ft9338-firmware.inc` 同源，14136 B）。

首次登录界面（SDDM）目前仍用密码（未接 pam_fprintd）。

---

## 芯片状态机

```
                    ┌──────────────┐
                    │  A-CUT boot  │ ← S5 断电后自然态 (90 00 00 → 0xEF 一次性)
                    └──────┬───────┘
              GPIO复位+55AA│（冷启动序列 ①-⑧）
                    ┌──────▼───────┐
        ┌──────────►│  下载模式     │ 05FA 写固件 → 04FB 校验 → 双复位重启
        │           └──────────────┘
        │           ┌──────────────┐
        └─FE≠0─────┤   工作态      │ A5 5A + 0x9338, 采图/匹配可用
                    └──────────────┘
        S3 挂起 / 锁屏超时 → 工作态保持（易失域不断电）
        S5 断电 → 回 A-CUT boot（固件在 RAM, 断电即失）
```

**判活铁律**：`10 EF 20` → `RX[4:6] == A5 5A` = 工作态；`RX 全零` = 挂起（正常，唤醒即可）；
`命令回显`（RX[n]==TX[n-1]）= A-CUT boot 或挂起未唤醒。

---

## 芯片身份

- **ACPI _HID**: FTE7001（DSDT 别名，真实芯片为 FT9338）
- **OTP ID**: 0xBEBE（SPI 读：`04 FB 85 C0 00 00 00`）
- **Sensor ID**: 0x58 / 0x58（寄存器 0x14 / 0x15）
- **固件**: **RAM 加载型**（非 ROM 自举，S5 后需冷序列下发，14136 B）
- **家族**: FocalTech 93xx 系列（与 FT9361 高度同构；关键差异：无 0x76 CAPTURE_MODE 寄存器）

---

## 版本更新

### v1.0 (2026-10-06) —— 冷启动全序列打通
- **冷启动状态机进驱动**：`img_open()` 自动完成 A-CUT 判定 → FE 检查 → 进下载模式 → 05FA 固件下发 → 04FB 校验 → **芯片双复位重启**（反汇编 0x001665 证实的缺失拼图）→ config init
- **`ft9338-firmware.inc`**：14136 B 固件 blob 编入驱动（Windows 冷启动线上抓取逐字节校验，md5 `847a9beb…`）
- **C2 认型号判据修正**：`rx[3]==0x55`（读回==写入值 = 命令模式确认）
- **fp-unlock.py 冷启动自愈**：握手失败（非 A5 5A）时自动走完整冷序列，S5 后锁屏指纹无需 Windows 交棒
- **libfprint-fte3600 转正为 git submodule**（构建基座一键拉取）
- 工具脚本重命名统一 `ft9338-*` 前缀；清理过时产物（旧暖探活脚本、已证伪的复刻版本、superseded patch）

### v0.3 (2026-10-06)
- 暖探活收进驱动（后被证明前提错误，v1.0 重写）

### v0.2 (2026-10-05)
- 暖路径自愈脚本 + 开机服务（后被 v1.0 取代）

### v0.1 (2026-10-03)
- 初始发布：完整采图链路 + 自研 PAM 解锁方案

---

## 致谢

本驱动基于 **[SamSeven777/libfprint-fte3600](https://github.com/SamSeven777/libfprint-fte3600)** 的 FT9361 SPI 驱动开发，继承了其 init 状态机、寄存器布局和 SPI 传输模型。

---

## 许可证

LGPL-2.1-or-later（与 libfprint 兼容）
