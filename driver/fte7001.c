/*
 * FocalTech FTE7001 / FT9338 SPI fingerprint driver
 *
 * Based on fte3600.c.  FT9338 is a ROM-based, match-on-host sensor in the
 * same FocalTech 93xx family.  Key differences from FT9361:
 *   - ROM boots autonomously (no firmware upload)
 *   - No 0x76 CAPTURE_MODE register
 *   - 0x30 gate: read 0x30 (verify 0xBB) immediately before 04FB capture
 *   - Image: 88 x 88 = 7744 B  (vs. 64 x 80 = 5120 B)
 *   - Finger detection polls 0x1D (GPIO 86 never fires on Linux)
 *   - ReturnAutoPower rearm: 0x20 read → 0x54 write → 0x20 read
 *
 * SPDX-License-Identifier: LGPL-2.1-or-later
 */

#include "fte7001.h"
#include "drivers_api.h"
#include "fpi-image-device.h"

#include <errno.h>
#include <fcntl.h>
#include <gpiod.h>
#include <gudev/gudev.h>
#include <linux/spi/spidev.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/utsname.h>

/* Embedded FT9338 cold-boot firmware blob (Windows cold-boot capture verified, 14136 B) */
#include "ft9338-firmware.inc"

#define FP_COMPONENT "fte7001"

/* ----- GPIO profiles (OneMix3) ----- */
static const Fte7001GpioProfile fte7001_gpio_profiles[] = {
  { .sys_vendor = "One Netbook Technology Co., Ltd.",
    .product_name = "One Mix 3",
    .controller_acpi_path = "\\_SB_.PCI0.GPI0",
    .controller_hid = "INT3450",
    .reset_offset = 85,
    .irq_offset = 86,
  },
  { .sys_vendor = "Acidanthera",
    .product_name = "MacBookAir8,1",
    .controller_acpi_path = "\\_SB_.PCI0.GPI0",
    .controller_hid = "INT3450",
    .reset_offset = 85,
    .irq_offset = 86,
  },
  { NULL, NULL, NULL, NULL, 0, 0 }
};

const Fte7001GpioProfile *
fpi_fte7001_lookup_gpio_profile (const gchar *sys_vendor,
                                 const gchar *product_name)
{
  for (const Fte7001GpioProfile *p = fte7001_gpio_profiles; p->sys_vendor; p++)
    if (g_strcmp0 (sys_vendor, p->sys_vendor) == 0 &&
        g_strcmp0 (product_name, p->product_name) == 0)
      return p;
  return NULL;
}

/* ----- Device struct ----- */
struct _FpiDeviceFte7001
{
  FpImageDevice      parent;

  /* SPI */
  gint              spi_fd;
  guint8            small_rx[FT9338_SMALL_FRAME_SIZE];
  gboolean          small_rx_valid;
  guint8           *capture_tx;
  guint8           *capture_rx;
  gint64            capture_deadline;

  /* GPIO */
  const Fte7001GpioProfile *gpio_profile;
  struct gpiod_line_request *gpio_request;

  /* State */
  gboolean          idle_verified;
  gboolean          armed;
  guint             false_finger_count;
  FpiSsm           *task_ssm;

  /* Cold init */
  gboolean          cold_path;         /* TRUE when A-CUT boot detected */
  guint             fe_round;          /* FE check loop counter */
  guint16           chip_id;
  guint8           *fw_write_buf;      /* 05 FA transfer buffer (header+blob+tail) */
  guint8           *fw_readback_buf;   /* 04 FB RX buffer */

  /* Image */
  FpImage          *captured_image;
};

G_DECLARE_FINAL_TYPE (FpiDeviceFte7001, fpi_device_fte7001, FPI, DEVICE_FTE7001, FpImageDevice)
G_DEFINE_TYPE (FpiDeviceFte7001, fpi_device_fte7001, FP_TYPE_IMAGE_DEVICE)

/* ----- helpers ----- */
static inline gboolean
fte7001_acpi_path_equal (const gchar *path_a, const gchar *path_b)
{
  /* Urgent ACPI path comparison: normalised names, unequal lengths
   * always differ, and strings must match after that normalisation. */
  guint ia = 0, ib = 0;

  if (!path_a || !path_b) return FALSE;
  while (path_a[ia] == '\\') ia++;
  while (path_b[ib] == '\\') ib++;

  while (path_a[ia] && path_b[ib])
    {
      if (path_a[ia] == '\\') { ia++; continue; }
      if (path_b[ib] == '\\') { ib++; continue; }
      if (g_ascii_toupper (path_a[ia]) != g_ascii_toupper (path_b[ib]))
        return FALSE;
      ia++; ib++;
    }
  return path_a[ia] == path_b[ib];
}

static guint8
fte7001_read_result_byte (FpiDeviceFte7001 *self)
{
  return self->small_rx[FT9338_REG_READ_HEADER_SIZE];
}

static gboolean
fte7001_mcu_is_idle (FpiDeviceFte7001 *self)
{
  return self->small_rx_valid &&
         self->small_rx[FT9338_REG_READ_HEADER_SIZE] == 0xa5 &&
         self->small_rx[FT9338_REG_READ_HEADER_SIZE + 1] == 0x5a;
}

static void
fte7001_clear_captured_image (FpiDeviceFte7001 *self)
{
  if (self->captured_image)
    {
      g_clear_object (&self->captured_image);
      self->captured_image = NULL;
    }
}

/* ----- SPI transfer helpers ----- */
static void
fte7001_submit_transfer (FpiSsm *ssm, FpiSpiTransfer *transfer, gboolean cancellable)
{
  GCancellable *c = cancellable ? fpi_device_get_cancellable (fpi_ssm_get_device (ssm)) : NULL;

  fpi_spi_transfer_submit (transfer, c, fpi_ssm_spi_transfer_cb, NULL);
  transfer->ssm = ssm;
}

/* Write-only frame (path-2 equivalent): 55 AA (2B), 70 (1B), 05 FA.
 * No read phase — matches Windows WdfIoTargetSendWriteSynchronously. */
static void
fte7001_submit_write_only (FpiSsm *ssm, const guint8 *data, gsize len)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  self->small_rx_valid = FALSE;
  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, len);
  memcpy (transfer->buffer_wr, data, len);
  fte7001_submit_transfer (ssm, transfer, FALSE);
}

static void
fte7001_reg_read_cb (FpiSpiTransfer *transfer, FpDevice *device,
                     gpointer unused, GError *error)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (device);

  (void) unused;
  if (error)
    {
      fpi_ssm_mark_failed (transfer->ssm, g_error_copy (error));
      return;
    }
  self->small_rx_valid = TRUE;
  fpi_ssm_next_state (transfer->ssm);
}

/* 08 F7 <reg> 00 — short-config read (4B/4B full-duplex).
 * Value offset is register-dependent: CB in rx[3], C2 in rx[0]. */
