"""The sound for `kindle synth`: the Kindle is the instrument (synth.py on the Pi), this plays it
on the Mac. Runs with the Kindle Sim's Python (numpy + sounddevice).

Live: keys (N/O) and drum pads (H) sound at once. Pattern: the drum grid (G) and the notes (P)
loop while playing (T play 1), timed on the audio clock here, so the Kindle's e-ink and the
network never make the beat wobble.

  synth.py                 play from the Kindle (Ctrl+C or Exit on the Kindle to end)
  synth.py test            a short sound check on the Mac, no Kindle
"""

import json
import os
import subprocess
import sys
import threading
import time

import numpy as np

RATE = 48000
BLOCK = 256                           # samples per audio callback (~5 ms)
VOICES = 24
ATTACK = 0.006
RELEASE = {"short": 0.12, "long": 1.4}
DECAY_TO = 0.75
ECHO_S, ECHO_FEEDBACK, ECHO_MIX = 0.32, 0.38, 0.35
DRUM_GAIN = 0.9
PI = os.environ.get("KINDLE_PI", "arham@arham-pi")
DASH = os.environ.get("KINDLE_DASH", "kindle-dash")


# ------------------------------------------------------------ drum sounds (made, not samples)

def _drums():
    rng = np.random.default_rng(7)
    t = lambda s: np.arange(int(RATE * s)) / RATE
    noise = lambda s: rng.uniform(-1, 1, int(RATE * s))

    def hp(x, a=0.9):                                     # a crude high-pass: keep the hiss
        y = np.empty_like(x)
        prev_x = prev_y = 0.0
        for i, v in enumerate(x):
            prev_y = a * (prev_y + v - prev_x)
            prev_x = v
            y[i] = prev_y
        return y

    def tone(f0, f1, dur, k):
        tt = t(dur)
        freq = f1 + (f0 - f1) * np.exp(-tt * k)
        return np.sin(2 * np.pi * np.cumsum(freq) / RATE)

    tt = t(0.45)
    kick = tone(150, 45, 0.45, 28) * np.exp(-tt * 7)
    kick[:120] += np.linspace(0.6, 0, 120)                 # the click of the beater
    ts = t(0.22)
    snare = 0.55 * hp(noise(0.22), 0.8) * np.exp(-ts * 18) + 0.5 * tone(220, 180, 0.22, 20) * np.exp(-ts * 25)
    clap = np.zeros(int(RATE * 0.25))
    for k, at in enumerate((0, 0.011, 0.022)):
        i = int(at * RATE)
        n = hp(noise(0.25 - at), 0.85) * np.exp(-t(0.25 - at) * (90 if k < 2 else 16))
        clap[i:i + len(n)] += n * 0.6
    hat = hp(noise(0.06), 0.95) * np.exp(-t(0.06) * 70) * 0.6
    ohat = hp(noise(0.4), 0.95) * np.exp(-t(0.4) * 8) * 0.5
    ltom = tone(130, 85, 0.35, 12) * np.exp(-t(0.35) * 9)
    htom = tone(200, 150, 0.3, 12) * np.exp(-t(0.3) * 10)
    tc = t(1.3)
    metal = sum(np.sign(np.sin(2 * np.pi * f * tc)) for f in (540, 800, 1150, 1370, 1680)) / 5
    crash = (0.5 * hp(noise(1.3), 0.96) + 0.25 * metal) * np.exp(-tc * 2.6)
    return [x.astype(np.float32) for x in (kick, snare, clap, hat, ohat, ltom, htom, crash)]


