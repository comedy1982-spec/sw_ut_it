/* encoder.c — 쿼드러처 엔코더 디코딩
 * 스타일: 비트연산, static const 룩업테이블, do-while, 부호있는 래핑, 0나눗셈 가드
 */
#include <stddef.h>
#include "encoder.h"
#include "hw_api.h"

/* 그레이코드 전이 룩업: index = (prev<<2)|cur, 값 = +1/-1/0(불변)/2(무효) */
static const int s_quad_lut[16] = {
     0, -1, +1,  2,
    +1,  0,  2, -1,
    -1,  2,  0, +1,
     2, +1, -1,  0,
};

/* 엔코더 A/B 를 읽어 위치 갱신. 반환: 현재 위치(래핑됨) */
int32_t encoder_decode(EncoderState* e)
{
    if (e == NULL) {
        return 0;
    }
    uint8_t cur = (uint8_t)(encoder_read_ab() & 0x3);
    uint8_t idx = (uint8_t)((e->prev_ab << 2) | cur);
    int step = s_quad_lut[idx & 0xF];

    if (step == 2) {
        e->error_count++;               /* 무효 전이 (양 비트 동시변화) */
    } else {
        e->position += step;
        /* 1회전 경계 래핑 (do-while 로 여러 회전 보정) */
        do {
            if (e->position >= ENC_CPR) {
                e->position -= ENC_CPR;
            } else if (e->position < 0) {
                e->position += ENC_CPR;
            }
        } while (e->position >= ENC_CPR || e->position < 0);
    }

    e->prev_ab = cur;
    return e->position;
}

/* 위치 변화량 / 시간 -> 속도. dt<=0 가드, 최단경로(부호) 계산 */
int32_t encoder_velocity(EncoderState* e, int dt_ms)
{
    if (e == NULL || dt_ms <= 0) {
        return 0;
    }
    int32_t d = e->position - e->last_position;

    /* 래핑 경계를 가로지른 경우 최단 방향으로 보정 */
    if (d > ENC_HALF) {
        d -= ENC_CPR;
    } else if (d < -ENC_HALF) {
        d += ENC_CPR;
    }

    e->last_position = e->position;
    e->velocity = (d * 1000) / dt_ms;   /* 카운트/초 */
    return e->velocity;
}
