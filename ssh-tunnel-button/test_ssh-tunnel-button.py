#!/usr/bin/env python3
"""Smoke test for ssh-tunnel-button.py with stubbed GTK/GLib/gi modules.

Exercises: state machine transitions (idle -> connecting -> connected),
partial-line stderr buffering, stderr mirroring to Python's stderr,
toggle dispatch (the start/stop bug), stop_tunnel cleanup, poll_process
reverting to idle on SSH exit, D-Bus notifications (with fallback and
throttling), the port-in-use startup warning, the CONNECTING port-probe
fallback, and reentrancy-safe signal handling.

Run:  python3 test_ssh-tunnel-button.py
"""

import argparse
import importlib.util
import io
import os
import signal
import sys
import time
import types
from pathlib import Path

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "ssh-tunnel-button.py"
)


# ---------------------------------------------------------------- stubs ---
class FakeWidget:
    def __init__(self, **kw):
        self.kw = kw
        self.label = kw.get("label")

    def connect(self, signal, handler):
        self.handler = handler

    def set_sensitive(self, v):
        pass

    def set_label(self, v):
        self.label = v

    def append(self, w):
        pass

    def show_all(self):
        pass


class FakeGtk(types.ModuleType):
    Menu = type("Menu", (FakeWidget,), {})
    MenuItem = type("MenuItem", (FakeWidget,), {})
    SeparatorMenuItem = type("SeparatorMenuItem", (FakeWidget,), {})

    @staticmethod
    def main():
        pass

    @staticmethod
    def main_quit():
        pass


class FakeGLib:
    IO_IN = 1
    IO_HUP = 8

    class Error(Exception):
        pass

    @staticmethod
    def Variant(signature, value):
        return (signature, value)

    @staticmethod
    def io_add_watch(fd, condition, callback):
        FakeGLib.last_watch = (fd, condition, callback)
        return 4242

    @staticmethod
    def idle_add(func, *args):
        FakeGLib.idles.append((func, args))
        return len(FakeGLib.idles)

    @staticmethod
    def run_idles():
        while FakeGLib.idles:
            func, args = FakeGLib.idles.pop(0)
            func(*args)

    @staticmethod
    def source_remove(source_id):
        FakeGLib.removed.append(source_id)

    @staticmethod
    def timeout_add_seconds(*a, **k):
        return 1


FakeGLib.removed = []
FakeGLib.last_watch = None
FakeGLib.idles = []


class FakeGio:
    BusType = types.SimpleNamespace(SESSION=1)
    DBusProxyFlags = types.SimpleNamespace(NONE=0)
    DBusCallFlags = types.SimpleNamespace(NONE=0)
    # Records (method, params) of every Notify call sent over the bus.
    notifications = []

    class DBusProxy:
        @staticmethod
        def new_for_bus_sync(*a, **k):
            return FakeGio

    @staticmethod
    def call_sync(method, params, flags, timeout, cancellable):
        FakeGio.notifications.append((method, params))


class FakeAppIndicator(types.ModuleType):
    class Indicator(FakeWidget):
        def set_icon_full(self, path, desc):
            FakeAppIndicator.icon = (path, desc)

        def set_status(self, status):
            pass

        def set_menu(self, menu):
            pass

        @classmethod
        def new(cls, *a, **k):
            return cls()

    IndicatorCategory = type("Cat", (), {"SYSTEM_SERVICES": 1})
    IndicatorStatus = type("Status", (), {"ACTIVE": 2})


FakeAppIndicator.icon = None


def make_fake_gi():
    gi_mod = types.ModuleType("gi")
    gi_mod.require_version = lambda *a, **k: None
    repo = types.ModuleType("gi.repository")
    repo.Gtk = FakeGtk("Gtk")
    repo.GLib = FakeGLib
    repo.Gio = FakeGio
    repo.AyatanaAppIndicator3 = FakeAppIndicator("AyatanaAppIndicator3")
    gi_mod.repository = repo
    sys.modules["gi"] = gi_mod
    sys.modules["gi.repository"] = repo
    return gi_mod, repo


