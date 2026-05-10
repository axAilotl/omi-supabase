#ifndef SETTINGS_H
#define SETTINGS_H

#include <stdint.h>

int app_settings_init(void);

int app_settings_save_dim_ratio(uint8_t new_ratio);
uint8_t app_settings_get_dim_ratio(void);

int app_settings_save_mic_gain(uint8_t new_gain);
uint8_t app_settings_get_mic_gain(void);

int app_settings_save_rtc_epoch(uint64_t epoch_s);
uint64_t app_settings_get_rtc_epoch(void);

#endif
