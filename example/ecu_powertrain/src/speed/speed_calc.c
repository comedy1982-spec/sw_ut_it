#include <stddef.h>
#define MAX_SPEED 200
int read_sensor(int id);
int g_last = 0;

int calculate_speed(int sensor_id, int* out_buf) {
    int raw = read_sensor(sensor_id);
    if (raw < 0) {
        return -1;
    }
    int kmh = raw * 36 / 10;
    if (kmh > MAX_SPEED) {
        kmh = MAX_SPEED;
    }
    out_buf[0] = kmh;
    g_last = kmh;
    return kmh;
}
