# ssh-tunnel-button

This is a top-bar (app indicator) icon that can start a SSH process
for you.  The color (blue, orange, green) for (disconnected,
connecting, connected) indicates the state.  It can be used for
opening SSH tunnels on your request, but doesn't do it without your
action.  It is run by systemd.


## Below this line may be machine-genarated

GTK tray (AppIndicator) button that runs `ssh -D PORT HOST` as a child
process and tracks the tunnel state from ssh's stderr:

- blue = stopped, orange = connecting, green = SOCKS forward listening
  (detected from `ssh -v` output, with a fallback that probes
  `127.0.0.1:PORT` after 15 s).

Usage:

    ./ssh-tunnel-button.py HOST SOCKS_PORT [-l LABEL]

`-l/--label` (1–5 characters) draws the label on the tray icon and
prefixes logs/notifications, so several instances can run in parallel.
Icon SVGs live in `~/.cache/ssh-tunnel-button/` and are regenerated at
every startup (labeled instances get their own icon files).

systemd (parallel instances): the checked-in unit file is a template.
Install it as `~/.config/systemd/user/ssh-tunnel-button@.service`,
create `~/.config/ssh-tunnel-button/NAME.conf` with `SSH_HOST=` and
`SSH_PORT=` lines, then:

    systemctl --user daemon-reload
    systemctl --user enable --now ssh-tunnel-button@NAME.service

Just start for this session:

    systemctl --user start ssh-tunnel-button@A.service

The unit starts the tray button only; the tunnel itself is started and
stopped by clicking the icon. Unexpected ssh exits and port-in-use
errors raise a desktop notification. Logs:
`journalctl --user -u 'ssh-tunnel-button@*'`.
