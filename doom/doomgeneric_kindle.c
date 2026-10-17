// doomgeneric backend for the Kindle dashboard (doom.py runs it on the Pi).
//
// Screen: every frame goes to a shared-memory file as 8-bit grey, with a frame counter that
//   doom.py polls; it sends the newest frame to the Kindle whenever the panel is ready.
//     header: 4 x uint32 = magic "KDOM", width, height, frame counter (written last)
//     then width*height bytes of grey
// Keys: 2-byte messages {pressed (0/1), doom key code} on a named pipe, written by touch.py
//   from the Kindle's touch buttons.
// Paths come from KINDLE_DOOM_FB and KINDLE_DOOM_KEYS. No sound.

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "doomgeneric.h"

#define HEADER 16
#define QUEUE 64

static uint8_t *shared;
static volatile uint32_t *counter;
static int keys_fd = -1, keys_keepalive = -1;
static unsigned char queue[QUEUE][2];
static int q_head, q_tail;

static const char *env(const char *name, const char *fallback) {
    const char *v = getenv(name);
    return v && *v ? v : fallback;
}

void DG_Init(void) {
    const char *fb = env("KINDLE_DOOM_FB", "/dev/shm/kindle-doom.fb");
    size_t size = HEADER + DOOMGENERIC_RESX * DOOMGENERIC_RESY;
    int fd = open(fb, O_RDWR | O_CREAT, 0644);
    if (fd < 0 || ftruncate(fd, size) != 0) {
        perror(fb);
        exit(1);
    }
    shared = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (shared == MAP_FAILED) {
        perror("mmap");
        exit(1);
    }
    close(fd);
    uint32_t *h = (uint32_t *)shared;
    memcpy(h, "KDOM", 4);
    h[1] = DOOMGENERIC_RESX;
    h[2] = DOOMGENERIC_RESY;
    h[3] = 0;
    counter = &h[3];

    const char *keys = env("KINDLE_DOOM_KEYS", "/dev/shm/kindle-doom.keys");
    if (mkfifo(keys, 0600) != 0 && errno != EEXIST) {
        perror(keys);
        exit(1);
    }
    keys_fd = open(keys, O_RDONLY | O_NONBLOCK);
    keys_keepalive = open(keys, O_WRONLY | O_NONBLOCK);   // so reads never hit end-of-file
    if (keys_fd < 0) {
        perror(keys);
        exit(1);
    }
}

void DG_DrawFrame(void) {
    uint8_t *out = shared + HEADER;
    const uint32_t *in = (const uint32_t *)DG_ScreenBuffer;
    for (int i = 0; i < DOOMGENERIC_RESX * DOOMGENERIC_RESY; i++) {
        uint32_t p = in[i];
        out[i] = (uint8_t)((((p >> 16) & 0xff) * 77 + ((p >> 8) & 0xff) * 150 + (p & 0xff) * 29) >> 8);
    }
    __sync_synchronize();
    *counter = *counter + 1;
}

void DG_SleepMs(uint32_t ms) {
    usleep(ms * 1000);
}

uint32_t DG_GetTicksMs(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

int DG_GetKey(int *pressed, unsigned char *key) {
    unsigned char buf[2 * QUEUE];
    ssize_t n = read(keys_fd, buf, sizeof buf);       // whole messages: writes of 2 bytes are atomic
    for (ssize_t i = 0; i + 1 < n; i += 2) {
        int next = (q_tail + 1) % QUEUE;
        if (next == q_head)
            break;                                     // full: drop (never happens in practice)
        queue[q_tail][0] = buf[i];
        queue[q_tail][1] = buf[i + 1];
        q_tail = next;
    }
    if (q_head == q_tail)
        return 0;
    *pressed = queue[q_head][0];
    *key = queue[q_head][1];
    q_head = (q_head + 1) % QUEUE;
    return 1;
}

void DG_SetWindowTitle(const char *title) {
    (void)title;
}

int main(int argc, char **argv) {
    doomgeneric_Create(argc, argv);
    for (;;)
        doomgeneric_Tick();
    return 0;
}
