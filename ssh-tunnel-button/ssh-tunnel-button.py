#!/usr/bin/env python3

# sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
"""
Tray button that manages an `ssh -D` SOCKS tunnel.

Usage: ssh-tunnel-button.py ssh_host socks_port [--label LABEL]

--label draws a short tag (1-5 characters) on the tray icon so that
several parallel tunnels can be told apart at a glance.

Icons are (re)generated at every startup into ~/.cache/ssh-tunnel-button/
(one SVG per state, prefixed with the --label slug for labeled instances);
the directory is shared by all parallel instances.
"""


import argparse
import hashlib
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

# Parse the command line before touching GTK, so --help and argument
# errors work even on machines where GTK is missing or broken.
def label_type(value):
    # Validate --label: 1-5 characters after stripping whitespace.
    value = value.strip()
    if not 1 <= len(value) <= 5:
        raise argparse.ArgumentTypeError("label must be 1-5 characters")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="ssh-tunnel-button",
        description="Tray button that manages an `ssh -D` SOCKS tunnel.",
    )
    parser.add_argument("ssh_host", help="ssh target host")
    parser.add_argument("socks_port", type=int, help="local SOCKS port (-D)")
    parser.add_argument(
        "--label", "-l",
        type=label_type,
        default=None,
        metavar="LABEL",
        help="short label (1-5 characters) drawn on the tray icon to tell "
        "parallel tunnel instances apart, e.g. --label=A",
    )
    return parser.parse_args(argv)


_args = parse_args(sys.argv[1:])
SSH_TARGET = _args.ssh_host  # ssh TARGET
FORWARD = _args.socks_port   # -D FORWARD
LABEL = _args.label or ""    # None (no --label) means unlabeled

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gio

# Current Ubuntu uses AyatanaAppIndicator; some older versions use AppIndicator3.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except ValueError:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator

SSH_COMMAND = [
    "ssh",
    "-v",
    "-N",  # Don't run a remote shell; keep SSH in the foreground to monitor it.
    "-D", str(FORWARD),
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "BatchMode=yes",
    "--",
    SSH_TARGET,
]

ICON_DIR = Path.home() / ".cache" / "ssh-tunnel-button"
ICON_DIR.mkdir(parents=True, exist_ok=True)

# Label text font size by label length, so 1-2 characters are large and
# readable while longer labels shrink to fit inside the circle.
LABEL_FONT_SIZES = {1: 15, 2: 13, 3: 11, 4: 9, 5: 7.5}


def label_font_size(label):
    return LABEL_FONT_SIZES.get(len(label), 6.5)


def label_slug(label):
    # Filename-safe identifier for labeled icons; parallel instances must
    # not overwrite each other's icon files.
    slug = re.sub(r"[^A-Za-z0-9]", "", label)
    return slug or hashlib.md5(label.encode()).hexdigest()[:8]


