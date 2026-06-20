/* hw_api.h — 하드웨어 추상화 계층 (단위테스트에서는 스텁으로 대체)
 * 센서/액추에이터 접근을 한 곳에 모아 다양한 컴포넌트가 공유한다.
 */
#ifndef HW_API_H
#define HW_API_H

#include <stdint.h>

/* ---- 센서 입력 (테스트 시 스텁 반환값을 스윕) ---- */
int      adc_read(int channel);        /* 전류/전압 ADC raw 카운트 */
int      hall_read(void);              /* 홀센서 3비트 상태 (U,V,W) */
int      read_temp_raw(int sensor_id); /* 온도 센서 raw 값 */
int      encoder_read_ab(void);        /* 쿼드러처 엔코더 A/B 2비트 */
uint32_t millis(void);                 /* 시스템 틱 (ms) */

/* ---- 액추에이터 출력 ---- */
void     pwm_set_duty(int channel, int duty_q10);
void     gpio_write(int pin, int level);
void     fault_latch(uint32_t code);

#endif /* HW_API_H */
