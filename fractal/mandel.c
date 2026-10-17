// Mandelbrot escape times for mandel.py (loaded with ctypes; built automatically on first use).
// out[y*w + x] = smooth iteration count, or -1 inside the set. Rows are shared across cores
// with OpenMP when it's available.

#include <math.h>

void mandel(double cx, double cy, double width, int w, int h, int maxit, float *out) {
    double step = width / (w - 1);
    #pragma omp parallel for schedule(dynamic, 4)
    for (int y = 0; y < h; y++) {
        double ci = cy + (h / 2.0 - y) * step;
        for (int x = 0; x < w; x++) {
            double cr = cx + (x - w / 2.0) * step;
            // the main cardioid and the period-2 bulb are inside: skip the slowest pixels
            double q = (cr - 0.25) * (cr - 0.25) + ci * ci;
            if (q * (q + (cr - 0.25)) <= 0.25 * ci * ci || (cr + 1) * (cr + 1) + ci * ci <= 0.0625) {
                out[y * w + x] = -1;
                continue;
            }
            double zr = 0, zi = 0, zr2 = 0, zi2 = 0;
            int n = 0;
            while (n < maxit && zr2 + zi2 <= 256.0) {
                zi = 2 * zr * zi + ci;
                zr = zr2 - zi2 + cr;
                zr2 = zr * zr;
                zi2 = zi * zi;
                n++;
            }
            out[y * w + x] = n >= maxit ? -1.0f : (float)(n - log2(log2(zr2 + zi2) / 2));   // (n already counts the escaping step)
        }
    }
}