def write_icon(filename, color):
    if LABEL:
        path = ICON_DIR / f"{label_slug(LABEL)}-{filename}"
        fs = label_font_size(LABEL)
        # Baseline y offset (~0.35 * font size) vertically centers the text.
        text = (
            f'<text x="12" y="{12 + fs * 0.35:.1f}" font-family="sans-serif" '
            f'font-size="{fs}" font-weight="bold" fill="#ffffff" '
            f'text-anchor="middle">{escape(LABEL)}</text>'
        )
    else:
        path = ICON_DIR / filename
        text = ""
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"
        viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="12" fill="{color}"/>{text}
        </svg>"""
    )
    return str(path)


# Tunnel states: blue = not connected, orange = connecting, green = connected.
STATE_IDLE = "idle"  # Not connected, ready to start.
STATE_CONNECTING = "connecting"  # SSH started, forward not confirmed yet.
STATE_CONNECTED = "connected"  # SOCKS forward is listening.

STATES = {
    STATE_IDLE: ("blue.svg", "#1e88e5", "Tunnel: stopped", "Start tunnel"),
    STATE_CONNECTING: ("orange.svg", "#fb8c00", "Tunnel: connecting…", "Stop tunnel"),
    STATE_CONNECTED: ("green.svg", "#43a047", "Tunnel: active", "Stop tunnel"),
}

STATE_ICONS = {
    state: write_icon(filename, color)
    for state, (filename, color, _status, _toggle) in STATES.items()
}

# With `ssh -v`, this line appears on stderr once the SOCKS forward is
# actually listening (authentication succeeded and forwards are set up).
CONNECTED_MARKER = "local forwarding listening"
CONNECT_PROBE_DELAY = 15  # Seconds; port-probe fallback if marker unseen.

process = None
state = STATE_IDLE
stderr_buffer = ""
stderr_watch_id = None
connecting_since = None
quitting = False


LOG_PREFIX = f"ssh-tunnel-button[{LABEL}]" if LABEL else "ssh-tunnel-button"


def log(message):
    # Log to stderr so it lands in the systemd journal alongside the
    # mirrored ssh output.
    print(f"{LOG_PREFIX}: {message}", file=sys.stderr, flush=True)


def mirror_text(text):
    # Mirror ssh output to Python's stderr for logging.
    try:
        sys.stderr.write(text)
        sys.stderr.flush()
    except (OSError, ValueError):
        pass  # Our stderr is closed; keep going anyway.


def drain_stderr(proc):
    # Mirror any final ssh output (e.g. "Connection to ... closed.") that
    # arrived between the last io-watch read and process exit.
    try:
        fd = proc.stderr.fileno()
    except (OSError, ValueError, AttributeError):
        return
    while True:
        try:
            data = os.read(fd, 4096)
        except OSError:
            break
        if not data:
            break
        mirror_text(data.decode(errors="replace"))


# --- Desktop notifications (org.freedesktop.Notifications via D-Bus) ------

NOTIFY_DBUS_TIMEOUT_MS = 2000
NOTIFY_THROTTLE_SECONDS = 60  # Per summary; avoids spam on restart loops.

_notification_proxy = None
_notification_checked = False
_last_notify_times = {}


def _get_notification_proxy():
    global _notification_proxy, _notification_checked
    if not _notification_checked:
        _notification_checked = True
        try:
            _notification_proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications",
                "org.freedesktop.Notifications",
                None,
            )
        except GLib.Error as error:
            log(f"Desktop notifications unavailable: {error}")
            _notification_proxy = None
    return _notification_proxy


def notify(summary, body=""):
    # Journal first: notifications are best-effort, logging is not.
    if LABEL:
        summary = f"[{LABEL}] {summary}"
    log(f"{summary}" + (f" | {body}" if body else ""))
    now = time.monotonic()
    if now - _last_notify_times.get(summary, 0.0) < NOTIFY_THROTTLE_SECONDS:
        return
    _last_notify_times[summary] = now

    proxy = _get_notification_proxy()
    if proxy is not None:
        try:
            proxy.call_sync(
                "Notify",
                GLib.Variant(
                    "(susssasa{sv}i)",
                    (
                        "ssh-tunnel-button",
                        0,
                        "dialog-warning",
                        summary,
                        body,
                        [],
                        {},
                        NOTIFY_DBUS_TIMEOUT_MS,
                    ),
                ),
                Gio.DBusCallFlags.NONE,
                NOTIFY_DBUS_TIMEOUT_MS,
                None,
            )
            return
        except GLib.Error as error:
            log(f"Notification D-Bus call failed: {error}")
    try:
        subprocess.run(
            ["notify-send", summary, body],
            timeout=NOTIFY_DBUS_TIMEOUT_MS / 1000,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass  # Journal-only logging is all that is available.


# --- Port probing ---------------------------------------------------------


def port_in_use(port):
    # True if something is accepting TCP connections on localhost:port.
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def warn_if_port_in_use():
    if port_in_use(FORWARD):
        notify(
            f"Port {FORWARD} already in use",
            "An ssh tunnel from a previous run (or another program) may be "
            f"holding port {FORWARD}; a new tunnel cannot start until it exits.",
        )


def running():
    return process is not None and process.poll() is None


def update_ui():
    _filename, _color, status_label, toggle_label = STATES[state]
    indicator.set_icon_full(STATE_ICONS[state], status_label)
    status_item.set_label(status_label)
    toggle_item.set_label(toggle_label)


def on_stderr(fd, _condition):
    global stderr_buffer, state, stderr_watch_id
    try:
        data = os.read(fd, 4096)
    except OSError:
        data = b""
    if data:
        text = data.decode(errors="replace")
        mirror_text(text)
        stderr_buffer += text
        *lines, stderr_buffer = stderr_buffer.split("\n")
        for line in lines:
            if state == STATE_CONNECTING and CONNECTED_MARKER in line.lower():
                state = STATE_CONNECTED
                log("SSH tunnel connected (SOCKS forward listening)")
                update_ui()
        return True  # Keep reading stderr.
    # EOF or hang-up: the poll timer finalizes the state transition.
    stderr_watch_id = None
    return False  # Stop watching.


def watch_stderr(proc):
    global stderr_buffer, stderr_watch_id
    stderr_buffer = ""
    if stderr_watch_id is not None:
        GLib.source_remove(stderr_watch_id)
    stderr_watch_id = GLib.io_add_watch(
        proc.stderr.fileno(),
        GLib.IO_IN | GLib.IO_HUP,
        on_stderr,
    )


def unwatch_stderr():
    global stderr_watch_id
    if stderr_watch_id is not None:
        GLib.source_remove(stderr_watch_id)
        stderr_watch_id = None


def start_tunnel(_item):
    global process, state, connecting_since
    if running():
        return

    try:
        # stdout is deliberately left inherited, so ssh's stdout is
        # mirrored to this process's stdout; stderr is piped (see below)
        # for connection-state detection and mirrored manually.
        process = subprocess.Popen(
            SSH_COMMAND,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        notify("Could not start SSH", str(error))
        process = None
        state = STATE_IDLE
        update_ui()
        return

    log(f"Starting SSH tunnel: ssh -D {FORWARD} {SSH_TARGET}")
    state = STATE_CONNECTING
    connecting_since = time.monotonic()
    watch_stderr(process)
    update_ui()


def stop_tunnel(_item=None):
    global process, state, connecting_since
    unwatch_stderr()
    if process is None:
        state = STATE_IDLE
        connecting_since = None
        update_ui()
        return

    if not running():
        # SSH already exited on its own (before the poll timer noticed).
        drain_stderr(process)
        if process.stderr:
            process.stderr.close()
        log(f"SSH tunnel exited (exit code {process.returncode})")
        process = None
        state = STATE_IDLE
        connecting_since = None
        update_ui()
        return

    log("Stopping SSH tunnel...")
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    drain_stderr(process)
    if process.stderr:
        process.stderr.close()
    log(f"SSH tunnel stopped (exit code {process.returncode})")
    process = None
    state = STATE_IDLE
    connecting_since = None
    update_ui()


def toggle_tunnel(_item):
    # The menu item doubles as "Start tunnel" / "Stop tunnel"; dispatch
    # based on the actual state rather than the label.
    if running():
        stop_tunnel(_item)
    else:
        start_tunnel(_item)


def poll_process():
    global process, state, connecting_since
    if process is not None and not running():
        unwatch_stderr()
        drain_stderr(process)
        if process.stderr:
            process.stderr.close()
        notify(
            "SSH tunnel failed",
            f"ssh exited unexpectedly (code {process.returncode}). "
            "See journalctl --user -u 'ssh-tunnel-button@*'",
        )
        process = None
        state = STATE_IDLE
        connecting_since = None
        update_ui()
    elif process is not None and state == STATE_CONNECTING:
        # Fallback for ssh builds that never print the "connected" marker:
        # after the deadline, consider the tunnel up if the SOCKS port is
        # accepting connections. (Localhost connects return instantly,
        # refused or not.)
        if (
            connecting_since is not None
            and time.monotonic() - connecting_since > CONNECT_PROBE_DELAY
            and port_in_use(FORWARD)
        ):
            state = STATE_CONNECTED
            connecting_since = None
            log("SSH tunnel connected (port probe; marker not seen in ssh output)")
            update_ui()
    return True  # Keep polling.


def quit_app(_item=None):
    global quitting
    if quitting:
        return
    quitting = True
    stop_tunnel()
    Gtk.main_quit()


indicator = AppIndicator.Indicator.new(
    "ssh-tunnel-indicator" + (f"-{LABEL}" if LABEL else ""),
    STATE_ICONS[STATE_IDLE],
    AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
)
indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

menu = Gtk.Menu()

label_prefix = f"[{LABEL}] " if LABEL else ""
ssh_item = Gtk.MenuItem(label=f"{label_prefix}SSH proxy: {FORWARD} -> {SSH_TARGET} ")
ssh_item.set_sensitive(False)
menu.append(ssh_item)


status_item = Gtk.MenuItem(label="Tunnel: stopped")
status_item.set_sensitive(False)
menu.append(status_item)

toggle_item = Gtk.MenuItem(label="Start tunnel")
toggle_item.connect("activate", toggle_tunnel)
menu.append(toggle_item)

menu.append(Gtk.SeparatorMenuItem())

quit_item = Gtk.MenuItem(label="Quit")
quit_item.connect("activate", quit_app)
menu.append(quit_item)

menu.show_all()
indicator.set_menu(menu)
update_ui()

warn_if_port_in_use()

GLib.timeout_add_seconds(1, poll_process)

# Try to stop the child SSH process if the indicator is interrupted.
# Cleanup is deferred to the main loop (GLib.idle_add) and never runs in
# the signal handler itself: a signal can arrive while stop_tunnel() is
# already waiting on the ssh process, and re-entering it would corrupt
# the state (and crash on process=None).
signal.signal(signal.SIGINT, lambda *_: GLib.idle_add(quit_app, None))
signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(quit_app, None))

Gtk.main()
