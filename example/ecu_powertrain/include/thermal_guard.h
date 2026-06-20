/* thermal_guard.h — 인버터/모터 열 보호 (히스테리시스 + 디레이팅) */
#ifndef THERMAL_GUARD_H
#define THERMAL_GUARD_H

#include <stdint.h>

typedef enum {
    TH_NORMAL = 0,
    TH_WARN,
    TH_DERATE,
    TH_SHUTDOWN
} therm_state_t;

#define T_WARN_C      85
#define T_DERATE_C    105
#define T_SHUTDOWN_C  125
#define T_HYST_C      8        /* 히스테리시스 폭 */
#define NUM_TEMP_SENS 3

typedef struct {
    therm_state_t state;
    int  peak_c;
    int  derate_pct;          /* 0..100 출력 제한 */
    uint8_t sensor_fault;     /* 비트별 센서 폴트 */
} ThermalState;

therm_state_t thermal_classify(int temp_c);
int           thermal_step(ThermalState* t);

#endif /* THERMAL_GUARD_H */