static void
fte7001_submit_short_read (FpiSsm *ssm, guint8 reg, gboolean cancellable)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  self->small_rx_valid = FALSE;
  memset (self->small_rx, 0, 4);

  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, 4);
  transfer->buffer_wr[0] = 0x08;
  transfer->buffer_wr[1] = 0xf7;
  transfer->buffer_wr[2] = reg;
  transfer->buffer_wr[3] = 0x00;
  fpi_spi_transfer_read_full (transfer, self->small_rx, 4, NULL);
  fpi_spi_transfer_set_full_duplex (transfer, TRUE);
  transfer->ssm = ssm;
  fpi_spi_transfer_submit (
    transfer,
    cancellable ? fpi_device_get_cancellable (FP_DEVICE (self)) : NULL,
    fte7001_reg_read_cb, NULL);
}

/* 09 F6 <reg> <val> — short-config write.  The Windows capture shows all 09 F6 writes
 * as opcode=09f6 expRX=4 (full-duplex, response 00 00 00 00), NOT write-only.
 * Only 55 AA / 70 / 05 FA are write-only (PATH2). */
static void
fte7001_submit_short_write (FpiSsm *ssm, guint8 reg, guint8 val,
                            gboolean cancellable)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  self->small_rx_valid = FALSE;
  memset (self->small_rx, 0, 4);

  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, 4);
  transfer->buffer_wr[0] = 0x09;
  transfer->buffer_wr[1] = 0xf6;
  transfer->buffer_wr[2] = reg;
  transfer->buffer_wr[3] = val;
  fpi_spi_transfer_read_full (transfer, self->small_rx, 4, NULL);
  fpi_spi_transfer_set_full_duplex (transfer, TRUE);
  transfer->ssm = ssm;
  fpi_spi_transfer_submit (
    transfer,
    cancellable ? fpi_device_get_cancellable (FP_DEVICE (self)) : NULL,
    fte7001_reg_read_cb, NULL);
}

/* 90 00 00 — A-CUT boot detect (3B/3B full-duplex).  RX[2]==0xEF. */
static void
fte7001_submit_acut_detect (FpiSsm *ssm)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  self->small_rx_valid = FALSE;
  memset (self->small_rx, 0, 3);

  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, 3);
  transfer->buffer_wr[0] = 0x90;
  transfer->buffer_wr[1] = 0x00;
  transfer->buffer_wr[2] = 0x00;
  fpi_spi_transfer_read_full (transfer, self->small_rx, 3, NULL);
  fpi_spi_transfer_set_full_duplex (transfer, TRUE);
  transfer->ssm = ssm;
  fpi_spi_transfer_submit (
    transfer,
    fpi_device_get_cancellable (FP_DEVICE (self)),
    fte7001_reg_read_cb, NULL);
}

/* 05 FA block write (write-only, 14143 B = 6 header + blob + 1 tail). */
static void
fte7001_submit_firmware_write (FpiSsm *ssm)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write_full (transfer, self->fw_write_buf,
                               6 + FT9338_FW_BLOB_SIZE + 1, NULL);
  fte7001_submit_transfer (ssm, transfer, TRUE);
}

/* 04 FB readback (full-duplex, 14144 B).  TX 7-byte cmd + zero padding. */
static void
fte7001_submit_firmware_readback (FpiSsm *ssm)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  memset (self->fw_readback_buf, 0, FT9338_FW_READBACK_RX);
  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, FT9338_FW_READBACK_RX);
  transfer->buffer_wr[0] = 0x04;
  transfer->buffer_wr[1] = 0xfb;
  transfer->buffer_wr[2] = FT9338_FW_ADDR_HIGH;
  transfer->buffer_wr[3] = FT9338_FW_ADDR_LOW;
  transfer->buffer_wr[4] = 0x37;   /* len hi: 0x373A = 14138 */
  transfer->buffer_wr[5] = 0x3a;   /* len lo */
  transfer->buffer_wr[6] = 0x00;
  fpi_spi_transfer_read_full (transfer, self->fw_readback_buf,
                              FT9338_FW_READBACK_RX, NULL);
  fpi_spi_transfer_set_full_duplex (transfer, TRUE);
  fte7001_submit_transfer (ssm, transfer, TRUE);
}

static void
fte7001_submit_reg_read (FpiSsm *ssm, guint8 reg, guint8 result_len,
                         gboolean cancellable)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;
  gsize frame_len = FT9338_REG_READ_HEADER_SIZE + result_len;

  g_assert (frame_len <= sizeof (self->small_rx));
  self->small_rx_valid = FALSE;
  memset (self->small_rx, 0, frame_len);

  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, frame_len);
  transfer->buffer_wr[0] = 0x10;
  transfer->buffer_wr[1] = 0xef;
  transfer->buffer_wr[2] = reg;
  transfer->buffer_wr[3] = 0x00;
  fpi_spi_transfer_read_full (transfer, self->small_rx, frame_len, NULL);
  fpi_spi_transfer_set_full_duplex (transfer, TRUE);
  transfer->ssm = ssm;
  fpi_spi_transfer_submit (
    transfer,
    cancellable ? fpi_device_get_cancellable (FP_DEVICE (self)) : NULL,
    fte7001_reg_read_cb, NULL);
}

static void
fte7001_submit_reg_write (FpiSsm *ssm, guint8 reg, guint8 value,
                          gboolean cancellable)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  self->small_rx_valid = FALSE;
  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, FT9338_REG_WRITE_SIZE);
  transfer->buffer_wr[0] = 0x11;
  transfer->buffer_wr[1] = 0xee;
  transfer->buffer_wr[2] = reg;
  transfer->buffer_wr[3] = value;
  transfer->buffer_wr[4] = 0x00;
  fte7001_submit_transfer (ssm, transfer, cancellable);
}

static void
fte7001_submit_capture (FpiSsm *ssm)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;

  memset (self->capture_rx, 0, FT9338_CAPTURE_FRAME_SIZE);
  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write_full (transfer, self->capture_tx,
                               FT9338_CAPTURE_FRAME_SIZE, NULL);
  fpi_spi_transfer_read_full (transfer, self->capture_rx,
                              FT9338_CAPTURE_FRAME_SIZE, NULL);
  fpi_spi_transfer_set_full_duplex (transfer, TRUE);
  fpi_spi_transfer_set_sensitive (transfer, TRUE);
  fte7001_submit_transfer (ssm, transfer, TRUE);
}

/* ----- GPIO ----- */
static void
fte7001_set_hardware_reset (FpiSsm *ssm, FpiDeviceFte7001 *self, gboolean asserted)
{
  enum gpiod_line_value value;

  g_assert (self->gpio_request != NULL);
  g_assert (self->gpio_profile != NULL);

  /* GPIO 85 is active-low: raw 0 = assert reset */
  value = asserted ? GPIOD_LINE_VALUE_INACTIVE : GPIOD_LINE_VALUE_ACTIVE;
  self->idle_verified = FALSE;
  if (gpiod_line_request_set_value (self->gpio_request,
                                    self->gpio_profile->reset_offset, value) < 0)
    {
      fpi_ssm_mark_failed (ssm,
        g_error_new (G_IO_ERROR, g_io_error_from_errno (errno),
                     "fte7001 reset GPIO failed: %s", g_strerror (errno)));
      return;
    }
  fpi_ssm_next_state (ssm);
}

static void
fte7001_release_gpio (FpiDeviceFte7001 *self)
{
  if (self->gpio_request)
    {
      gpiod_line_request_release (self->gpio_request);
      self->gpio_request = NULL;
    }
}

