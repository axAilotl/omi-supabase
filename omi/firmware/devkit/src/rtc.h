#ifndef OMI_RTC_H_
#define OMI_RTC_H_

#include <stdbool.h>
#include <stdint.h>

void init_rtc(void);
uint32_t get_utc_time(void);
bool rtc_is_valid(void);
int rtc_set_utc_time(uint64_t utc_epoch_s);
int rtc_set_utc_time_ms(uint64_t utc_epoch_ms);
uint64_t rtc_get_utc_time_ms(void);

#endif
