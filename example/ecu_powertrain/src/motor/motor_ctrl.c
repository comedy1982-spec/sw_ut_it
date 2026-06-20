/* motor_ctrl.c — BLDC 모터 제어 로직
 * 스타일: switch/case 상태머신, 삼항연산자, 복합조건(&&/||), 비트마스크, 슬루레이트 클램핑
 */
#include <stddef.h>
#include "motor_ctrl.h"
#include "hw_api.h"

static int g_estop = 0;        /* 비상정지 (외부에서 셋) */

/* 슬루레이트 제한 + 범위 클램핑 후 듀티 적용.
 * 반환: 실제 적용된 듀티 (오류 시 음수 코드) */
int motor_set_duty(MotorState* m, int target_duty)
{
    if (m == NULL || !m->enabled) {
        return -1;
    }
    if (g_estop || (m->flags & (MF_OVERCURRENT | MF_OVERTEMP))) {
        m->duty = 0;
        pwm_set_duty(0, 0);
        return -2;
    }

    /* 범위 클램핑 (삼항연산자) */
    int t = target_duty < DUTY_MIN ? DUTY_MIN
          : (target_duty > DUTY_MAX ? DUTY_MAX : target_duty);

    /* 슬루레이트 제한 */
    int delta = t - m->duty;
    if (delta > SLEW_MAX) {
        t = m->duty + SLEW_MAX;
    } else if (delta < -SLEW_MAX) {
        t = m->duty - SLEW_MAX;
    }

    /* 저전압이면서 듀티를 올리려 하면 거부 */
    if (m->bus_mv < 9000 && t > m->duty) {
        m->flags |= MF_UNDERVOLT;
        return -3;
    }

    m->duty = t;
    pwm_set_duty(0, t);
    return t;
}

/* 명령(cmd)에 따른 상태 전이 — switch/case 상태머신 */
int motor_state_step(MotorState* m, uint8_t cmd)
{
    if (m == NULL) {
        return MOTOR_FAULT;
    }

    switch (m->mode) {
    case MOTOR_IDLE:
        if (cmd == 1 && m->enabled) {
            m->mode = MOTOR_START;
        }
        break;

    case MOTOR_START:
        /* 시동 중 홀센서 무효 또는 과전류면 폴트 */
        if ((m->flags & MF_HALL_INVALID) || (m->flags & MF_OVERCURRENT)) {
            m->mode = MOTOR_FAULT;
        } else if (m->actual_rpm > RPM_STALL) {
            m->mode = MOTOR_RUN;
        }
        break;

    case MOTOR_RUN:
        if (cmd == 0) {
            m->mode = MOTOR_IDLE;
        } else if (m->actual_rpm < RPM_STALL && m->duty > SLEW_MAX) {
            m->flags |= MF_STALL;
            m->mode = MOTOR_FAULT;
        }
        break;

    case MOTOR_FAULT:
        /* cmd==9: 폴트 클리어 (플래그 없을 때만) */
        if (cmd == 9 && m->flags == 0u) {
            m->mode = MOTOR_IDLE;
        }
        break;

    default:
        m->mode = MOTOR_FAULT;
        break;
    }
    return (int)m->mode;
}

/* 센서값을 읽어 폴트 비트마스크 산출 — 비트연산 + 복합조건 */
uint32_t motor_fault_mask(const MotorState* m)
{
    uint32_t mask = 0u;
    if (m == NULL) {
        return MF_HALL_INVALID;
    }

    int hall = hall_read() & 0x7;           /* 3비트 */
    /* 유효 홀 상태는 1..6 (0,7 은 무효) */
    if (hall == 0 || hall == 7) {
        mask |= MF_HALL_INVALID;
    }

    int amps = adc_read(0);
    if (amps > 5000 || amps < -5000) {
        mask |= MF_OVERCURRENT;
    }

    if (m->bus_mv < 9000) {
        mask |= MF_UNDERVOLT;
    }

    /* RUN 인데 회전수가 안 나오면 스톨 */
    if (m->mode == MOTOR_RUN && m->actual_rpm < RPM_STALL && !(mask & MF_OVERCURRENT)) {
        mask |= MF_STALL;
    }
    return mask;
}