/* ----- Probe / open / close ----- */
static gboolean
fte7001_probe_acpi (FpiDeviceFte7001 *self, const gchar *spidev_path)
{
  /* spidev_path is a devnode like "/dev/spidev0.0" — extract device name */
  const gchar *devname = strrchr (spidev_path, '/');
  devname = devname ? devname + 1 : spidev_path;

  g_autofree gchar *modalias_path = NULL;
  g_autofree gchar *modalias = NULL;
  g_autofree gchar *sys_vendor = NULL;
  g_autofree gchar *product_name = NULL;
  const Fte7001GpioProfile *profile;

  modalias_path = g_strdup_printf ("/sys/class/spidev/%s/device/modalias",
                                   devname);
  if (!g_file_get_contents (modalias_path, &modalias, NULL, NULL))
    return FALSE;
  if (!g_str_has_prefix (modalias, "acpi:FTE7001:"))
    return FALSE;

  if (!g_file_get_contents ("/sys/class/dmi/id/sys_vendor", &sys_vendor, NULL, NULL) ||
      !g_file_get_contents ("/sys/class/dmi/id/product_name", &product_name, NULL, NULL))
    return FALSE;

  g_strstrip (sys_vendor);
  g_strstrip (product_name);
  profile = fpi_fte7001_lookup_gpio_profile (sys_vendor, product_name);
  if (!profile)
    {
      fp_dbg ("FTE7001 unsupported DMI: %s / %s", sys_vendor, product_name);
      return FALSE;
    }

  self->gpio_profile = profile;
  return TRUE;
}

static gboolean
fte7001_determine_spidev_speed (FpiDeviceFte7001 *self, GError **error)
{
  guint32 speed = 0;

  /* FT9338 is only characterised at 1 MHz on this platform.  The
   * controller ceiling reported by spidev can be far higher (LPSS
   * controllers advertise tens of MHz); never program that — clamp to
   * the known-good rate from the header. */
  if (ioctl (self->spi_fd, SPI_IOC_RD_MAX_SPEED_HZ, &speed) != 0 ||
      speed == 0 || speed > FTE7001_SPI_SPEED_HZ)
    speed = FTE7001_SPI_SPEED_HZ;

  if (ioctl (self->spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed) != 0)
    {
      g_set_error (error, G_IO_ERROR, g_io_error_from_errno (errno),
                   "FTE7001: cannot set SPI speed %u Hz: %s",
                   speed, g_strerror (errno));
      return FALSE;
    }
  return TRUE;
}

static gboolean
fte7001_take_gpio (FpiDeviceFte7001 *self, GError **error)
{
  struct gpiod_line_settings *settings;
  struct gpiod_line_config *config;
  struct gpiod_request_config *req_cfg;
  struct gpiod_chip *chip;
  g_autofree gchar *chip_path = NULL;

  settings = gpiod_line_settings_new ();
  gpiod_line_settings_set_direction (settings, GPIOD_LINE_DIRECTION_OUTPUT);
  gpiod_line_settings_set_output_value (settings, GPIOD_LINE_VALUE_ACTIVE);

  config = gpiod_line_config_new ();
  gpiod_line_config_add_line_settings (config,
      &self->gpio_profile->reset_offset, 1, settings);
  req_cfg = gpiod_request_config_new ();
  gpiod_request_config_set_consumer (req_cfg, "fte7001");

  chip_path = g_strdup ("/dev/gpiochip0");
  chip = gpiod_chip_open (chip_path);
  if (!chip)
    {
      g_set_error (error, G_IO_ERROR, g_io_error_from_errno (errno),
                   "fte7001 could not open GPIO chip %s: %s",
                   chip_path, g_strerror (errno));
      gpiod_line_settings_free (settings);
      gpiod_line_config_free (config);
      gpiod_request_config_free (req_cfg);
      return FALSE;
    }

  self->gpio_request = gpiod_chip_request_lines (chip, req_cfg, config);
  /* The chip handle is only needed to create the request; close it on
   * every path so it does not leak (the request stays valid). */
  gpiod_chip_close (chip);
  gpiod_line_settings_free (settings);
  gpiod_line_config_free (config);
  gpiod_request_config_free (req_cfg);

  if (!self->gpio_request)
    {
      g_set_error (error, G_IO_ERROR, g_io_error_from_errno (errno),
                   "fte7001 could not claim GPIO chip %s: %s",
                   chip_path, g_strerror (errno));
      return FALSE;
    }
  return TRUE;
}

/* ----- State machine forward declarations and enums ----- */
static void fte7001_init_handler (FpiSsm *ssm, FpDevice *dev);
static void fte7001_init_complete (FpiSsm *ssm, FpDevice *dev, GError *error);