# ---------------------------------------------------------------- load ---
make_fake_gi()
# Unusual port: the startup port-in-use check does a real localhost probe,
# so avoid ports likely to be occupied on the test machine.
sys.argv = ["ssh-tunnel-button.py", "example.com", "47823"]

spec = importlib.util.spec_from_file_location("stb", MODULE_PATH)
stb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stb)

failures = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)


# --------------------------------------------------------------- tests ---
# 1. Initial state: idle/blue, labels correct.
check("initial state is IDLE", stb.state == stb.STATE_IDLE)
check("idle icon is blue.svg", FakeAppIndicator.icon[0].endswith("blue.svg"))
check("idle toggle label", stb.toggle_item.label == "Start tunnel")
check("three icons written", all(
    os.path.exists(os.path.expanduser(f"~/.cache/ssh-tunnel-button/{f}"))
    for f in ("blue.svg", "orange.svg", "green.svg")
))
orig_port_in_use = stb.port_in_use

# 1b. Startup: port 47823 is (almost surely) free, so no notification.
check("no notification at startup (port free)", FakeGio.notifications == [])

# 2. start_tunnel: monkeypatch Popen with a fake long-running process.
class FakeProc:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.stderr = open(os.devnull, "rb")
        self._alive = True
        self.returncode = 0

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False
        self.returncode = -15  # SIGTERM

    def kill(self):
        self.killed = True
        self._alive = False
        self.returncode = -9  # SIGKILL

    def wait(self, timeout=None):
        return self.returncode


fake = FakeProc()
popen_kwargs = {}
stb.subprocess.Popen = lambda *a, **k: (popen_kwargs.update(k), fake)[1]
stb.start_tunnel(None)
check("start sets CONNECTING", stb.state == stb.STATE_CONNECTING)
check("start records connecting_since (probe fallback works)",
      stb.connecting_since is not None)
check("connecting icon is orange.svg", FakeAppIndicator.icon[0].endswith("orange.svg"))
check("connecting toggle label", stb.toggle_item.label == "Stop tunnel")
check("io watch registered", FakeGLib.last_watch is not None)
check("Popen captured stderr pipe", popen_kwargs.get("stderr") == stb.subprocess.PIPE)

# 3. on_stderr: partial marker split across two reads; also verify that
# ssh's stderr is mirrored to Python's stderr for logging.
fd, condition, callback = FakeGLib.last_watch
r, w = os.pipe()

mirrored = io.StringIO()
real_stderr = sys.stderr
sys.stderr = mirrored
stb.state = stb.STATE_CONNECTING
stb.stderr_buffer = ""
os.write(w, b"debug1: Local forward")
alive = callback(r, FakeGLib.IO_IN)
check("partial line does not connect", alive is True and stb.state == stb.STATE_CONNECTING)
check("partial line mirrored", "debug1: Local forward" in mirrored.getvalue())
os.write(w, b"ing listening on 127.0.0.1 port 1080.\n")
callback(r, FakeGLib.IO_IN)
check("marker completes transition", stb.state == stb.STATE_CONNECTED)
check("full line mirrored", "Local forwarding listening on 127.0.0.1 port 1080." in mirrored.getvalue())
check("connected icon is green.svg", FakeAppIndicator.icon[0].endswith("green.svg"))
check("connected toggle label", stb.toggle_item.label == "Stop tunnel")
os.write(w, b"debug1: whatever later\n")
callback(r, FakeGLib.IO_IN)
check("stays connected on later stderr", stb.state == stb.STATE_CONNECTED)
check("later stderr mirrored too", "debug1: whatever later" in mirrored.getvalue())
check("connected log message",
      "SSH tunnel connected (SOCKS forward listening)" in mirrored.getvalue())

# Broken/closed Python stderr must not kill the watcher.
class BrokenErr:
    def write(self, s):
        raise ValueError("I/O operation on closed file")

    def flush(self):
        pass

