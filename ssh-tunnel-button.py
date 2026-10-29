#!/usr/bin/env python3

# sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1
"""
Arguments: ssh_host socks_port
"""


import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib

# Current Ubuntu uses AyatanaAppIndicator; some older versions use AppIndicator3.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except ValueError:
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator


SSH_TARGET = sys.argv[1]    # ssh TARGET
FORWARD = int(sys.argv[2])  # -D FORWARD

SSH_COMMAND = [
    "ssh",
    "-v",
    "-N",  # Don't run a remote shell; keep SSH in the foreground to monitor it.
    "-D", str(FORWARD),
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "BatchMode=yes",
    SSH_TARGET,
]

ICON_DIR = Path.home() / ".cache" / "ssh-tunnel-button"
ICON_DIR.mkdir(parents=True, exist_ok=True)


def write_icon(filename, color):
    path = ICON_DIR / filename
    path.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"
        viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="9" fill="{color}"/>
        </svg>"""
    )
    return str(path)


RED_ICON = write_icon("red.svg", "#e53935")
GREEN_ICON = write_icon("green.svg", "#43a047")

process = None


def running():
    return process is not None and process.poll() is None


def update_ui():
    active = running()
    indicator.set_icon_full(
        GREEN_ICON if active else RED_ICON,
        "SSH tunnel active" if active else "SSH tunnel stopped",
    )
    status_item.set_label("Tunnel: active" if active else "Tunnel: stopped")
    toggle_item.set_label("Stop tunnel" if active else "Start tunnel")


def start_tunnel(_item):
    global process
    if running():
        return

    try:
        process = subprocess.Popen(
            SSH_COMMAND,
            stdin=subprocess.DEVNULL,
        )
    except OSError as error:
        print(f"Could not start SSH: {error}")
        process = None

    update_ui()


def stop_tunnel(_item=None):
    global process
    if not running():
        process = None
        update_ui()
        return

    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    process = None
    update_ui()


def poll_process():
    # Also catches SSH exiting unexpectedly.
    update_ui()
    return True  # Keep polling.


def quit_app(_item):
    stop_tunnel()
    Gtk.main_quit()


indicator = AppIndicator.Indicator.new(
    "ssh-tunnel-indicator",
    RED_ICON,
    AppIndicator.IndicatorCategory.SYSTEM_SERVICES,
)
indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

menu = Gtk.Menu()

ssh_item = Gtk.MenuItem(label=f"SSH proxy: {FORWARD} -> {SSH_TARGET} ")
ssh_item.set_sensitive(False)
menu.append(ssh_item)


status_item = Gtk.MenuItem(label="Tunnel: stopped")
status_item.set_sensitive(False)
menu.append(status_item)

toggle_item = Gtk.MenuItem(label="Start tunnel")
toggle_item.connect("activate", start_tunnel)
menu.append(toggle_item)

menu.append(Gtk.SeparatorMenuItem())

quit_item = Gtk.MenuItem(label="Quit")
quit_item.connect("activate", quit_app)
menu.append(quit_item)

menu.show_all()
indicator.set_menu(menu)
update_ui()

GLib.timeout_add_seconds(1, poll_process)

# Try to stop the child SSH process if the indicator is interrupted.
signal.signal(signal.SIGINT, lambda *_: quit_app(None))
signal.signal(signal.SIGTERM, lambda *_: quit_app(None))

Gtk.main()