enum fte7001_init_state {
  /* Universal first probe: 90 00 00 3B full-duplex.
   * Preceded by warm-probe preamble (Windows capture frames 1-11):
   *   10 EF 20 + 70 wr ×4 + 10 EF 20/16/17
   * These frames establish initial chip state even though
   * the responses are garbage/echo on cold boot. */
  FTE7001_INIT_PREAMBLE_10EF20_1,
  FTE7001_INIT_PREAMBLE_70_1,
  FTE7001_INIT_PREAMBLE_70_2,
  FTE7001_INIT_PREAMBLE_70_3,
  FTE7001_INIT_PREAMBLE_70_4,
  FTE7001_INIT_PREAMBLE_10EF20_2,
  FTE7001_INIT_PREAMBLE_10EF16,
  FTE7001_INIT_PREAMBLE_10EF17,
  FTE7001_INIT_ACUT_DETECT,
  FTE7001_INIT_ACUT_CHECK,
  /* Warm path: chip already alive (10 EF 20 → A5 5A) */
  FTE7001_INIT_WARM_READ_MCU,
  FTE7001_INIT_WARM_CHECK_MCU,
  /* Cold path: hardware reset (assert 10 ms → deassert → settle 50 ms) */
  FTE7001_INIT_COLD_RESET_ASSERT,
  FTE7001_INIT_COLD_RESET_HOLD,
  FTE7001_INIT_COLD_RESET_DEASSERT,
  FTE7001_INIT_COLD_SETTLE,
  /* Cold path: 55 AA → 0xFE existence check loop (5 rounds) */
  FTE7001_INIT_COLD_55AA,
  FTE7001_INIT_COLD_FE_CB_READ,
  FTE7001_INIT_COLD_FE_CB_WRITE,
  FTE7001_INIT_COLD_FE_FD_WRITE,
  FTE7001_INIT_COLD_FE_FE_WRITE,
  FTE7001_INIT_COLD_FE_READ,
  FTE7001_INIT_COLD_FE_CHECK,
  FTE7001_INIT_COLD_FE_DELAY,
  /* Cold path: 10 EF 20 read + 70 wr ×2 alternation (Windows capture frames 43-48,
   * exit sensormode before download) ×3, then 55 AA #6 */
  /* The Windows capture has a D0Entry (D5→D0 power transition) between FE loop and the
   * 10EF20 block — it re-inits SPI+GPIO, i.e. one more GPIO reset. */
  FTE7001_INIT_COLD_D0_RESET_ASSERT,
  FTE7001_INIT_COLD_D0_RESET_HOLD,
  FTE7001_INIT_COLD_D0_RESET_DEASSERT,
  FTE7001_INIT_COLD_FW_10EF20_1,
  FTE7001_INIT_COLD_FW_70a_1,
  FTE7001_INIT_COLD_FW_70a_2,
  FTE7001_INIT_COLD_FW_10EF20_2,
  FTE7001_INIT_COLD_FW_70b_1,
  FTE7001_INIT_COLD_FW_70b_2,
  FTE7001_INIT_COLD_FW_10EF20_3,
  /* Cold path: 55 AA #6 — firmware-branch entry (after sensormode exit) */
  FTE7001_INIT_COLD_FW_55AA,
  /* Cold path: C2 identification (C2 rx[0]==0x02 → FT9338) */
  FTE7001_INIT_COLD_C2_WRITE,
  FTE7001_INIT_COLD_C2_READ,
  FTE7001_INIT_COLD_C2_CHECK,
  /* Cold path: download mode (09 F6 C8→CA→CB→B9×2) */
  FTE7001_INIT_COLD_DL_C8,
  FTE7001_INIT_COLD_DL_CA,
  FTE7001_INIT_COLD_DL_CB,
  FTE7001_INIT_COLD_DL_B9A,
  FTE7001_INIT_COLD_DL_B9B,
  /* Cold path: firmware 05 FA write + 04 FB readback */
  FTE7001_INIT_COLD_FW_WRITE,
  FTE7001_INIT_COLD_FW_READBACK,
  /* Cold path: chip restart after firmware download.
   * Windows DownLoadFirewareInternal 0x001665-0x0016AB (chip_type==1):
   *   Sleep(2) → rst pulse#1 (low 7 ms → high) → Sleep(10)
   *   → rst pulse#2 (low 7 ms → high) → Sleep(180) → probe.
   * Without this the written firmware never executes (verified: chip
   * stays silent after 05 FA + 04 FB unless hard-reset). */
  FTE7001_INIT_COLD_FW_RST2MS,
  FTE7001_INIT_COLD_FW_RST1_ASSERT,
  FTE7001_INIT_COLD_FW_RST1_HOLD,
  FTE7001_INIT_COLD_FW_RST1_DEASSERT,
  FTE7001_INIT_COLD_FW_RST10MS,
  FTE7001_INIT_COLD_FW_RST2_ASSERT,
  FTE7001_INIT_COLD_FW_RST2_HOLD,
  FTE7001_INIT_COLD_FW_RST2_DEASSERT,
  FTE7001_INIT_COLD_FW_RST180MS,
  /* Cold path: 10 EF 20 after firmware (Windows capture frame 60) */
  FTE7001_INIT_COLD_FW_10EF20,
  /* Cold path: sensor ID verify (0x14→0x58, 0x15→0x58) */
  FTE7001_INIT_COLD_SENSOR_H,
  FTE7001_INIT_COLD_SENSOR_L,
  /* Shared config init: 11 EE 01 → 41 0F → 30 BB → 22 00 → 23 0E */
  FTE7001_INIT_WRITE_01,
  FTE7001_INIT_DELAY_01,
  FTE7001_INIT_WRITE_41,
  FTE7001_INIT_DELAY_41,
  FTE7001_INIT_WRITE_30,
  FTE7001_INIT_DELAY_30,
  FTE7001_INIT_VERIFY_30,
  FTE7001_INIT_CHECK_30,
  FTE7001_INIT_WRITE_22,
  FTE7001_INIT_DELAY_22,
  FTE7001_INIT_WRITE_23,
  FTE7001_INIT_DELAY_23,
  FTE7001_INIT_FINAL_MCU,
  FTE7001_INIT_CHECK_FINAL,
  FTE7001_INIT_DONE,
  FTE7001_INIT_NSTATES,
};

static void
fte7001_img_open (FpImageDevice *dev)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);
  g_autofree gchar *spidev_path = NULL;
  GError *error = NULL;

  spidev_path = fpi_device_get_udev_data (FP_DEVICE (dev), FPI_DEVICE_UDEV_SUBTYPE_SPIDEV);
  if (!spidev_path || !fte7001_probe_acpi (self, spidev_path))
    {
      fpi_image_device_open_complete (dev,
        fpi_device_error_new_msg (FP_DEVICE_ERROR_NOT_SUPPORTED,
                                   "FTE7001: unknown or unsupported platform"));
      return;
    }

  if (!fte7001_take_gpio (self, &error))
    {
      fpi_image_device_open_complete (dev, error);
      return;
    }

  self->spi_fd = open (spidev_path, O_RDWR);
  if (self->spi_fd < 0)
    {
      /* Open failed before the SSM exists: the framework will not call
       * img_close, so release the GPIO claimed above ourselves. */
      fte7001_release_gpio (self);
      fpi_image_device_open_complete (dev,
        g_error_new (G_IO_ERROR, g_io_error_from_errno (errno),
                     "FTE7001: cannot open %s: %s", spidev_path,
                     g_strerror (errno)));
      return;
    }
  if (!fte7001_determine_spidev_speed (self, &error))
    {
      close (self->spi_fd);
      self->spi_fd = -1;
      fte7001_release_gpio (self);
      fpi_image_device_open_complete (dev, error);
      return;
    }

  /* Start init SSM: universal first probe (90 00 00 → A-CUT detect),
   * then branch warm/cold.  No 60 s window — cold boot detection is
   * sub-millisecond, per the Windows reference capture (first frame → work state 4.14 s). */
  self->cold_path = FALSE;
  self->fe_round = 0;
  self->chip_id = 0;
  {
    FpiSsm *ssm = fpi_ssm_new (FP_DEVICE (dev), fte7001_init_handler, FTE7001_INIT_NSTATES);
    self->task_ssm = ssm;
    fpi_ssm_start (ssm, fte7001_init_complete);
    return;
  }
}

static void
fte7001_img_close (FpImageDevice *dev)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);

  fte7001_release_gpio (self);
  if (self->spi_fd >= 0)
    {
      close (self->spi_fd);
      self->spi_fd = -1;
    }
  self->idle_verified = FALSE;
  self->armed = FALSE;
  fpi_image_device_close_complete (dev, NULL);
}

/* ----- State machines ----- */
static void fte7001_capture_handler (FpiSsm *ssm, FpDevice *dev);
static void fte7001_capture_complete (FpiSsm *ssm, FpDevice *dev, GError *error);

enum fte7001_capture_state {
  FTE7001_CAPTURE_ARM_1F,
  FTE7001_CAPTURE_ARM_1F_DELAY,
  FTE7001_CAPTURE_ARM_1E,
  FTE7001_CAPTURE_ARM_SETTLE,
  FTE7001_CAPTURE_POLL_5B,
  FTE7001_CAPTURE_CHECK_5B,
  FTE7001_CAPTURE_POLL_16B,
  FTE7001_CAPTURE_CHECK_16B,
  FTE7001_CAPTURE_GATE_READ_30,
  FTE7001_CAPTURE_GATE_CHECK_30,
  FTE7001_CAPTURE_READ_IMAGE,
  FTE7001_CAPTURE_PROCESS_IMAGE,
  FTE7001_CAPTURE_CLEANUP_READ_20,
  FTE7001_CAPTURE_CLEANUP_WRITE_54,
  FTE7001_CAPTURE_CLEANUP_READ_20B,
  FTE7001_CAPTURE_DONE,
  FTE7001_CAPTURE_NSTATES,
};

