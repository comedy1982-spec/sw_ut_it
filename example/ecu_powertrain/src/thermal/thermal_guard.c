/* thermal_guard.c — 열 보호 상태머신
 * 스타일: enum 상태머신, 구간별 if-else 체인, 상태의존 히스테리시스,
 *         스텁 온도센서 읽기, for 루프 + 비트플래그, goto cleanup
 */
#include <stddef.h>
#include "thermal_guard.h"
#include "hw_api.h"

/* 온도 -> 단순 분류 (구간별 if-else-if 체인) */
therm_state_t thermal_classify(int temp_c)
{
    if (temp_c >= T_SHUTDOWN_C) {
        return TH_SHUTDOWN;
    } else if (temp_c >= T_DERATE_C) {
        return TH_DERATE;
    } else if (temp_c >= T_WARN_C) {
        return TH_WARN;
    }
    return TH_NORMAL;
}

/* 여러 센서를 읽어 최댓값으로 상태 전이 (히스테리시스 포함).
 * 반환: 디레이팅 퍼센트 (오류 시 음수) */
int thermal_step(ThermalState* t)
{
    int rc = 0;
    if (t == NULL) {
        return -1;
    }

    /* 센서 스캔: 최고 온도 + 폴트 비트 (for + 비트연산) */
    int hottest = -40;
    for (int i = 0; i < NUM_TEMP_SENS; i++) {
        int raw = read_temp_raw(i);
        if (raw < -40 || raw > 200) {
            t->sensor_fault |= (uint8_t)(1u << i);
            continue;                       /* 무효 센서 건너뜀 */
        }
        if (raw > hottest) {
            hottest = raw;
        }
    }

    /* 모든 센서가 폴트면 안전측(셧다운)으로 */
    if (t->sensor_fault == ((1u << NUM_TEMP_SENS) - 1u)) {
        t->state = TH_SHUTDOWN;
        rc = -2;
        goto done;
    }

    if (hottest > t->peak_c) {
        t->peak_c = hottest;
    }

    /* 상태의존 히스테리시스: 올라갈 때와 내려올 때 임계가 다름 */
    switch (t->state) {
    case TH_NORMAL:
        if (hottest >= T_WARN_C) {
            t->state = (hottest >= T_DERATE_C) ? TH_DERATE : TH_WARN;
        }
        break;
    case TH_WARN:
        if (hottest >= T_DERATE_C) {
            t->state = TH_DERATE;
        } else if (hottest < T_WARN_C - T_HYST_C) {
            t->state = TH_NORMAL;
        }
        break;
    case TH_DERATE:
        if (hottest >= T_SHUTDOWN_C) {
            t->state = TH_SHUTDOWN;
        } else if (hottest < T_DERATE_C - T_HYST_C) {
            t->state = TH_WARN;
        }
        break;
    case TH_SHUTDOWN:
        if (hottest < T_DERATE_C - T_HYST_C && t->sensor_fault == 0u) {
            t->state = TH_WARN;             /* 충분히 식고 센서 정상일 때만 복귀 */
        }
        break;
    }

    /* 상태 -> 디레이팅 퍼센트 */
    if (t->state == TH_SHUTDOWN) {
        t->derate_pct = 100;
        gpio_write(2, 0);                   /* 게이트 차단 */
    } else if (t->state == TH_DERATE) {
        /* 선형 디레이팅: DERATE~SHUTDOWN 구간을 0~100% 로 */
        int span = T_SHUTDOWN_C - T_DERATE_C;
        int over = hottest - T_DERATE_C;
        t->derate_pct = over <= 0 ? 0 : (over >= span ? 100 : (over * 100) / span);
    } else {
        t->derate_pct = 0;
    }
    rc = t->derate_pct;

done:
    return rc;
}
