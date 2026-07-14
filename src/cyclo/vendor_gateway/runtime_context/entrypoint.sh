#!/bin/sh
set -eu

uid="${CYCLO_HOST_UID:-1000}"
gid="${CYCLO_HOST_GID:-1000}"
user="${CYCLO_CONTAINER_USER:-cyclo}"
home_dir="${CYCLO_CONTAINER_HOME:-/home/cyclo}"

if getent group "$gid" >/dev/null 2>&1; then
    group_name="$(getent group "$gid" | cut -d: -f1)"
else
    group_name="$user"
    groupadd -g "$gid" "$group_name"
fi

if getent passwd "$uid" >/dev/null 2>&1; then
    user_name="$(getent passwd "$uid" | cut -d: -f1)"
else
    user_name="$user"
    useradd -m -d "$home_dir" -u "$uid" -g "$gid" -s /bin/bash "$user_name"
fi

mkdir -p "$home_dir"
chown "$uid:$gid" "$home_dir"

extra_groups="${CYCLO_EXTRA_GROUPS:-}"
if [ -n "$extra_groups" ]; then
    old_ifs="$IFS"
    IFS=:
    for extra_gid in $extra_groups; do
        [ -n "$extra_gid" ] || continue
        if getent group "$extra_gid" >/dev/null 2>&1; then
            extra_group="$(getent group "$extra_gid" | cut -d: -f1)"
        else
            extra_group="cyclo-g$extra_gid"
            groupadd -g "$extra_gid" "$extra_group"
        fi
        usermod -aG "$extra_group" "$user_name"
    done
    IFS="$old_ifs"
fi

export HOME="$home_dir"
exec setpriv --reuid="$uid" --regid="$gid" --init-groups -- "$@"