static gboolean
fte7001_fail_if_cancelled (FpiSsm *ssm, FpDevice *dev)
{
  GCancellable *cancellable;
  GError *error = NULL;

  if (!fpi_device_action_is_cancelled (dev))
    return FALSE;

  cancellable = fpi_device_get_cancellable (dev);
  if (!cancellable ||
      !g_cancellable_set_error_if_cancelled (cancellable, &error))
    error = g_error_new_literal (G_IO_ERROR, G_IO_ERROR_CANCELLED,
                                 "Fingerprint operation was cancelled");
  fpi_ssm_mark_failed (ssm, error);
  return TRUE;
}

/* ----- Init SSM ----- */
static void
fte7001_init_handler (FpiSsm *ssm, FpDevice *dev)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);

  switch (fpi_ssm_get_cur_state (ssm))
    {
    /* --- Warm-probe preamble (Windows capture frames 1-11): 10EF20 + 70×4 + 10EF20/16/17 --- */
    case FTE7001_INIT_PREAMBLE_10EF20_1:
    case FTE7001_INIT_PREAMBLE_10EF20_2:
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 2, TRUE);
      return;
    case FTE7001_INIT_PREAMBLE_70_1:
    case FTE7001_INIT_PREAMBLE_70_2:
    case FTE7001_INIT_PREAMBLE_70_3:
    case FTE7001_INIT_PREAMBLE_70_4:
      {
        static const guint8 cmd70[1] = { 0x70 };
        fte7001_submit_write_only (ssm, cmd70, 1);
        return;
      }
    case FTE7001_INIT_PREAMBLE_10EF16:
      fte7001_submit_reg_read (ssm, FT9338_REG_CHIP_ID_HIGH, 1, TRUE);
      return;
    case FTE7001_INIT_PREAMBLE_10EF17:
      fte7001_submit_reg_read (ssm, FT9338_REG_CHIP_ID_LOW, 1, TRUE);
      return;

    /* --- Universal first probe: 90 00 00 → A-CUT detection (Windows cold-boot capture, 2026-10-06) --- */
    case FTE7001_INIT_ACUT_DETECT:
      fte7001_submit_acut_detect (ssm);
      return;
    case FTE7001_INIT_ACUT_CHECK:
      if (self->small_rx_valid && self->small_rx[2] == 0xef)
        {
          self->cold_path = TRUE;
          fp_dbg ("FT9338 A-CUT boot (EF) → cold path");
          fpi_ssm_jump_to_state (ssm, FTE7001_INIT_COLD_RESET_ASSERT);
          return;
        }
      fpi_ssm_jump_to_state (ssm, FTE7001_INIT_WARM_READ_MCU);
      return;

    /* --- Warm path: chip already alive → MCU idle check --- */
    case FTE7001_INIT_WARM_READ_MCU:
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 2, TRUE);
      return;
    case FTE7001_INIT_WARM_CHECK_MCU:
      if (fte7001_mcu_is_idle (self))
        {
          fp_dbg ("FT9338 warm path: MCU idle → skip cold");
          fpi_ssm_jump_to_state (ssm, FTE7001_INIT_WRITE_01);
          return;
        }
      /* Not idle → go cold */
      fpi_ssm_jump_to_state (ssm, FTE7001_INIT_COLD_RESET_ASSERT);
      return;

    /* --- Cold: GPIO reset 10 ms → deassert → settle 50 ms --- */
    case FTE7001_INIT_COLD_RESET_ASSERT:
      fte7001_set_hardware_reset (ssm, self, TRUE);
      return;
    case FTE7001_INIT_COLD_RESET_HOLD:
      fpi_ssm_next_state_delayed (ssm, FT9338_COLD_RESET_PULSE_MS);
      return;
    case FTE7001_INIT_COLD_RESET_DEASSERT:
      fte7001_set_hardware_reset (ssm, self, FALSE);
      return;
    case FTE7001_INIT_COLD_SETTLE:
      /* Windows sequence: deassert → immediately 55 AA, no settle delay. */
      fpi_ssm_jump_to_state (ssm, FTE7001_INIT_COLD_55AA);
      return;

    /* --- Cold: 55 AA (command mode) → 0xFE existence check loop --- */
    case FTE7001_INIT_COLD_55AA:
      {
        static const guint8 aa55[2] = { 0x55, 0xaa };
        fte7001_submit_write_only (ssm, aa55, 2);
        return;
      }
    case FTE7001_INIT_COLD_FE_CB_READ:
      fte7001_submit_short_read (ssm, FT9338_REG_CB, TRUE);
      return;
    case FTE7001_INIT_COLD_FE_CB_WRITE:
      fte7001_submit_short_write (ssm, FT9338_REG_CB, 0x20, TRUE);
      return;
    case FTE7001_INIT_COLD_FE_FD_WRITE:
      fte7001_submit_short_write (ssm, FT9338_REG_FD, 0x11, TRUE);
      return;
    case FTE7001_INIT_COLD_FE_FE_WRITE:
      fte7001_submit_short_write (ssm, FT9338_REG_FE, 0x11, TRUE);
      return;
    case FTE7001_INIT_COLD_FE_READ:
      fte7001_submit_short_read (ssm, FT9338_REG_FE, TRUE);
      return;
    case FTE7001_INIT_COLD_FE_CHECK:
      /* 08 F7 responses come in two flavours:
       *   Real data:  rx[1]==0x00 rx[2]==0x00 rx[3]==<value>
       *   Echo:       rx[1]==0x08 rx[2]==0xf7 rx[3]==<reg_byte>
       * Reference capture round 4 was echo 'aa 08 f7 fe' — rx[3]==0xfe was fake.
       * Discard echo before trusting FE != 0. */
      fp_dbg ("FT9338 FE raw: %02x %02x %02x %02x (round %u)",
              self->small_rx[0], self->small_rx[1],
              self->small_rx[2], self->small_rx[3], self->fe_round + 1);
      if (self->small_rx_valid
          && self->small_rx[1] == 0x08 && self->small_rx[2] == 0xf7)
        {
          fp_dbg ("FT9338 FE=0x%02x (echo, discard)", self->small_rx[3]);
          /* fall through to exhausted check below */
        }
      else if (self->small_rx_valid && self->small_rx[3] != 0)
        {
          fp_dbg ("FT9338 FE=0x%02x → chip has firmware, go warm",
                  self->small_rx[3]);
          fpi_ssm_jump_to_state (ssm, FTE7001_INIT_WRITE_01);
          return;
        }
      if (self->fe_round >= FT9338_FE_CHECK_ROUNDS - 1)
        {
          fp_dbg ("FT9338 FE exhausted (%u rounds) → download",
                  self->fe_round + 1);
          fpi_ssm_jump_to_state (ssm, FTE7001_INIT_COLD_D0_RESET_ASSERT);
          return;
        }
      fpi_ssm_jump_to_state (ssm, FTE7001_INIT_COLD_FE_DELAY);
      return;
    case FTE7001_INIT_COLD_FE_DELAY:
      /* Continue FE loop without GPIO reset.  The 01 §5 pseudo-code
       * reserves GPIO reset for step ② only (once, before 55 AA).
       * Do NOT reset per round — keeping command mode across rounds
       * is required for C2 identification to trigger. */
      fpi_ssm_jump_to_state_delayed (ssm, FTE7001_INIT_COLD_55AA,
                                     FT9338_FE_CHECK_DELAY_MS);
      self->fe_round++;
      return;

    /* --- Cold: D0Entry GPIO reset (Windows D5-to-D0 power transition, before 10EF20) --- */
    case FTE7001_INIT_COLD_D0_RESET_ASSERT:
      fte7001_set_hardware_reset (ssm, self, TRUE);
      return;
    case FTE7001_INIT_COLD_D0_RESET_HOLD:
      fpi_ssm_next_state_delayed (ssm, FT9338_COLD_RESET_PULSE_MS);
      return;
    case FTE7001_INIT_COLD_D0_RESET_DEASSERT:
      fte7001_set_hardware_reset (ssm, self, FALSE);
      return;

    /* --- Cold: 10 EF 20 read + 70 wr ×2 alternation ×3 (Windows capture frames 43-48) --- */
    case FTE7001_INIT_COLD_FW_10EF20_1:
    case FTE7001_INIT_COLD_FW_10EF20_2:
    case FTE7001_INIT_COLD_FW_10EF20_3:
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 2, TRUE);
      return;
    case FTE7001_INIT_COLD_FW_70a_1:
    case FTE7001_INIT_COLD_FW_70a_2:
    case FTE7001_INIT_COLD_FW_70b_1:
    case FTE7001_INIT_COLD_FW_70b_2:
      {
        static const guint8 cmd70[1] = { 0x70 };
        fte7001_submit_write_only (ssm, cmd70, 1);
        return;
      }

    /* --- Cold: 55 AA #6 firmware-branch entry (Windows capture frame 50) --- */
    case FTE7001_INIT_COLD_FW_55AA:
      {
        static const guint8 aa55[2] = { 0x55, 0xaa };
        fte7001_submit_write_only (ssm, aa55, 2);
        return;
      }

    /* --- Cold: C2 identification (0x02 → FT9338) --- */
    case FTE7001_INIT_COLD_C2_WRITE:
      fte7001_submit_short_write (ssm, FT9338_REG_C2, 0x55, TRUE);
      return;
    case FTE7001_INIT_COLD_C2_READ:
      fte7001_submit_short_read (ssm, FT9338_REG_C2, TRUE);
      return;
    case FTE7001_INIT_COLD_C2_CHECK:
      fp_dbg ("FT9338 C2 raw: %02x %02x %02x %02x",
              self->small_rx[0], self->small_rx[1],
              self->small_rx[2], self->small_rx[3]);
      /* Windows IC_EnterDownloadMode (0x1CE4) success criterion is
       * readback == written value (0x55): confirms command mode.  The
       * The Windows capture's "02 00 00 00" was stale small-frame buffer content,
       * not this chip's answer.  Linux verified: 00 00 00 55 on success. */
      if (!self->small_rx_valid || self->small_rx[3] != 0x55)
        {
          fpi_ssm_mark_failed (ssm,
            fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
              "FT9338 C2 check failed: rx[3]=0x%02x (want 0x55)",
              self->small_rx_valid ? self->small_rx[3] : 0xff));
          return;
        }
      fp_dbg ("FT9338 C2=0x55 command mode confirmed");
      fpi_ssm_next_state (ssm);
      return;

    /* --- Cold: download mode --- */
    case FTE7001_INIT_COLD_DL_C8:
      fte7001_submit_short_write (ssm, FT9338_REG_C8, 0xff, TRUE);
      return;
    case FTE7001_INIT_COLD_DL_CA:
      fte7001_submit_short_write (ssm, FT9338_REG_CA, 0xff, TRUE);
      return;
    case FTE7001_INIT_COLD_DL_CB:
      fte7001_submit_short_write (ssm, FT9338_REG_CB, 0xff, TRUE);
      return;
    case FTE7001_INIT_COLD_DL_B9A:
      fte7001_submit_short_write (ssm, FT9338_REG_B9, 0xbf, TRUE);
      return;
    case FTE7001_INIT_COLD_DL_B9B:
      fte7001_submit_short_write (ssm, FT9338_REG_B9, 0xff, TRUE);
      return;

    /* --- Cold: firmware 05 FA block write (write-only) --- */
    case FTE7001_INIT_COLD_FW_WRITE:
      fp_dbg ("FT9338 downloading firmware (%u B) …", FT9338_FW_BLOB_SIZE);
      fte7001_submit_firmware_write (ssm);
      return;

    /* --- Cold: firmware 04 FB readback verify (full-duplex) --- */
    case FTE7001_INIT_COLD_FW_READBACK:
      fte7001_submit_firmware_readback (ssm);
      return;

    /* --- Cold: chip restart sequence (Windows 0x001665, chip_type==1) ---
     * Sleep(2) → rst(7ms) → Sleep(10) → rst(7ms) → Sleep(180).
     * The 04 FB readback leaves the chip in download mode; only this
     * double hard-reset makes the freshly written firmware execute. */
    case FTE7001_INIT_COLD_FW_RST2MS:
      fpi_ssm_next_state_delayed (ssm, 2);
      return;
    case FTE7001_INIT_COLD_FW_RST1_ASSERT:
      fte7001_set_hardware_reset (ssm, self, TRUE);
      return;
    case FTE7001_INIT_COLD_FW_RST1_HOLD:
      fpi_ssm_next_state_delayed (ssm, FT9338_FW_RESTART_PULSE_MS);
      return;
    case FTE7001_INIT_COLD_FW_RST1_DEASSERT:
      fte7001_set_hardware_reset (ssm, self, FALSE);
      return;
    case FTE7001_INIT_COLD_FW_RST10MS:
      fpi_ssm_next_state_delayed (ssm, 10);
      return;
    case FTE7001_INIT_COLD_FW_RST2_ASSERT:
      fte7001_set_hardware_reset (ssm, self, TRUE);
      return;
    case FTE7001_INIT_COLD_FW_RST2_HOLD:
      fpi_ssm_next_state_delayed (ssm, FT9338_FW_RESTART_PULSE_MS);
      return;
    case FTE7001_INIT_COLD_FW_RST2_DEASSERT:
      fte7001_set_hardware_reset (ssm, self, FALSE);
      return;
    case FTE7001_INIT_COLD_FW_RST180MS:
      /* chip_type==1 (FT9338): Sleep(180) before probing */
      fpi_ssm_next_state_delayed (ssm, FT9338_FW_BOOT_DELAY_MS);
      return;

    /* --- Cold: 10 EF 20 after firmware (Windows capture frame 60 — triggers download success) --- */
    case FTE7001_INIT_COLD_FW_10EF20:
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 2, TRUE);
      return;

    /* --- Cold: sensor ID sanity --- */
    case FTE7001_INIT_COLD_SENSOR_H:
      fte7001_submit_reg_read (ssm, FT9338_REG_SENSOR_ID_HIGH, 1, TRUE);
      return;
    case FTE7001_INIT_COLD_SENSOR_L:
      if (self->small_rx_valid)
        fp_dbg ("FT9338 sensor id high=0x%02x …",
                fte7001_read_result_byte (self));
      fte7001_submit_reg_read (ssm, FT9338_REG_SENSOR_ID_LOW, 1, TRUE);
      return;
    /* auto-advances to WRITE_01 */
    case FTE7001_INIT_WRITE_01:
      fte7001_submit_reg_write (ssm, 0x01, 0x01, TRUE);
      return;
    case FTE7001_INIT_DELAY_01:
    case FTE7001_INIT_DELAY_41:
    case FTE7001_INIT_DELAY_30:
    case FTE7001_INIT_DELAY_22:
    case FTE7001_INIT_DELAY_23:
      fpi_ssm_next_state_delayed (ssm, FT9338_CONFIG_DELAY_MS);
      return;
    case FTE7001_INIT_WRITE_41:
      fte7001_submit_reg_write (ssm, FT9338_REG_CONFIG_41, 0x0f, TRUE);
      return;
    case FTE7001_INIT_WRITE_30:
      fte7001_submit_reg_write (ssm, FT9338_REG_CONFIG_MARKER, 0xbb, TRUE);
      return;
    case FTE7001_INIT_VERIFY_30:
      fte7001_submit_reg_read (ssm, FT9338_REG_CONFIG_MARKER, 1, TRUE);
      return;
    case FTE7001_INIT_CHECK_30:
      if (fte7001_read_result_byte (self) != 0xbb)
        {
          fpi_ssm_mark_failed (ssm,
            fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
              "FT9338 config marker verify failed (%02x)",
              fte7001_read_result_byte (self)));
          return;
        }
      fpi_ssm_next_state (ssm);
      return;
    case FTE7001_INIT_WRITE_22:
      fte7001_submit_reg_write (ssm, FT9338_REG_CONFIG_22, 0x00, TRUE);
      return;
    case FTE7001_INIT_WRITE_23:
      fte7001_submit_reg_write (ssm, FT9338_REG_CONFIG_23, 0x0e, TRUE);
      return;
    case FTE7001_INIT_FINAL_MCU:
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 2, TRUE);
      return;
    case FTE7001_INIT_CHECK_FINAL:
      if (!fte7001_mcu_is_idle (self))
        {
          fpi_ssm_mark_failed (ssm,
            fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
              "FT9338 MCU not idle after config init (%02x %02x)",
              self->small_rx[4], self->small_rx[5]));
          return;
        }
      self->idle_verified = TRUE;
      fpi_ssm_next_state (ssm);
      return;
    case FTE7001_INIT_DONE:
      fpi_ssm_mark_completed (ssm);
      return;
    case FTE7001_INIT_NSTATES:
      g_assert_not_reached ();
    }
}

