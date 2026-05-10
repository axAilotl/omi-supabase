#include "settings.h"

#include <errno.h>
#include <zephyr/logging/log.h>
#include <zephyr/settings/settings.h>

#include "config.h"

LOG_MODULE_REGISTER(app_settings, CONFIG_LOG_DEFAULT_LEVEL);

#define DEFAULT_DIM_LIGHT_RATIO 50

static uint8_t dim_light_ratio = DEFAULT_DIM_LIGHT_RATIO;
static uint8_t mic_gain = MIC_DEFAULT_GAIN_LEVEL;
static uint64_t rtc_epoch = 0;

static int settings_set(const char *name, size_t len, settings_read_cb read_cb, void *cb_arg)
{
    const char *next;
    int rc;

    if (settings_name_steq(name, "dim_ratio", &next) && !next) {
        if (len != sizeof(dim_light_ratio)) {
            return -EINVAL;
        }
        rc = read_cb(cb_arg, &dim_light_ratio, sizeof(dim_light_ratio));
        return (rc >= 0) ? 0 : rc;
    }

    if (settings_name_steq(name, "mic_gain", &next) && !next) {
        if (len != sizeof(mic_gain)) {
            return -EINVAL;
        }
        rc = read_cb(cb_arg, &mic_gain, sizeof(mic_gain));
        return (rc >= 0) ? 0 : rc;
    }

    if (settings_name_steq(name, "rtc_epoch", &next) && !next) {
        if (len == sizeof(rtc_epoch)) {
            rc = read_cb(cb_arg, &rtc_epoch, sizeof(rtc_epoch));
            return (rc >= 0) ? 0 : rc;
        }

        if (len == sizeof(uint32_t)) {
            uint32_t rtc_epoch_legacy = 0;

            rc = read_cb(cb_arg, &rtc_epoch_legacy, sizeof(rtc_epoch_legacy));
            if (rc >= 0) {
                rtc_epoch = rtc_epoch_legacy;
                return 0;
            }
            return rc;
        }

        return -EINVAL;
    }

    return -ENOENT;
}

SETTINGS_STATIC_HANDLER_DEFINE(app_settings, "omi", NULL, settings_set, NULL, NULL);

int app_settings_init(void)
{
    int err = settings_subsys_init();
    if (err) {
        LOG_ERR("Failed to initialize settings subsystem (err %d)", err);
        return err;
    }

    err = settings_load_subtree("omi");
    if (err && err != -ENOENT) {
        LOG_ERR("Failed to load app settings (err %d)", err);
        return err;
    }

    LOG_INF("Settings initialized. dim_ratio=%u mic_gain=%u rtc_epoch=%llu",
            dim_light_ratio,
            mic_gain,
            (unsigned long long) rtc_epoch);

    return 0;
}

int app_settings_save_dim_ratio(uint8_t new_ratio)
{
    dim_light_ratio = new_ratio;
    return settings_save_one("omi/dim_ratio", &dim_light_ratio, sizeof(dim_light_ratio));
}

uint8_t app_settings_get_dim_ratio(void)
{
    return dim_light_ratio;
}

int app_settings_save_mic_gain(uint8_t new_gain)
{
    mic_gain = new_gain;
    return settings_save_one("omi/mic_gain", &mic_gain, sizeof(mic_gain));
}

uint8_t app_settings_get_mic_gain(void)
{
    return mic_gain;
}

int app_settings_save_rtc_epoch(uint64_t epoch_s)
{
    rtc_epoch = epoch_s;
    return settings_save_one("omi/rtc_epoch", &rtc_epoch, sizeof(rtc_epoch));
}

uint64_t app_settings_get_rtc_epoch(void)
{
    return rtc_epoch;
}