class Synth:
    def __init__(self):
        self.lock = threading.RLock()
        self.voices = {}                  # id -> dict(freq, phase, level, gate, age)
        self.hits = []                    # [samples, position, gain] playing drum sounds
        self.wave, self.release, self.echo = "saw", "short", False
        self.lp = 0.0
        self.delay = np.zeros(int(RATE * ECHO_S), np.float32)
        self.dpos = 0
        self.drums = _drums()
        self.t = 0                        # samples played so far (the clock)
        self.playing, self.bpm, self.length = False, 110, 16
        self.grid = [[0] * 32 for _ in range(8)]
        self.notes = []
        self.next_step, self.step_no = None, 0
        self.offs = []                    # (sample time, voice id) for pattern notes

    # ------------------------------------------------------------ playing things
    def note_on(self, vid, midi):
        freq = 440.0 * 2 ** ((midi - 69) / 12)
        with self.lock:
            if len(self.voices) >= VOICES:
                del self.voices[min(self.voices, key=lambda k: self.voices[k]["level"])]
            old = self.voices.pop(vid, None)
            if old:
                self.voices[("fade", self.t, vid)] = dict(old, gate=False)
            self.voices[vid] = {"freq": freq, "phase": 0.0, "level": 0.0, "gate": True, "age": 0.0}

    def note_off(self, vid):
        with self.lock:
            v = self.voices.get(vid)
            if v:
                v["gate"] = False

    def hit(self, pad, gain=1.0):
        with self.lock:
            if 0 <= pad < len(self.drums):
                self.hits.append([self.drums[pad], 0, gain * DRUM_GAIN])

    def transport(self, what, value):
        with self.lock:
            if what == "play":
                self.playing = bool(int(value))
                self.next_step, self.step_no = (self.t, 0) if self.playing else (None, 0)
                if not self.playing:
                    for _, vid in self.offs:
                        self.note_off(vid)
                    self.offs = []
            elif what == "bpm":
                self.bpm = max(40, min(240, int(value)))
                if self.playing:
                    self.next_step, self.step_no = self.t, 0           # restart in step with the Kindle
            elif what == "len":
                self.length = int(value)

    # ------------------------------------------------------------ the clock
    def _sequence(self, n):
        """Trigger the pattern's steps that fall in this block (at its start: <=5 ms off)."""
        end = self.t + n
        for at, vid in [o for o in self.offs if o[0] < end]:
            self.note_off(vid)
        self.offs = [o for o in self.offs if o[0] >= end]
        if not self.playing:
            return
        step_len = RATE * 60.0 / self.bpm / 4
        while self.next_step is not None and self.next_step < end:
            step = self.step_no % self.length
            for row in range(8):
                if self.grid[row][step]:
                    self.hit(row, 0.85)
            for k, nt in enumerate(self.notes):
                if nt["step"] == step:
                    vid = ("seq", k, self.step_no)
                    self.note_on(vid, nt["midi"])
                    self.offs.append((self.next_step + nt["len"] * step_len * 0.92, vid))
            self.step_no += 1
            self.next_step += step_len

    def _osc(self, phase):
        if self.wave == "sine":
            return np.sin(2 * np.pi * phase)
        if self.wave == "square":
            return np.where(phase % 1 < 0.5, 0.6, -0.6)
        if self.wave == "tri":
            return 4 * np.abs(phase % 1 - 0.5) - 1
        return 2 * (phase % 1) - 1

    def render(self, n):
        out = np.zeros(n, np.float32)
        tt = np.arange(1, n + 1, dtype=np.float64)
        rel = RELEASE[self.release]
        with self.lock:
            self._sequence(n)
            for vid in list(self.voices):
                v = self.voices[vid]
                phase = v["phase"] + v["freq"] * tt / RATE
                if v["gate"]:
                    age = v["age"] + tt / RATE
                    target = np.where(age < ATTACK, age / ATTACK, DECAY_TO + (1 - DECAY_TO) * np.exp(-(age - ATTACK) * 6))
                    env = np.maximum(target, v["level"] * (age < ATTACK))
                else:
                    env = v["level"] * np.exp(-tt / RATE * 5 / rel)
                out += (self._osc(phase) * env).astype(np.float32)
                v["phase"] = phase[-1] % 1.0
                v["age"] += n / RATE
                v["level"] = float(env[-1])
                if not v["gate"] and v["level"] < 1e-3:
                    del self.voices[vid]
            out *= 0.22
            a = 0.35 if self.wave in ("saw", "square") else 0.8
            y = np.empty_like(out)
            lp = self.lp
            for i in range(n):
                lp += a * (out[i] - lp)
                y[i] = lp
            self.lp = lp
            drums = np.zeros(n, np.float32)
            for hit in self.hits:
                sound, pos, gain = hit
                part = sound[pos:pos + n]
                drums[:len(part)] += part * gain
                hit[1] = pos + n
            self.hits = [hh for hh in self.hits if hh[1] < len(hh[0])]
            y += drums * 0.5
            if self.echo:
                for i in range(n):
                    d = self.delay[self.dpos]
                    self.delay[self.dpos] = y[i] + d * ECHO_FEEDBACK
                    y[i] += d * ECHO_MIX
                    self.dpos = (self.dpos + 1) % len(self.delay)
            self.t += n
        return np.tanh(y * 1.5) / 1.5

    def stream(self):
        import sounddevice as sd

        def callback(outdata, frames, time_info, status):
            outdata[:, 0] = self.render(frames)
        return sd.OutputStream(samplerate=RATE, channels=1, blocksize=BLOCK, latency="low", callback=callback)

    # ------------------------------------------------------------ messages from the Kindle
    def handle(self, line):
        p = line.split(None, 1)
        if not p:
            return None
        cmd, arg = p[0], (p[1].strip() if len(p) > 1 else "")
        a = arg.split()
        if cmd == "N" and len(a) == 2:
            self.note_on(int(a[0]), int(a[1]))
        elif cmd == "O" and len(a) == 1:
            self.note_off(int(a[0]))
        elif cmd == "H" and len(a) == 1:
            self.hit(int(a[0]))
        elif cmd == "C" and len(a) == 2:
            if a[0] == "wave":
                self.wave = a[1]
            elif a[0] == "release":
                self.release = a[1]
            elif a[0] == "echo":
                self.echo = a[1] == "on"
        elif cmd == "T" and len(a) == 2:
            self.transport(a[0], a[1])
        elif cmd == "G":
            with self.lock:
                self.grid = json.loads(arg)
        elif cmd == "P":
            with self.lock:
                self.notes = json.loads(arg)
        return cmd


def test():
    s = Synth()
    with s.stream():
        for k, midi in enumerate((60, 64, 67, 72)):
            s.note_on(k, midi)
            time.sleep(0.15)
        time.sleep(0.3)
        for k in range(4):
            s.note_off(k)
        for pad in (0, 3, 1, 3, 0, 0, 1, 7):
            s.hit(pad)
            time.sleep(0.2)
        time.sleep(1)


def play():
    s = Synth()
    game = subprocess.Popen(["ssh", "-tt", PI, "cd ~/%s && exec venv/bin/python synth.py" % DASH],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        with s.stream():
            for line in game.stdout:
                cmd = s.handle(line)
                if cmd == "READY":
                    print("the Kindle is your instrument now: Keys, Drums, Beat and Notes along the top. "
                          "Exit on the Kindle or Ctrl+C ends it.", flush=True)
                elif cmd == "QUIT":
                    break
                elif cmd == "ERROR":
                    print(line.strip())
            time.sleep(RELEASE["long"])
    except KeyboardInterrupt:
        try:
            game.stdin.write("QUIT\n")
            game.stdin.flush()
        except OSError:
            pass
    finally:
        try:
            game.wait(15)
        except subprocess.TimeoutExpired:
            game.kill()
        print("synth stopped")


if __name__ == "__main__":
    if sys.argv[1:2] == ["test"]:
        test()
    else:
        play()