static void
fte7001_init_complete (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);

  self->task_ssm = NULL;
  if (error)
    {
      /* Init failed: the framework will not call img_close after a
       * failed open, so undo img_open's claims here (fd + GPIO). */
      self->idle_verified = FALSE;
      if (self->spi_fd >= 0)
        {
          close (self->spi_fd);
          self->spi_fd = -1;
        }
      fte7001_release_gpio (self);
      fpi_image_device_open_complete (FP_IMAGE_DEVICE (dev), error);
      return;
    }
  fpi_image_device_open_complete (FP_IMAGE_DEVICE (dev), NULL);
}

/* ----- Capture SSM ----- */
static void
fte7001_capture_handler (FpiSsm *ssm, FpDevice *dev)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);
  guint8 finger_status;

  switch (fpi_ssm_get_cur_state (ssm))
    {
    case FTE7001_CAPTURE_ARM_1F:
      if (fte7001_fail_if_cancelled (ssm, dev)) return;
      fte7001_submit_reg_write (ssm, FT9338_REG_CAPTURE_ENABLE, 0x01, TRUE);
      return;
    case FTE7001_CAPTURE_ARM_1F_DELAY:
      fpi_ssm_next_state_delayed (ssm, FT9338_ARM_DELAY_MS);
      return;
    case FTE7001_CAPTURE_ARM_1E:
      fte7001_submit_reg_write (ssm, FT9338_REG_CAPTURE_START, 0x01, TRUE);
      return;
    case FTE7001_CAPTURE_ARM_SETTLE:
      fpi_ssm_next_state_delayed (ssm, FT9338_ARM_SETTLE_MS);
      return;

    case FTE7001_CAPTURE_POLL_5B:
      if (fte7001_fail_if_cancelled (ssm, dev)) return;
      if (g_get_monotonic_time () >= self->capture_deadline)
        {
          fpi_ssm_mark_failed (ssm,
            g_error_new_literal (G_IO_ERROR, G_IO_ERROR_TIMED_OUT,
              "FT9338 timed out waiting for finger"));
          return;
        }
      /* 5B short frame: single result byte = image-ready signal. */
      fte7001_submit_reg_read (ssm, FT9338_REG_FINGER_STATUS, 1, TRUE);
      return;
    case FTE7001_CAPTURE_CHECK_5B:
      /* Windows ISR criterion: RX[4] == 0x01 (or 0xA0) = image RAM ready.
       * NOT RX[2:3] == 11 11 (that only means the finger is physically
       * present — capturing then reads an empty image). */
      finger_status =
        self->small_rx[4] == 0x01 || self->small_rx[4] == 0xa0;
      g_debug ("FT9338 0x1D 5B: %02x %02x %02x %02x %02x  ready=%d",
               self->small_rx[0], self->small_rx[1], self->small_rx[2],
               self->small_rx[3], self->small_rx[4], finger_status);
      if (finger_status)
        {
          self->false_finger_count = 0;
          fpi_image_device_report_finger_status (FP_IMAGE_DEVICE (dev), TRUE);
          fpi_ssm_jump_to_state (ssm, FTE7001_CAPTURE_GATE_READ_30);
          return;
        }
      /* Not ready → 16B read keeps the MCU alive, then poll again. */
      fpi_ssm_jump_to_state (ssm, FTE7001_CAPTURE_POLL_16B);
      return;
    case FTE7001_CAPTURE_POLL_16B:
      /* 16B long frame.  FT9338's 0x1D register kills the MCU when polled
       * with a single frame format (5B or 16B alone → all-zero after 2-3
       * reads).  Alternating 5B/16B keeps it alive; this read is the
       * "antidote" and its payload is not used for detection. */
      fte7001_submit_reg_read (ssm, FT9338_REG_FINGER_STATUS, 12, TRUE);
      return;
    case FTE7001_CAPTURE_CHECK_16B:
      self->false_finger_count++;
      fpi_ssm_jump_to_state_delayed (ssm, FTE7001_CAPTURE_POLL_5B,
                                     FT9338_IRQ_FALLBACK_POLL_MS);
      return;

    case FTE7001_CAPTURE_GATE_READ_30:
      /* Critical 0x30 gate — must be read immediately before 04FB */
      fte7001_submit_reg_read (ssm, FT9338_REG_CONFIG_MARKER, 1, TRUE);
      return;
    case FTE7001_CAPTURE_GATE_CHECK_30:
      if (fte7001_read_result_byte (self) != 0xbb)
        {
          fpi_ssm_mark_failed (ssm,
            fpi_device_error_new_msg (FP_DEVICE_ERROR_PROTO,
              "FT9338 config marker lost before capture (%02x)",
              fte7001_read_result_byte (self)));
          return;
        }
      fpi_ssm_next_state (ssm);
      return;

    case FTE7001_CAPTURE_READ_IMAGE:
      fte7001_submit_capture (ssm);
      return;
    case FTE7001_CAPTURE_PROCESS_IMAGE:
      fte7001_clear_captured_image (self);
      self->captured_image =
        fp_image_new (FT9338_IMAGE_WIDTH, FT9338_IMAGE_HEIGHT);
      self->captured_image->ppmm = FT9338_IMAGE_PPMM;
      self->captured_image->flags |= FPI_IMAGE_PARTIAL;

      for (gsize i = 0; i < FT9338_IMAGE_SIZE; i++)
        self->captured_image->data[i] =
          (guint8) ~self->capture_rx[FT9338_CAPTURE_DATA_OFFSET + i];

      /* Secure-clear the raw SPI buffer */
      memset (self->capture_rx, 0, FT9338_CAPTURE_FRAME_SIZE);
      fpi_ssm_next_state (ssm);
      return;

    case FTE7001_CAPTURE_CLEANUP_READ_20:
      /* ReturnAutoPower: 10 EF 20 → 11 EE 54 01 → 10 EF 20 */
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 1, TRUE);
      return;
    case FTE7001_CAPTURE_CLEANUP_WRITE_54:
      fte7001_submit_reg_write (ssm, FT9338_REG_QUICK_TRIGGER, 0x01, TRUE);
      return;
    case FTE7001_CAPTURE_CLEANUP_READ_20B:
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 2, TRUE);
      return;

    case FTE7001_CAPTURE_DONE:
      fpi_ssm_mark_completed (ssm);
      return;
    case FTE7001_CAPTURE_NSTATES:
      g_assert_not_reached ();
    }
}

