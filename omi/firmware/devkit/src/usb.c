#include "usb.h"

#include <stdio.h>
#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/logging/log.h>
#include <zephyr/pm/pm.h>
#include <zephyr/sys/util.h>
#include <zephyr/usb/usb_device.h>

#include "sdcard.h"
#include "speaker.h"
#include "transport.h"

LOG_MODULE_REGISTER(usb, CONFIG_LOG_DEFAULT_LEVEL);

// add all device drivers here?
bool usb_charge = false;

#if DT_HAS_COMPAT_STATUS_OKAY(zephyr_cdc_acm_uart)

extern struct k_mutex write_sdcard_mutex;

#define USB_BULK_LINE_SIZE 96
#define USB_BULK_READ_CHUNK_SIZE 4096
#define USB_BULK_MAX_READ_BYTES (128 * 1024)
#define USB_BULK_STORAGE_BLOCK_SIZE 440

static const struct device *const usb_bulk_uart = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
static uint8_t usb_bulk_read_buffer[USB_BULK_READ_CHUNK_SIZE];

static void usb_bulk_write_bytes(const uint8_t *data, size_t len)
{
    if (!device_is_ready(usb_bulk_uart)) {
        return;
    }

    size_t written = 0;
    while (written < len) {
        int sent = uart_fifo_fill(usb_bulk_uart, data + written, (int) MIN(len - written, (size_t) INT_MAX));
        if (sent > 0) {
            written += (size_t) sent;
        } else {
            k_yield();
        }
    }
}

static int usb_bulk_write_sink(const uint8_t *data, int length, void *ctx)
{
    ARG_UNUSED(ctx);
    usb_bulk_write_bytes(data, (size_t) length);
    return length;
}

static void usb_bulk_write_string(const char *text)
{
    usb_bulk_write_bytes((const uint8_t *) text, strlen(text));
}

static void usb_bulk_write_error(const char *reason)
{
    char response[USB_BULK_LINE_SIZE];
    snprintf(response, sizeof(response), "ERR %s\n", reason);
    usb_bulk_write_string(response);
}

static uint32_t usb_bulk_total_size(void)
{
    return get_file_size(1);
}

static uint32_t usb_bulk_offset(void)
{
    int offset = get_offset();
    if (offset < 0) {
        return 0;
    }
    return (uint32_t) offset;
}

static void usb_bulk_handle_stat(void)
{
    uint32_t total = usb_bulk_total_size();
    uint32_t offset = usb_bulk_offset();
    if (offset > total) {
        offset = total;
    }

    char response[USB_BULK_LINE_SIZE];
    snprintf(response, sizeof(response), "STAT %u %u 1 %u\n", total, offset, USB_BULK_STORAGE_BLOCK_SIZE);
    usb_bulk_write_string(response);
}

static void usb_bulk_handle_read(uint32_t offset, uint32_t requested)
{
    uint32_t total = usb_bulk_total_size();
    if (offset > total) {
        usb_bulk_write_error("RANGE");
        return;
    }

    uint32_t length = MIN(requested, total - offset);
    length = MIN(length, (uint32_t) USB_BULK_MAX_READ_BYTES);

    if (move_read_pointer(1) != 0) {
        usb_bulk_write_error("READ_POINTER");
        return;
    }

    char header[USB_BULK_LINE_SIZE];
    snprintf(header, sizeof(header), "DATA %u %u\n", offset, length);
    usb_bulk_write_string(header);

    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    int rc = read_audio_data_bulk(
        usb_bulk_read_buffer, sizeof(usb_bulk_read_buffer), (int) length, (int) offset, usb_bulk_write_sink, NULL);
    k_mutex_unlock(&write_sdcard_mutex);

    if (rc == (int) length) {
        usb_bulk_write_string("OK\n");
    } else {
        usb_bulk_write_string("ERR READ_SHORT\n");
    }
}

