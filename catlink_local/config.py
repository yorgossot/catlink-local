"""Configuration: CATLINK_* environment variables, parsed and validated once at startup.

Under systemd the variables come from /etc/catlink-local/catlink-local.env (EnvironmentFile=). Each
service calls load() once from its main(); a missing or malformed value raises ConfigError, whose message
names the variable but never echoes the value, and main() exits 2.

The variable table (names, defaults, required-ness) is documented in config/catlink-local.env.example
and docs/SETUP.md.
"""
import ipaddress
import os
import re
import time
import types

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
NODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
MAX_CATS = 3          # the tracker is generic in N; 3 is the most its tests cover
# A slug becomes object ids / state keys <slug>_weight, <slug>_visits, ... and the labels "unknown" / "two_cats"
# are taken: refuse the slugs that would collide with an existing entity, state key or label.
RESERVED_SLUGS = {"unknown", "last_visit", "last_deposit", "two_cat", "two_cats"}
CAT_SEED_MIN_KG = 0.2
CAT_SEED_MAX_KG = 12.0
DEFAULT_STATE_DIR = "/var/lib/catlink-local"
ROLES = ("gateway", "authresp")


class ConfigError(Exception):
    """A configuration problem. The message names the variable, never its value."""


def _get(env, name, default=""):
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _flag(env, name, default="0"):
    value = _get(env, name, default)
    if value not in ("0", "1"):
        raise ConfigError("%s must be 0 or 1" % name)
    return value == "1"


def _int(env, name, default, lo, hi):
    value = _get(env, name, str(default))
    try:
        number = int(value)
    except ValueError:
        raise ConfigError("%s must be an integer" % name) from None
    if not lo <= number <= hi:
        raise ConfigError("%s must be between %d and %d" % (name, lo, hi))
    return number


def _ipv4(env, name, default=""):
    value = _get(env, name, default)
    if value == "":
        return ""
    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise ConfigError("%s must be an IPv4 address" % name) from None


def _topic(env, name, default):
    value = _get(env, name, default)
    if any(c in value for c in "+#\x00") or value.startswith("/") or value.endswith("/") or "//" in value:
        raise ConfigError("%s must be a plain MQTT topic (no wildcards, no leading/trailing '/')" % name)
    return value


def parse_mac(value, name="CATLINK_BOX_MAC"):
    mac = (value or "").strip().lower().replace("-", ":")
    if not MAC_RE.match(mac):
        raise ConfigError("%s must be a MAC address written as six hex pairs, xx:xx:xx:xx:xx:xx" % name)
    return mac


def parse_cats(value):
    """'slug=kg[,slug=kg...]' -> [(slug, kg), ...] in the given order. '' -> []. The weights are rough seeds
    (a kitchen-scale guess is fine); they are never published as a cat's weight."""
    value = (value or "").strip()
    if not value:
        return []
    cats = []
    for item in value.split(","):
        item = item.strip()
        slug, sep, kg = item.partition("=")
        slug, kg = slug.strip(), kg.strip()
        if not sep or not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
            raise ConfigError("CATLINK_CATS entries must be slug=kg with slug [a-z][a-z0-9_]* "
                              "(not unknown/last_visit/last_deposit/two_cat/two_cats)")
        try:
            seed = round(float(kg), 3)
        except ValueError:
            raise ConfigError("CATLINK_CATS weights must be numbers in kg") from None
        if not CAT_SEED_MIN_KG <= seed <= CAT_SEED_MAX_KG:
            raise ConfigError("CATLINK_CATS weights must be between %.1f and %.1f kg"
                              % (CAT_SEED_MIN_KG, CAT_SEED_MAX_KG))
        cats.append((slug, seed))
    if len(cats) > MAX_CATS:
        raise ConfigError("CATLINK_CATS supports at most %d cats" % MAX_CATS)
    if len({slug for slug, _ in cats}) != len(cats):
        raise ConfigError("CATLINK_CATS slugs must be unique")
    return cats


def _state_file(env, name, filename):
    explicit = _get(env, name)
    if explicit:
        return explicit
    state_dir = env.get("STATE_DIRECTORY")
    base = state_dir.split(":")[0] if state_dir else DEFAULT_STATE_DIR
    return os.path.join(base, filename)


def _credential(env, name, filename):
    explicit = _get(env, name)
    if explicit:
        return explicit
    cred_dir = env.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        return os.path.join(cred_dir, filename)
    return ""


