#!/usr/bin/python3
"""The Pi's desktop as a stream of grey frames, on desktops where screenshots need permission
(KDE Plasma): asks the desktop's screen sharing (the xdg-desktop-portal ScreenCast service),
then reads the shared video with GStreamer. mirror.py runs this for `mirror pi` on Plasma.

The first time, Plasma shows "Allow screen sharing?" on the Pi's screen: tick "allow restoring"
and Allow. The answer is kept (out/screencast_token) so later starts don't ask again.

Runs with the system Python (python3-dbus, python3-gi, gir1.2-gst-1.0, gstreamer1.0-pipewire).

  pi_screencast.py WIDTH HEIGHT FPS

stdout: a line "READY WxH", then raw 8-bit grey frames of W*H bytes each, forever.
stderr: progress ("asking", "waiting for approval") and errors.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "out", "screencast_token")
uid = os.getuid()
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % uid)
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/%d/bus" % uid)

import dbus                                              # noqa: E402
import gi                                                # noqa: E402
from dbus.mainloop.glib import DBusGMainLoop             # noqa: E402
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst                     # noqa: E402

PORTAL = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
SC = "org.freedesktop.portal.ScreenCast"
MONITOR, CURSOR_EMBEDDED, PERSIST_UNTIL_REVOKED = 1, 2, 2


def say(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


class Cast:
    def __init__(self, w, h, fps):
        self.w, self.h, self.fps = w, h, fps
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()
        self.portal = self.bus.get_object(PORTAL, PATH)
        self.sc = dbus.Interface(self.portal, SC)
        self.loop = GLib.MainLoop()
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.n = 0
        self.session = None
        self.out = sys.stdout.buffer

    def _request(self, method, *args, then):
        """Call a portal method whose answer comes later as a Request.Response signal."""
        self.n += 1
        token = "kindle%d" % self.n
        path = "/org/freedesktop/portal/desktop/request/%s/%s" % (self.sender, token)

        def on_response(code, results):
            if code != 0:
                say("ERROR screen sharing wasn't allowed (or was cancelled) on the Pi's screen")
                self.loop.quit()
                return
            then(results)
        self.bus.add_signal_receiver(on_response, "Response", "org.freedesktop.portal.Request", PORTAL, path)
        opts = args[-1]
        opts["handle_token"] = token
        getattr(self.sc, method)(*args)

    def start(self):
        say("asking the desktop for screen sharing")
        self._request("CreateSession", {"session_handle_token": "kindlesession"}, then=self._created)

    def _created(self, results):
        self.session = results["session_handle"]
        opts = {"types": dbus.UInt32(MONITOR), "multiple": False, "cursor_mode": dbus.UInt32(CURSOR_EMBEDDED),
                "persist_mode": dbus.UInt32(PERSIST_UNTIL_REVOKED)}
        try:
            with open(TOKEN_FILE) as f:
                opts["restore_token"] = f.read().strip()
        except OSError:
            say("first time: approve screen sharing on the Pi's screen (tick 'allow restoring')")
        self._request("SelectSources", self.session, opts, then=self._selected)

    def _selected(self, results):
        self._request("Start", self.session, "", {}, then=self._started)

    def _started(self, results):
        if "restore_token" in results:
            os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
            with open(TOKEN_FILE, "w") as f:
                f.write(str(results["restore_token"]))
        streams = results.get("streams") or []
        if not streams:
            say("ERROR the desktop didn't share any screen")
            self.loop.quit()
            return
        node = int(streams[0][0])
        fd = self.sc.OpenPipeWireRemote(self.session, dbus.Dictionary({}, signature="sv")).take()
        Gst.init(None)
        pipe = Gst.parse_launch(
            "pipewiresrc fd=%d path=%d do-timestamp=true keepalive-time=1000 ! videoconvert ! "
            "videorate drop-only=true ! video/x-raw,framerate=%d/1 ! "
            "videoscale add-borders=true ! video/x-raw,format=GRAY8,width=%d,height=%d,pixel-aspect-ratio=1/1 ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false" % (fd, node, self.fps, self.w, self.h))
        pipe.get_by_name("sink").connect("new-sample", self._frame)
        pipe.get_bus().add_signal_watch()
        pipe.get_bus().connect("message::error", lambda bus, m: (say("ERROR %s" % m.parse_error()[0].message),
                                                                  self.loop.quit()))
        pipe.set_state(Gst.State.PLAYING)
        self.pipe = pipe
        self.out.write(b"READY %dx%d\n" % (self.w, self.h))
        self.out.flush()
        say("sharing the screen")

    def _frame(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = bytes(info.data)
                stride = len(data) // self.h                 # rows may be padded
                if stride != self.w:
                    data = b"".join(data[r * stride:r * stride + self.w] for r in range(self.h))
                self.out.write(data)
                self.out.flush()
            except BrokenPipeError:
                self.loop.quit()
            finally:
                buf.unmap(info)
        return Gst.FlowReturn.OK

    def run(self):
        GLib.idle_add(lambda: (self.start(), False)[1])
        GLib.timeout_add_seconds(90, self._timeout)
        self.loop.run()

    def _timeout(self):
        if not hasattr(self, "pipe"):
            say("ERROR nobody approved screen sharing on the Pi's screen within 90 seconds")
            self.loop.quit()
        return False


if __name__ == "__main__":
    w, h, fps = (int(a) for a in sys.argv[1:4]) if len(sys.argv) > 3 else (1448, 1072, 6)
    try:
        Cast(w, h, fps).run()
    except dbus.exceptions.DBusException as e:
        say("ERROR %s" % e.get_dbus_message())
        sys.exit(1)