static void usb_bulk_handle_mark(uint32_t new_offset)
{
    uint32_t total = usb_bulk_total_size();
    if (new_offset > total) {
        usb_bulk_write_error("RANGE");
        return;
    }

    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    int rc = save_offset(new_offset);
    k_mutex_unlock(&write_sdcard_mutex);

    if (rc == 0) {
        char response[USB_BULK_LINE_SIZE];
        snprintf(response, sizeof(response), "OK MARK %u\n", new_offset);
        usb_bulk_write_string(response);
    } else {
        usb_bulk_write_error("MARK");
    }
}

static void usb_bulk_handle_clear(void)
{
    k_mutex_lock(&write_sdcard_mutex, K_FOREVER);
    int rc = clear_audio_file(1);
    if (rc == 0) {
        rc = save_offset(0);
    }
    if (rc == 0) {
        move_read_pointer(1);
        move_write_pointer(1);
    }
    k_mutex_unlock(&write_sdcard_mutex);

    if (rc == 0) {
        usb_bulk_write_string("OK CLEAR\n");
    } else {
        usb_bulk_write_error("CLEAR");
    }
}

static void usb_bulk_handle_line(char *line)
{
    while (*line == ' ' || *line == '\t') {
        line++;
    }

    if (strcmp(line, "PING") == 0) {
        usb_bulk_write_string("OK OMIUSB 1\n");
        return;
    }

    if (strcmp(line, "STAT") == 0) {
        usb_bulk_handle_stat();
        return;
    }

    if (strcmp(line, "CLEAR") == 0) {
        usb_bulk_handle_clear();
        return;
    }

    uint32_t offset = 0;
    uint32_t length = 0;
    if (sscanf(line, "READ %u %u", &offset, &length) == 2) {
        usb_bulk_handle_read(offset, length);
        return;
    }

    if (sscanf(line, "MARK %u", &offset) == 1) {
        usb_bulk_handle_mark(offset);
        return;
    }

    usb_bulk_write_error("COMMAND");
}

static void usb_bulk_thread(void *p1, void *p2, void *p3)
{
    ARG_UNUSED(p1);
    ARG_UNUSED(p2);
    ARG_UNUSED(p3);

    char line[USB_BULK_LINE_SIZE];
    size_t line_len = 0;

    while (1) {
        if (!device_is_ready(usb_bulk_uart)) {
            k_msleep(500);
            continue;
        }
        if (!usb_charge) {
            k_msleep(250);
            continue;
        }

        uint8_t ch;
        int rc = uart_poll_in(usb_bulk_uart, &ch);
        if (rc != 0) {
            k_msleep(5);
            continue;
        }

        if (ch == '\r') {
            continue;
        }

        if (ch == '\n') {
            line[line_len] = '\0';
            if (line_len > 0) {
                usb_bulk_handle_line(line);
            }
            line_len = 0;
            continue;
        }

        if (line_len < sizeof(line) - 1) {
            line[line_len++] = (char) ch;
        } else {
            line_len = 0;
            usb_bulk_write_error("LINE_TOO_LONG");
        }
    }
}

K_THREAD_DEFINE(usb_bulk_thread_id, 2048, usb_bulk_thread, NULL, NULL, NULL, 7, 0, 0);

#endif

static void udc_status_cb(enum usb_dc_status_code status, const uint8_t *param)
{
    ARG_UNUSED(param);

    switch (status) {
    case USB_DC_CONNECTED:
        usb_charge = true;
        break;
    case USB_DC_DISCONNECTED:
        usb_charge = false;
        break;
    default:
        usb_charge = true;
    }
}

int init_usb()
{
#if DT_HAS_COMPAT_STATUS_OKAY(zephyr_cdc_acm_uart)
    if (!device_is_ready(usb_bulk_uart)) {
        LOG_ERR("USB CDC ACM device is not ready");
    }
#endif

#ifndef CONFIG_UART_CONSOLE
    usb_disable();
    int ret = usb_enable(udc_status_cb);
    LOG_INF("USB ret: %d\n", ret);
#else
    // Use this instead of the disable/enable lines above
    // as USB disabling messes up the UART logging
    usb_dc_set_status_callback(udc_status_cb);
#endif
    return 0;
}
