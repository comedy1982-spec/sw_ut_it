/* current_sense.h — 상전류 센싱 + 과전류 보호 */
#ifndef CURRENT_SENSE_H
#define CURRENT_SENSE_H

#define PHASE_U   0
#define PHASE_V   1
#define PHASE_W   2
#define ADC_MAX   4095
#define I_TRIP_MA 8000     /* 과전류 트립 임계 */
#define I_WIN_N   4        /* 이동평균 윈도우 */

typedef struct {
    int  window[I_WIN_N];  /* 최근 샘플 링버퍼 */
    int  head;
    int  sum;
    int  trip_count;       /* 연속 초과 횟수 */
    int  tripped;          /* 트립 래치 */
} CurrentMon;

int current_sample_phase(int phase);          /* ADC -> mA, 오류 시 음수 */
int current_protect(CurrentMon* c, int amps_ma);

#endif /* CURRENT_SENSE_H */
