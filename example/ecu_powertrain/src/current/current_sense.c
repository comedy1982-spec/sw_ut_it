/* current_sense.c — 상전류 센싱 및 과전류 보호
 * 스타일: 스텁 ADC 읽기, 배열 링버퍼, for 루프, 이동평균, 히스테리시스 래치, 조기반환
 */
#include <stddef.h>
#include "current_sense.h"
#include "hw_api.h"

/* ADC 채널 매핑 (파일 로컬, static const 배열) */
static const int s_phase_ch[3] = { 5, 6, 7 };

/* 한 상의 전류를 읽어 mA 로 변환. 잘못된 phase/ADC 는 음수 반환 */
int current_sample_phase(int phase)
{
    if (phase < PHASE_U || phase > PHASE_W) {
        return -1;                      /* 잘못된 상 */
    }
    int raw = adc_read(s_phase_ch[phase]);
    if (raw < 0 || raw > ADC_MAX) {
        return -2;                      /* ADC 범위 이탈 */
    }
    /* 중점(2048) 기준 양/음, 1카운트 ≈ 4mA (삼항으로 부호 처리) */
    int centered = raw - 2048;
    int ma = centered * 4;
    return ma >= 0 ? ma : -ma;          /* 절대값 */
}

/* 이동평균 갱신 후 과전류 판정 (연속 3회 초과 시 트립) */
int current_protect(CurrentMon* c, int amps_ma)
{
    if (c == NULL) {
        return -1;
    }
    if (c->tripped) {
        return 1;                       /* 이미 트립 (래치) */
    }

    /* 링버퍼 갱신 + 합계 재계산 (for 루프) */
    c->sum -= c->window[c->head];
    c->window[c->head] = amps_ma;
    c->sum += amps_ma;
    c->head = (c->head + 1) % I_WIN_N;

    int avg = 0;
    for (int i = 0; i < I_WIN_N; i++) {
        avg += c->window[i];
    }
    avg /= I_WIN_N;

    /* 히스테리시스: 평균 또는 순간값이 임계 초과면 카운트 증가 */
    if (avg > I_TRIP_MA || amps_ma > (I_TRIP_MA * 3) / 2) {
        c->trip_count++;
        if (c->trip_count >= 3) {
            c->tripped = 1;
            fault_latch(0xC0u);
            return 1;
        }
    } else if (c->trip_count > 0) {
        c->trip_count--;                /* 정상이면 서서히 감소 */
    }
    return 0;
}
