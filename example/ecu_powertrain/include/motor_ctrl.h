/* motor_ctrl.h — BLDC 모터 제어 (상태머신 + PWM 듀티) */
#ifndef MOTOR_CTRL_H
#define MOTOR_CTRL_H

#include <stdint.h>

enum motor_mode { MOTOR_IDLE = 0, MOTOR_START, MOTOR_RUN, MOTOR_FAULT };

/* 상태 플래그 (비트마스크) */
#define MF_OVERCURRENT  (1u << 0)
#define MF_OVERTEMP     (1u << 1)
#define MF_STALL        (1u << 2)
#define MF_HALL_INVALID (1u << 3)
#define MF_UNDERVOLT    (1u << 4)

#define DUTY_MAX   1024     /* Q10 풀스케일 */
#define DUTY_MIN   0
#define SLEW_MAX   64       /* 1스텝 최대 변화 */
#define RPM_STALL  30

typedef struct {
    enum motor_mode mode;
    int   duty;         /* 현재 듀티 (Q10) */
    int   target_rpm;
    int   actual_rpm;
    uint32_t flags;     /* MF_* 비트 */
    int   bus_mv;       /* 버스 전압 mV */
    int   enabled;
} MotorState;

int  motor_set_duty(MotorState* m, int target_duty);
int  motor_state_step(MotorState* m, uint8_t cmd);
uint32_t motor_fault_mask(const MotorState* m);

#endif /* MOTOR_CTRL_H */