static void
fte7001_capture_complete (FpiSsm *ssm, FpDevice *dev, GError *error)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);

  self->task_ssm = NULL;
  if (self->captured_image)
    {
      /* image_captured takes ownership of img; do NOT unref again. */
      FpImage *img = g_steal_pointer (&self->captured_image);
      fpi_image_device_image_captured (FP_IMAGE_DEVICE (dev), img);
    }
  else
    {
      fpi_image_device_session_error (FP_IMAGE_DEVICE (dev),
        error ? g_error_copy (error) :
        fpi_device_error_new_msg (FP_DEVICE_ERROR_GENERAL,
          "FT9338 capture produced no image"));
    }
}

static void
fte7001_start_capture (FpiDeviceFte7001 *self)
{
  FpiSsm *ssm;

  fte7001_clear_captured_image (self);
  ssm = fpi_ssm_new (FP_DEVICE (self), fte7001_capture_handler,
                     FTE7001_CAPTURE_NSTATES);
  self->task_ssm = ssm;
  self->capture_deadline =
    g_get_monotonic_time () + (gint64) FT9338_FINGER_TIMEOUT_MS * 1000;
  fpi_ssm_start (ssm, fte7001_capture_complete);
}

/* ----- Action entry points (FpImageDevice) ----- */
static void
fte7001_activate (FpImageDevice *dev)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);
  fte7001_start_capture (self);
  fpi_image_device_activate_complete (dev, NULL);
}

