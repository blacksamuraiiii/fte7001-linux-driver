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

static void
fte7001_submit_command (FpiSsm *ssm, guint8 cmd, gboolean cancellable)
{
  FpiDeviceFte7001 *self = FPI_DEVICE_FTE7001 (fpi_ssm_get_device (ssm));
  FpiSpiTransfer *transfer;
  const gsize plen = 4096;

  self->small_rx_valid = FALSE;
  transfer = fpi_spi_transfer_new (FP_DEVICE (self), self->spi_fd);
  fpi_spi_transfer_write (transfer, plen);
  transfer->buffer_wr[0] = cmd;
  fpi_spi_transfer_read (transfer, 1);
  fte7001_submit_transfer (ssm, transfer, cancellable);
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

static void
fte7001_determine_spidev_speed (FpiDeviceFte7001 *self)
{
  guint32 speed;

  if (ioctl (self->spi_fd, SPI_IOC_RD_MAX_SPEED_HZ, &speed) != 0)
    speed = FTE7001_SPI_SPEED_HZ;
  if (speed == 0)
    speed = FTE7001_SPI_SPEED_HZ;
  /* Configure the speed for all subsequent transfers */
  ioctl (self->spi_fd, SPI_IOC_WR_MAX_SPEED_HZ, &speed);
}

static gboolean
fte7001_take_gpio (FpiDeviceFte7001 *self, GError **error)
{
  struct gpiod_line_settings *settings;
  struct gpiod_line_config *config;
  struct gpiod_request_config *req_cfg;
  const gchar *chip_path;

  settings = gpiod_line_settings_new ();
  gpiod_line_settings_set_direction (settings, GPIOD_LINE_DIRECTION_OUTPUT);
  gpiod_line_settings_set_output_value (settings, GPIOD_LINE_VALUE_ACTIVE);

  config = gpiod_line_config_new ();
  gpiod_line_config_add_line_settings (config,
      &self->gpio_profile->reset_offset, 1, settings);
  req_cfg = gpiod_request_config_new ();
  gpiod_request_config_set_consumer (req_cfg, "fte7001");

  chip_path = g_strdup_printf ("/dev/gpiochip0");
  self->gpio_request = gpiod_chip_request_lines (
      gpiod_chip_open (chip_path), req_cfg, config);
  gpiod_line_settings_free (settings);
  gpiod_line_config_free (config);
  gpiod_request_config_free (req_cfg);

  if (!self->gpio_request)
    {
      g_set_error (error, G_IO_ERROR, G_IO_ERROR_FAILED,
                   "fte7001 could not claim GPIO chip %s", chip_path);
      return FALSE;
    }
  return TRUE;
}

/* ----- State machine forward declarations and enums ----- */
static void fte7001_init_handler (FpiSsm *ssm, FpDevice *dev);
static void fte7001_init_complete (FpiSsm *ssm, FpDevice *dev, GError *error);

enum fte7001_init_state {
  FTE7001_INIT_RESET_ASSERT,
  FTE7001_INIT_RESET_HOLD,
  FTE7001_INIT_RESET_DEASSERT,
  FTE7001_INIT_RESET_SETTLE,
  FTE7001_INIT_WAKE_1,
  FTE7001_INIT_WAKE_DELAY,
  FTE7001_INIT_WAKE_2,
  FTE7001_INIT_SETTLE,
  FTE7001_INIT_READ_MCU,
  FTE7001_INIT_CHECK_MCU,
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
      fpi_image_device_open_complete (dev,
        g_error_new (G_IO_ERROR, g_io_error_from_errno (errno),
                     "FTE7001: cannot open %s: %s", spidev_path,
                     g_strerror (errno)));
      return;
    }
  fte7001_determine_spidev_speed (self);

  /* Start init SSM */
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
    case FTE7001_INIT_RESET_ASSERT:
      fte7001_set_hardware_reset (ssm, self, TRUE);
      return;
    case FTE7001_INIT_RESET_HOLD:
      fpi_ssm_next_state_delayed (ssm, FT9338_RESET_PULSE_MS);
      return;
    case FTE7001_INIT_RESET_DEASSERT:
      fte7001_set_hardware_reset (ssm, self, FALSE);
      return;
    case FTE7001_INIT_RESET_SETTLE:
      fpi_ssm_next_state_delayed (ssm, 50);
      return;
    case FTE7001_INIT_WAKE_1:
    case FTE7001_INIT_WAKE_2:
      fte7001_submit_command (ssm, 0x70, TRUE);
      return;
    case FTE7001_INIT_WAKE_DELAY:
      fpi_ssm_next_state_delayed (ssm, FT9338_RESET_DELAY_MS);
      return;
    case FTE7001_INIT_SETTLE:
      fpi_ssm_next_state_delayed (ssm, FT9338_RESET_SETTLE_MS + 2);
      return;
    case FTE7001_INIT_READ_MCU:
      fte7001_submit_reg_read (ssm, FT9338_REG_MCU_STATUS, 2, TRUE);
      return;
    case FTE7001_INIT_CHECK_MCU:
      /* Cold-booted chip may not be idle yet even with correct timing
       * (P7-H: ROM firmware is already running; a non-idle MCU here is
       * normal, not an error).  Downgrade to a warning and let config
       * init finish waking it up; only INIT_CHECK_FINAL may demand idle. */
      if (!fte7001_mcu_is_idle (self))
        {
          g_warning ("FT9338: MCU not idle right after wake (%02x %02x) — "
                     "continuing with config init (P7-H)",
                     self->small_rx[4], self->small_rx[5]);
        }
      fpi_ssm_next_state (ssm);
      return;
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
      self->idle_verified = FALSE;
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