sys.stderr = BrokenErr()
os.write(w, b"debug1: after broken stderr\n")
check("watcher survives broken stderr",
      callback(r, FakeGLib.IO_IN) is True and stb.state == stb.STATE_CONNECTED)
sys.stderr = real_stderr
os.close(w)
os.close(r)

# EOF path: callback returns False and clears the watch id.
stb.stderr_watch_id = 4242
r2, w2 = os.pipe()
os.close(w2)
check("EOF returns False", callback(r2, FakeGLib.IO_HUP) is False)
check("EOF clears watch id", stb.stderr_watch_id is None)
os.close(r2)

# 4. Toggle dispatch: while running, clicking must STOP (the old bug),
# and the stop must be logged.
stb.state = stb.STATE_CONNECTED
stb.stderr_watch_id = None
fake._alive = True
stopcap = io.StringIO()
sys.stderr = stopcap
stb.toggle_tunnel(stb.toggle_item)
sys.stderr = real_stderr
check("toggle while running stops tunnel", fake.terminated and stb.process is None)
check("stop reverts to IDLE", stb.state == stb.STATE_IDLE)
check("idle icon restored", FakeAppIndicator.icon[0].endswith("blue.svg"))
check("toggle label back to start", stb.toggle_item.label == "Start tunnel")
check("stop logs stopping message", "Stopping SSH tunnel..." in stopcap.getvalue())
check("stop logs exit code", "SSH tunnel stopped (exit code -15)" in stopcap.getvalue())
check("no notification on manual stop", FakeGio.notifications == [])

# 5. Toggle when idle starts again.
fake.stderr = open(os.devnull, "rb")  # fresh pipe, as a real Popen would give
stb.toggle_tunnel(stb.toggle_item)
check("toggle while idle starts tunnel",
      stb.state == stb.STATE_CONNECTING and stb.process is fake)

# 6. poll_process reverts to IDLE when SSH dies on its own, and logs it.
fake._alive = False
fake.returncode = 255
polllcap = io.StringIO()
sys.stderr = polllcap
stb.poll_process()
sys.stderr = real_stderr
check("poll detects dead SSH -> IDLE", stb.process is None and stb.state == stb.STATE_IDLE)
check("poll logs unexpected exit",
      "ssh exited unexpectedly (code 255)" in polllcap.getvalue())
check("poll death notifies", any(
    "SSH tunnel failed" in str(n) for n in FakeGio.notifications))

# 6b. CONNECTING port-probe fallback: ssh that never prints the marker.
fake3 = FakeProc()
stb.subprocess.Popen = lambda *a, **k: fake3
stb.start_tunnel(None)
stb.connecting_since = time.monotonic() - stb.CONNECT_PROBE_DELAY - 1
stb.port_in_use = lambda *a, **k: True
stb.poll_process()
check("probe fallback marks CONNECTED", stb.state == stb.STATE_CONNECTED)
stb.toggle_tunnel(stb.toggle_item)  # clean up fake3

# 6c. ...but no probe fires before the deadline.
fake4 = FakeProc()
stb.subprocess.Popen = lambda *a, **k: fake4
stb.start_tunnel(None)
stb.connecting_since = time.monotonic()
stb.poll_process()
check("no probe before deadline", stb.state == stb.STATE_CONNECTING)
stb.toggle_tunnel(stb.toggle_item)  # clean up fake4
stb.port_in_use = orig_port_in_use

# 6d. Port-in-use warning at startup, with per-summary throttling.
stb.port_in_use = lambda *a, **k: True
stb._last_notify_times.clear()
stb.warn_if_port_in_use()
in_use_notes = [n for n in FakeGio.notifications if "already in use" in str(n)]
check("port-in-use notifies", len(in_use_notes) == 1)
stb.warn_if_port_in_use()
in_use_notes = [n for n in FakeGio.notifications if "already in use" in str(n)]
check("notifications are throttled", len(in_use_notes) == 1)
stb.port_in_use = orig_port_in_use

