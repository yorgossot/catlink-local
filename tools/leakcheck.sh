#!/usr/bin/env bash
# Generic leak check for this repository: device identifiers, private addresses, keys, captured frames,
# personal paths and dated one-off scripts must never be committed. Run from anywhere; scans the repo
# (excluding .git and this script). Prints file:line for every hit and exits 1 if there is any.
# CI runs it on every push; run it yourself before opening a pull request.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

# The one MAC address allowed in the tree: the synthetic, locally administered test MAC
# (02:ca:7b:0c:00:01), with ':' or '-' separators.
SYNTHETIC_MAC='02[:-]ca[:-]7b[:-]0c[:-]00[:-]01'

hits=0

report() {   # report <label> <grep output>
    if [ -n "$2" ]; then
        echo "== $1"
        echo "$2"
        hits=$((hits + 1))
    fi
}

scan() {     # scan <extra grep args...> -- content scan over the tree, file:line:text
    grep -rInE --exclude-dir=.git --exclude-dir=__pycache__ --exclude=leakcheck.sh "$@" . 2>/dev/null
}

# 1. MAC addresses other than the synthetic one (the synthetic literal is removed before matching).
MAC_RE='([0-9a-f]{2}[:-]){5}[0-9a-f]{2}'
report "MAC address" "$(scan -i "$MAC_RE" | sed -E "s/$SYNTHETIC_MAC//Ig" | grep -iE ":[0-9]+:.*$MAC_RE")"

# 2. Private IPv4 literals (docs use placeholders such as <host-lan-ip>).
report "private IPv4" "$(scan '(^|[^0-9.])(192\.168\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3})\.[0-9]{1,3}')"

# 3. Key material.
report "private key / certificate" "$(scan 'BEGIN .*PRIVATE KEY|BEGIN CERTIFICATE')"

# 4. Instance-specific IoT hostnames (the generic iot-auth-global names are allowed).
report "instance-specific IoT hostname" "$(scan -i 'iot-[0-9a-z]{6,}(\.|-x)' | sed -E 's/iot-auth-global(-x)?//Ig' | grep -iE ':[0-9]+:.*iot-[0-9a-z]{6,}(\.|-x)')"

# 5. Long hex runs outside tests/ (captured frames, device ids).
report "long hex run outside tests/" "$(scan -i --exclude-dir=tests '[0-9a-f]{24,}')"

# 6. Personal paths, bots, private notification hooks.
report "home path" "$(scan '/home/|/Users/')"
report "bot handle / private notifier" "$(scan -i '@[a-z0-9_]+_bot|telegram|rest_command')"

# 7. Files that must not exist: dated one-shot scripts, keys, certificates, logs, state.
report "forbidden file" "$(find . -path ./.git -prune -o -type f \( -name '*_20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' \
    -o -name '*.pem' -o -name '*.key' -o -name '*.jsonl' -o -name 'cats.json' -o -name '*.env' \) -print)"

if [ "$hits" -ne 0 ]; then
    echo "leakcheck: $hits categor$( [ "$hits" -eq 1 ] && echo y || echo ies ) with hits" >&2
    exit 1
fi
echo "leakcheck: clean"
exit 0
