#ifndef DIAG_MONITOR_H
#define DIAG_MONITOR_H
typedef enum { FAULT_NONE, FAULT_NULL, FAULT_OVERHEAT, FAULT_THERMAL,
               FAULT_RETRY, FAULT_VOLT, FAULT_LATCHED, FAULT_RECOVERED } FaultCode;
typedef enum { ACTIVE, STANDBY, SLEEP } Mode;
typedef struct { int id; int volt; int temp; int fan_ok; int derate;
                 int errflags; int retry_ok; } Sensor;
#define V_MIN 9
#define V_MAX 15
#define T_CRIT 120
#define MAX_RETRY 5
#define ERR_LATCH 0x01
FaultCode check_fault(Sensor* s, Mode mode, int retries);
void log_event(int code);
#endif