# 7. start failure path (OSError) keeps things consistent, and notifies.
def boom(*a, **k):
    raise OSError("no ssh binary")

stb.subprocess.Popen = boom
stb.start_tunnel(None)
check("start failure stays IDLE", stb.state == stb.STATE_IDLE and stb.process is None)
check("start failure notifies", any(
    "Could not start SSH" in str(n) for n in FakeGio.notifications))

# 8. quit_app stops the tunnel (and is idempotent via the quitting flag).
fake2 = FakeProc()
stb.subprocess.Popen = lambda *a, **k: fake2
stb.start_tunnel(None)
stb.quitting = False
FakeGLib.idles.clear()
stb.quit_app(None)
check("quit stops tunnel", fake2.terminated and stb.state == stb.STATE_IDLE)

# 9. Signal handler defers cleanup to the main loop (no reentrant stop).
fake5 = FakeProc()
stb.subprocess.Popen = lambda *a, **k: fake5
stb.start_tunnel(None)
sigcap = io.StringIO()
sys.stderr = sigcap
signal.getsignal(signal.SIGTERM)(None, None)  # simulate SIGTERM
sys.stderr = real_stderr
check("SIGTERM does not stop tunnel synchronously", stb.process is fake5)
check("SIGTERM schedules deferred quit", bool(FakeGLib.idles))
stb.quitting = False  # reset flag from the direct quit_app() in section 8
sys.stderr = sigcap
FakeGLib.run_idles()
sys.stderr = real_stderr
check("deferred quit stops tunnel",
      fake5.terminated and stb.process is None and stb.state == stb.STATE_IDLE)
check("cleanup ran exactly once",
      sigcap.getvalue().count("Stopping SSH tunnel...") == 1)

# 10. --label option: label validation, auto-scaling, and a labeled
# instance (second import) that gets its own tagged icons/menu/log prefix.
check("label accepts 1-5 chars (strips whitespace)",
      stb.label_type(" A ") == "A" and stb.label_type("abcde") == "abcde")
for bad in ("abcdef", ""):
    try:
        stb.label_type(bad)
        ok = False
    except argparse.ArgumentTypeError:
        ok = True
    check(f"label rejects {bad!r}", ok)
check("label font autoscales",
      stb.label_font_size("A") > stb.label_font_size("AB") > stb.label_font_size("ABCDE"))
check("punctuation labels get unique hash slugs",
      stb.label_slug("&&") != stb.label_slug("##") and len(stb.label_slug("&&")) == 8)

sys.argv = ["ssh-tunnel-button.py", "example.com", "47824", "--label=A"]
spec2 = importlib.util.spec_from_file_location("stb2", MODULE_PATH)
stb2 = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(stb2)

check("labeled instance uses tagged icon file",
      stb2.STATE_ICONS[stb2.STATE_IDLE].endswith("A-blue.svg"))
icon_svg = Path(stb2.STATE_ICONS[stb2.STATE_IDLE]).read_text()
check("label rendered inside icon SVG", ">A</text>" in icon_svg)
check("labeled icon circle fills canvas", 'r="12"' in icon_svg)
unlabeled_svg = Path(stb.STATE_ICONS[stb.STATE_IDLE]).read_text()
check("unlabeled icons remain plain",
      stb.STATE_ICONS[stb.STATE_IDLE].endswith("blue.svg") and "<text" not in unlabeled_svg)
check("unlabeled icon circle fills canvas", 'r="12"' in unlabeled_svg)
check("menu shows label prefix", stb2.ssh_item.label.startswith("[A] SSH proxy:"))
check("labeled log prefix", stb2.LOG_PREFIX == "ssh-tunnel-button[A]")
check("unlabeled log prefix unchanged", stb.LOG_PREFIX == "ssh-tunnel-button")
FakeGio.notifications.clear()
stb2.notify("label test")
check("notification summary prefixed", "[A] label test" in str(FakeGio.notifications[-1]))

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL TESTS PASSED")
