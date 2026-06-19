#include "diag_monitor.h"
#include <stdlib.h>

int g_shutdown = 0;
int g_recover_en = 0;
int clamp_volt(int v);
void clear_latch(Sensor* s);

FaultCode check_fault(Sensor* s, Mode mode, int retries) {
    if (s == NULL || s->id < 0) {
        return FAULT_NULL;
    }
    if (mode == ACTIVE) {
        if (s->volt < V_MIN || s->volt > V_MAX) {
            if (s->temp > T_CRIT && retries <= 0) {
                if (s->fan_ok && !s->derate) {
                    g_shutdown = 1; // modified
                    return FAULT_OVERHEAT;
                }
                return FAULT_THERMAL;
            } else if (retries > 0 && retries < MAX_RETRY) {
                s->volt = clamp_volt(s->volt);
                return FAULT_RETRY;
            } else {
                return FAULT_VOLT;
            }
        }
    } else if (mode == STANDBY || mode == SLEEP) {
        if (s->errflags & ERR_LATCH) {
            if (g_recover_en && s->retry_ok) {
                clear_latch(s);
                return FAULT_RECOVERED;
            }
            return FAULT_LATCHED;
        }
    }
    return FAULT_NONE;
}

void log_event(int code) {
    if (code != 0) {
        check_fault(NULL, ACTIVE, 0);
    }
}