def load(role, env=None):
    """Parse and validate the environment for `role` ('gateway' or 'authresp'). Returns a namespace."""
    if role not in ROLES:
        raise ValueError("unknown role")
    env = os.environ if env is None else env
    c = types.SimpleNamespace(role=role)

    # --- shared: the box and where it is redirected to ---
    if not _get(env, "CATLINK_BOX_MAC"):
        raise ConfigError("CATLINK_BOX_MAC is required")
    c.box_mac = parse_mac(_get(env, "CATLINK_BOX_MAC"))
    c.box_mac_bytes = bytes.fromhex(c.box_mac.replace(":", ""))
    c.box_ip = _ipv4(env, "CATLINK_BOX_IP")
    if not _get(env, "CATLINK_BOX_BIND_IP"):
        raise ConfigError("CATLINK_BOX_BIND_IP is required")
    c.allow_any_bind = _flag(env, "CATLINK_ALLOW_ANY_BIND")
    c.box_bind_ip = _ipv4(env, "CATLINK_BOX_BIND_IP")
    c.box_bind_port = _int(env, "CATLINK_BOX_BIND_PORT", 1883, 1, 65535)
    c.skip_peer_check = _flag(env, "CATLINK_SKIP_PEER_CHECK")
    c.log_max_bytes = _int(env, "CATLINK_LOG_MAX_BYTES", 0, 0, 2 ** 40)   # 0 = no cap (journald rotates)
    if 0 < c.log_max_bytes < 4096:
        raise ConfigError("CATLINK_LOG_MAX_BYTES must be 0 (no cap) or at least 4096")

    # --- authresp ---
    c.auth_bind_ip = _ipv4(env, "CATLINK_AUTH_BIND_IP", c.box_bind_ip)
    c.auth_bind_port = _int(env, "CATLINK_AUTH_BIND_PORT", 443, 1, 65535)
    c.auth_cert = _credential(env, "CATLINK_AUTH_CERT", "cert.pem")
    c.auth_key = _credential(env, "CATLINK_AUTH_KEY", "key.pem")
    c.advertised_mqtt_host = _get(env, "CATLINK_ADVERTISED_MQTT_HOST", c.box_bind_ip)
    if not HOST_RE.match(c.advertised_mqtt_host):
        raise ConfigError("CATLINK_ADVERTISED_MQTT_HOST must be an IPv4 address or a host name")
    c.advertised_mqtt_port = _int(env, "CATLINK_ADVERTISED_MQTT_PORT", c.box_bind_port, 1, 65535)

    # --- gateway ---
    c.ha_host = _get(env, "CATLINK_HA_HOST", "127.0.0.1")
    if not HOST_RE.match(c.ha_host):
        raise ConfigError("CATLINK_HA_HOST must be an IPv4 address or a host name")
    c.ha_port = _int(env, "CATLINK_HA_PORT", 1883, 1, 65535)
    c.ha_user = _get(env, "CATLINK_HA_USER", "catlink")
    c.ha_pass_file = _credential(env, "CATLINK_HA_PASS_FILE", "ha_mqtt_pass")
    c.commands = _flag(env, "CATLINK_COMMANDS")
    c.discovery_prefix = _topic(env, "CATLINK_DISCOVERY_PREFIX", "homeassistant")
    c.base = _topic(env, "CATLINK_BASE", "catlink/litterbox")
    c.node = _get(env, "CATLINK_NODE", "catlink_litterbox")
    if not NODE_RE.match(c.node):
        raise ConfigError("CATLINK_NODE may contain only letters, digits, '_' and '-'")
    c.device_name = _get(env, "CATLINK_DEVICE_NAME", "CATLINK Open X")
    c.device_id = _get(env, "CATLINK_DEVICE_ID", "catlink_open_x")
    if not NODE_RE.match(c.device_id):
        raise ConfigError("CATLINK_DEVICE_ID may contain only letters, digits, '_' and '-'")
    c.cats = parse_cats(env.get("CATLINK_CATS", ""))
    c.visits_file = _state_file(env, "CATLINK_VISITS_FILE", "visits.jsonl")
    c.cats_file = _state_file(env, "CATLINK_CATS_FILE", "cats.json")
    c.litter_file = _state_file(env, "CATLINK_LITTER_FILE", "litter.json")
    c.no_ha = _flag(env, "CATLINK_NO_HA")

    # --- role-specific checks ---
    binds = [("CATLINK_BOX_BIND_IP", c.box_bind_ip)] if role == "gateway" else [("CATLINK_AUTH_BIND_IP", c.auth_bind_ip)]
    for name, ip in binds:
        if ip == "0.0.0.0" and not c.allow_any_bind:
            raise ConfigError("%s is 0.0.0.0; bind the host's LAN IP or set CATLINK_ALLOW_ANY_BIND=1" % name)
    if role == "authresp":
        if not c.auth_cert:
            raise ConfigError("CATLINK_AUTH_CERT is not set and no systemd credential directory is present")
        if not c.auth_key:
            raise ConfigError("CATLINK_AUTH_KEY is not set and no systemd credential directory is present")
    return c


# ----------------------------------------------------------------------------------------------------
# Peer pinning (both services): the box is recognised by its MAC in the kernel ARP table, and by its
# IP too when CATLINK_BOX_IP is set.
# ----------------------------------------------------------------------------------------------------
def mac_of(ip):
    try:
        with open("/proc/net/arp") as arp:
            for row in arp.readlines()[1:]:
                fields = row.split()
                if len(fields) >= 4 and fields[0] == ip:
                    return fields[3].lower()
    except OSError:
        pass
    return None


def peer_is_box(cfg, ip, attempts=1, delay=0.2):
    if cfg.skip_peer_check:
        return True
    if cfg.box_ip and ip != cfg.box_ip:
        return False
    for attempt in range(attempts):
        if mac_of(ip) == cfg.box_mac:
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False
