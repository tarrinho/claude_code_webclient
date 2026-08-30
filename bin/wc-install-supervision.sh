#!/usr/bin/env bash
# Install the systemd --user units that keep WebConsole running.
#
# Idempotent: safe to re-run after editing a unit file.
set -euo pipefail
cd "$(dirname "$0")/.."

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR" logs

echo "Installing units into $UNIT_DIR"
for unit in systemd/*.service systemd/*.timer; do
    install -m 644 "$unit" "$UNIT_DIR/$(basename "$unit")"
    echo "  $(basename "$unit")"
done

# Without linger, user services stop the moment the last session logs out --
# which for a box reached over SSH means the server dies when you disconnect.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo "Enabling linger so the units survive logout"
    loginctl enable-linger "$USER" || {
        echo "  could not enable linger; the units will stop at logout." >&2
        echo "  run: sudo loginctl enable-linger $USER" >&2
    }
fi

systemctl --user daemon-reload
systemctl --user enable --now webconsole-proxy.service
systemctl --user enable --now webconsole.service
systemctl --user enable --now webconsole-health.timer

echo
systemctl --user --no-pager --output=short status \
    webconsole-proxy.service webconsole.service 2>/dev/null | grep -E "●|Active:" || true
echo
echo "Manage with:"
echo "  systemctl --user status webconsole"
echo "  systemctl --user restart webconsole"
echo "  journalctl --user -u webconsole -f"