static void
fte7001_deactivate (FpImageDevice *dev)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (dev);

  if (self->task_ssm)
    {
      GCancellable *cancellable = fpi_device_get_cancellable (FP_DEVICE (dev));
      if (cancellable)
        g_cancellable_cancel (cancellable);
    }
  fpi_image_device_deactivate_complete (dev, NULL);
}

/* ----- Class init ----- */
static void
fpi_device_fte7001_init (FpiDeviceFte7001 *self)
{
  self->spi_fd = -1;
  self->capture_tx = g_malloc0 (FT9338_CAPTURE_FRAME_SIZE);
  self->capture_rx = g_malloc0 (FT9338_CAPTURE_FRAME_SIZE);

  /* 04 FB 34 00 1E 48 00 — address 0x3400, length 0x1E48 = 7752 */
  self->capture_tx[0] = 0x04;
  self->capture_tx[1] = 0xfb;
  self->capture_tx[2] = 0x34;  /* addr high */
  self->capture_tx[3] = 0x00;  /* addr low  */
  self->capture_tx[4] = 0x1e;  /* len high  */
  self->capture_tx[5] = 0x48;  /* len low   */

  /* Firmware write buffer: 6 (header) + blob + 1 (tail 0x00) */
  self->fw_write_buf = g_malloc0 (6 + FT9338_FW_BLOB_SIZE + 1);
  self->fw_write_buf[0] = 0x05;       /* 05 FA */
  self->fw_write_buf[1] = 0xFA;
  self->fw_write_buf[2] = FT9338_FW_ADDR_HIGH;
  self->fw_write_buf[3] = FT9338_FW_ADDR_LOW;
  self->fw_write_buf[4] = 0x37;       /* len hi: 0x3738 = 14136 */
  self->fw_write_buf[5] = 0x38;       /* len lo */
  memcpy (self->fw_write_buf + 6, ft9338_firmware_blob, FT9338_FW_BLOB_SIZE);
  self->fw_write_buf[6 + FT9338_FW_BLOB_SIZE] = 0x00;  /* tail */

  /* Firmware readback RX buffer */
  self->fw_readback_buf = g_malloc0 (FT9338_FW_READBACK_RX);
}

static void
fpi_device_fte7001_finalize (GObject *object)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (object);

  if (self->spi_fd >= 0) close (self->spi_fd);
  self->spi_fd = -1;
  fte7001_release_gpio (self);
  g_free (self->capture_tx);
  g_free (self->capture_rx);
  g_free (self->fw_write_buf);
  g_free (self->fw_readback_buf);
  fte7001_clear_captured_image (self);

  G_OBJECT_CLASS (fpi_device_fte7001_parent_class)->finalize (object);
}

static void
fpi_device_fte7001_class_init (FpiDeviceFte7001Class *klass)
{
  FpDeviceClass      *dev_class = FP_DEVICE_CLASS (klass);
  FpImageDeviceClass *img_class = FP_IMAGE_DEVICE_CLASS (klass);

  dev_class->id       = "fte7001";
  dev_class->full_name = "FocalTech FT9338 Embedded Fingerprint Sensor";
  dev_class->type     = FP_DEVICE_TYPE_UDEV;
  dev_class->id_table = fte7001_id_table;
  dev_class->scan_type = FP_SCAN_TYPE_PRESS;
  dev_class->temp_hot_seconds = -1;
  dev_class->nr_enroll_stages = 7;

  dev_class->probe  = NULL;  /* using id_table spidev match */

  img_class->img_open  = fte7001_img_open;
  img_class->img_close = fte7001_img_close;
  img_class->activate  = fte7001_activate;
  img_class->deactivate = fte7001_deactivate;
  img_class->bz3_threshold = 25;
  img_class->img_width  = FT9338_IMAGE_WIDTH;
  img_class->img_height = FT9338_IMAGE_HEIGHT;

  G_OBJECT_CLASS (klass)->finalize = fpi_device_fte7001_finalize;
  fpi_device_class_auto_initialize_features (dev_class);
}