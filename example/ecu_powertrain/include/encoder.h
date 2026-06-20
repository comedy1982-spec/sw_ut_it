/* encoder.h — 쿼드러처 엔코더 위치/속도 디코딩 */
#ifndef ENCODER_H
#define ENCODER_H

#include <stdint.h>

#define ENC_CPR     2048        /* 회전당 카운트 */
#define ENC_HALF    (ENC_CPR / 2)

typedef struct {
    uint8_t  prev_ab;           /* 직전 A/B 2비트 */
    int32_t  position;          /* 누적 카운트 (-CPR..CPR 로 래핑) */
    int32_t  last_position;
    int32_t  velocity;          /* 카운트/초 */
    int      error_count;
} EncoderState;

int32_t encoder_decode(EncoderState* e);
int32_t encoder_velocity(EncoderState* e, int dt_ms);

#endif /* ENCODER_H */
