#!/bin/sh
set -eu

if ! getent passwd cyclo >/dev/null 2>&1; then
    echo "cyclo runtime user is missing" >&2
    exit 70
fi
user_name=cyclo
uid="$(id -u "$user_name")"
gid="$(id -g "$user_name")"

export HOME=/home/cyclo
if [ "$(id -u)" -eq 0 ]; then
    exec setpriv --reuid="$uid" --regid="$gid" --init-groups -- "$0" "$@"
fi

if [ "${CYCLO_ADMIN_TOOL:-}" = "1" ]; then
    exec "$@"
fi

settings_template=/opt/cyclo/pi-settings.json
if [ ! -f "$settings_template" ]; then
    echo "Cyclo Pi settings template is missing" >&2
    exit 70
fi

# The host supplies only an immutable template. All writes happen after
# dropping privilege and remain inside the team's private Pi state bind.
umask 077
mkdir -p "$HOME/.pi/agent"
temporary="$(mktemp "$HOME/.pi/agent/.settings.json.XXXXXX")"
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
cp -- "$settings_template" "$temporary"
chmod 0600 "$temporary"
mv -f -- "$temporary" "$HOME/.pi/agent/settings.json"
trap - EXIT HUP INT TERM

exec "$@"
