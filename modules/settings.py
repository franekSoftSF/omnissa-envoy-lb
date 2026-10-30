"""Wczytywanie, uzupełnianie wartości domyślnych i walidacja settings.yaml."""

from __future__ import annotations

import copy
import ipaddress
import json
import re
from pathlib import Path

from .common import EXIT_CONFIG, OmnissaError

try:  # PyYAML z venv (lub systemowy python3-yaml w trybie dry-run)
    import yaml
except ImportError:  # pragma: no cover - obsługa świeżego systemu w dry-run
    yaml = None

SERVICES = ("tunnel", "seg")

DEFAULTS: dict = {
    "version": 1,
    "deployment": {"name": "omnissa-lb"},
    "network": {
        "interface": "auto",
        "apply_mode": "runtime",  # runtime | netplan-apply
        "confirm_timeout": 120,
        "connectivity_targets": [],  # dodatkowe adresy do testu łączności (poza bramą)
    },
    "vip": {
        "mode": "shared",  # shared | dedicated
        "prefix": None,
        "shared": None,
        "tunnel": None,
        "seg": None,
        "duplicate_check": True,
        "gratuitous_arp": True,
    },
    "services": {
        "tunnel": {
            "enabled": True,
            "fqdn": None,
            "sni_aliases": [],
            "listeners": [
                {"protocol": "tcp", "port": 443, "backend_port": None},
                {"protocol": "udp", "port": 443, "backend_port": None},
            ],
            "backends": [],
            "lb_policy": "least_request",
            "session_affinity": "source_ip",
            "health_check": {},
        },
        "seg": {
            "enabled": True,
            "fqdn": None,
            "sni_aliases": [],
            "listeners": [{"protocol": "tcp", "port": 443, "backend_port": None}],
            "backends": [],
            "lb_policy": "least_request",
            "session_affinity": "none",
            "health_check": {},
        },
    },
    "health_check_defaults": {
        "type": "tcp",  # tcp | http | https
        "interval": "5s",
        "timeout": "2s",
        "unhealthy_threshold": 3,
        "healthy_threshold": 2,
        "path": "/favicon.ico",
        "host": None,
        "expected_statuses": [200],
        "port": None,
    },
    "sni": {
        "unknown_sni_action": "reject",  # reject | tunnel | seg
        "enforce_on_dedicated_vip": True,
    },
    "envoy": {
        "version": "",
        "source": "apt",
        "apt_suite": "",
        "apt_key_fingerprint": "",
        "admin_address": "127.0.0.1",
        "admin_port": 9901,
        "log_level": "info",
        "concurrency": 0,
        "connect_timeout": "5s",
        "tcp_idle_timeout": "3600s",
        "udp_idle_timeout": "300s",
        "max_connections_per_cluster": 50000,
        "max_downstream_connections": 100000,
        "drain_seconds": 15,
        "access_log": True,
    },
    "resources": {
        "memory_max": "2G",
        "cpu_quota": "",
        "nofile": 1048576,
        "tasks_max": 4096,
    },
    "firewall": {
        "manage": True,
        "backend": "auto",  # auto | nftables | ufw
        "restrict_vip": True,
        "allowed_sources": ["0.0.0.0/0"],
    },
    "monitoring": {
        "healthcheck_timer": True,
        "interval": "5min",
        "prometheus": {"enabled": False, "address": "127.0.0.1", "port": 9902},
    },
    "dns": {"verify": True, "require_match": False, "servers": []},
    "validation": {"backend_check": "critical", "sni_check": "critical"},
    "backup": {"keep": 20},
}

_FQDN_RE = re.compile(
    r"^(\*\.)?(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,15}$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)?$")
_SYSTEMD_SIZE_RE = re.compile(r"^(\d+[KMGT]?|infinity|\d{1,3}%)$")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def duration(value, field: str) -> str:
    """Konwertuje '500ms', '5s', '2m', 10 na format Envoy ('<n>s')."""
    m = _DURATION_RE.match(str(value).strip())
    if not m:
        raise OmnissaError(f"Nieprawidłowy czas w polu {field}: {value!r} (np. 5s, 500ms, 2m)", EXIT_CONFIG)
    num = float(m.group(1))
    factor = {"ms": 0.001, "s": 1, None: 1, "m": 60, "h": 3600}[m.group(2)]
    secs = num * factor
    return f"{secs:.3f}".rstrip("0").rstrip(".") + "s"


def read_file(path: Path) -> dict:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise OmnissaError(f"Nie znaleziono pliku konfiguracji: {path}", EXIT_CONFIG) from exc
    if yaml is not None:
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise OmnissaError(f"Błąd składni YAML w {path}: {exc}", EXIT_CONFIG) from exc
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OmnissaError(
                "Biblioteka PyYAML jest niedostępna (świeży system, tryb dry-run) "
                "i plik nie jest w formacie JSON.",
                EXIT_CONFIG,
                hint="Uruchom instalację bez --dry-run lub zainstaluj pakiet python3-yaml.",
            ) from exc
    if not isinstance(data, dict):
        raise OmnissaError(f"Plik {path} musi zawierać mapę YAML", EXIT_CONFIG)
    return data


def dump_yaml(data: dict) -> str:
    if yaml is None:
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def load(path: Path, interface_override: str | None = None) -> dict:
    raw = read_file(path)
    cfg = deep_merge(DEFAULTS, raw)
    if interface_override:
        cfg["network"]["interface"] = interface_override
    validate(cfg)
    return cfg


def _err(errors: list, msg: str) -> None:
    errors.append(msg)


def _ipv4(value, field: str, errors: list):
    try:
        ip = ipaddress.ip_address(str(value))
    except ValueError:
        _err(errors, f"{field}: nieprawidłowy adres IP {value!r}")
        return None
    if ip.version != 4:
        _err(errors, f"{field}: obsługiwane są adresy VIP IPv4 ({value})")
        return None
    if ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
        _err(errors, f"{field}: adres {value} nie może być użyty jako VIP")
        return None
    return ip


def validate(cfg: dict) -> None:
    """Waliduje konfigurację; zbiera wszystkie błędy i zgłasza je razem."""
    errors: list[str] = []
    net, vip = cfg["network"], cfg["vip"]

    iface = str(net.get("interface") or "auto")
    if iface != "auto" and not _IFACE_RE.match(iface):
        _err(errors, f"network.interface: nieprawidłowa nazwa interfejsu {iface!r}")
    if net["apply_mode"] not in ("runtime", "netplan-apply"):
        _err(errors, "network.apply_mode: dozwolone runtime | netplan-apply")
    for t in net.get("connectivity_targets") or []:
        try:
            ipaddress.ip_address(str(t))
        except ValueError:
            _err(errors, f"network.connectivity_targets: nieprawidłowy adres {t!r}")

    if vip["mode"] not in ("shared", "dedicated"):
        _err(errors, "vip.mode: dozwolone shared | dedicated")
    if vip.get("prefix") is not None and not (isinstance(vip["prefix"], int) and 8 <= vip["prefix"] <= 32):
        _err(errors, "vip.prefix: liczba 8-32")

    enabled = [s for s in SERVICES if cfg["services"][s].get("enabled")]
    if not enabled:
        _err(errors, "services: co najmniej jedna usługa (tunnel/seg) musi być włączona")

    if vip["mode"] == "shared":
        if not vip.get("shared"):
            _err(errors, "vip.shared: wymagany adres VIP w trybie shared")
        else:
            _ipv4(vip["shared"], "vip.shared", errors)
    else:
        seen = {}
        for s in enabled:
            if not vip.get(s):
                _err(errors, f"vip.{s}: wymagany adres VIP w trybie dedicated")
                continue
            ip = _ipv4(vip[s], f"vip.{s}", errors)
            if ip and str(ip) in seen:
                _err(errors, f"vip.{s}: adres {ip} jest już używany przez {seen[str(ip)]} (użyj trybu shared)")
            if ip:
                seen[str(ip)] = s

    hc_defaults = cfg["health_check_defaults"]
    fqdns = {}
    for s in SERVICES:
        svc = cfg["services"][s]
        if not svc.get("enabled"):
            continue
        p = f"services.{s}"
        fqdn = svc.get("fqdn")
        if not fqdn or not _FQDN_RE.match(str(fqdn)):
            _err(errors, f"{p}.fqdn: wymagany prawidłowy FQDN (np. {s}.example.com), podano {fqdn!r}")
        else:
            svc["fqdn"] = str(fqdn).lower().rstrip(".")
        aliases = []
        for a in svc.get("sni_aliases") or []:
            if not _FQDN_RE.match(str(a)):
                _err(errors, f"{p}.sni_aliases: nieprawidłowa nazwa {a!r}")
            aliases.append(str(a).lower())
        svc["sni_aliases"] = aliases
        for name in [svc.get("fqdn")] + aliases:
            if not name:
                continue
            if name in fqdns:
                _err(errors, f"{p}: nazwa SNI {name} jest już przypisana do {fqdns[name]}")
            fqdns[name] = s

        if not svc.get("listeners"):
            _err(errors, f"{p}.listeners: wymagany co najmniej jeden listener")
        norm_listeners = []
        for i, lst in enumerate(svc.get("listeners") or []):
            proto = str(lst.get("protocol", "tcp")).lower()
            if proto not in ("tcp", "udp"):
                _err(errors, f"{p}.listeners[{i}].protocol: tcp | udp")
            port = lst.get("port")
            bport = lst.get("backend_port") or port
            for fld, val in (("port", port), ("backend_port", bport)):
                if not isinstance(val, int) or not 1 <= val <= 65535:
                    _err(errors, f"{p}.listeners[{i}].{fld}: port 1-65535, podano {val!r}")
            norm_listeners.append({"protocol": proto, "port": port, "backend_port": bport})
        svc["listeners"] = norm_listeners

        if not svc.get("backends"):
            _err(errors, f"{p}.backends: wymagany co najmniej jeden backend (np. węzeł UAG)")
        norm_backends = []
        for i, b in enumerate(svc.get("backends") or []):
            if isinstance(b, str):
                b = {"address": b}
            addr = str(b.get("address", ""))
            is_ip = True
            try:
                ipaddress.ip_address(addr)
            except ValueError:
                is_ip = False
                if not _FQDN_RE.match(addr):
                    _err(errors, f"{p}.backends[{i}].address: nieprawidłowy adres/FQDN {addr!r}")
            weight = b.get("weight", 1)
            if not isinstance(weight, int) or not 1 <= weight <= 128:
                _err(errors, f"{p}.backends[{i}].weight: liczba 1-128")
            norm_backends.append({"address": addr, "weight": weight, "is_ip": is_ip})
        svc["backends"] = norm_backends
        if any(not b["is_ip"] for b in norm_backends) and any(b["is_ip"] for b in norm_backends):
            _err(errors, f"{p}.backends: nie mieszaj adresów IP i nazw DNS w jednej usłudze")

        if svc.get("lb_policy") not in ("least_request", "round_robin", "random", "ring_hash", "maglev"):
            _err(errors, f"{p}.lb_policy: least_request | round_robin | random | ring_hash | maglev")
        if svc.get("session_affinity") not in ("none", "source_ip"):
            _err(errors, f"{p}.session_affinity: none | source_ip")

        hc = deep_merge(hc_defaults, svc.get("health_check") or {})
        if hc["type"] not in ("tcp", "http", "https", "none"):
            _err(errors, f"{p}.health_check.type: tcp | http | https | none")
        try:
            hc["interval"] = duration(hc["interval"], f"{p}.health_check.interval")
            hc["timeout"] = duration(hc["timeout"], f"{p}.health_check.timeout")
        except OmnissaError as exc:
            _err(errors, str(exc))
        if hc.get("port") is not None and not (isinstance(hc["port"], int) and 1 <= hc["port"] <= 65535):
            _err(errors, f"{p}.health_check.port: port 1-65535")
        if not str(hc.get("path", "/")).startswith("/"):
            _err(errors, f"{p}.health_check.path musi zaczynać się od '/'")
        svc["health_check"] = hc

    # Konflikty listenerów
    seen_l: dict = {}
    for s in enabled:
        for lst in cfg["services"][s]["listeners"]:
            key = (vip_for(cfg, s), lst["protocol"], lst["port"])
            seen_l.setdefault(key, []).append(s)
    for (addr, proto, port), svcs in seen_l.items():
        if len(svcs) > 1 and proto == "udp":
            _err(
                errors,
                f"UDP {addr}:{port} współdzielony przez {', '.join(svcs)} - UDP nie obsługuje SNI; "
                "użyj innych portów UDP lub trybu dedicated",
            )
        if len(set(svcs)) != len(svcs):
            _err(errors, f"Usługa zdefiniowała listener {proto}/{port} wielokrotnie")
    if cfg["sni"]["unknown_sni_action"] not in ("reject", "tunnel", "seg"):
        _err(errors, "sni.unknown_sni_action: reject | tunnel | seg")
    elif cfg["sni"]["unknown_sni_action"] != "reject" and cfg["sni"]["unknown_sni_action"] not in enabled:
        _err(errors, "sni.unknown_sni_action wskazuje na wyłączoną usługę")

    env = cfg["envoy"]
    if env.get("version") and not re.match(r"^\d+\.\d+(\.\d+)?$", str(env["version"])):
        _err(errors, "envoy.version: format X.Y lub X.Y.Z")
    if env.get("source") not in ("apt",):
        _err(errors, "envoy.source: obsługiwane jest 'apt' (oficjalne repozytorium apt.envoyproxy.io)")
    if env.get("log_level") not in ("trace", "debug", "info", "warning", "error", "critical", "off"):
        _err(errors, "envoy.log_level: trace|debug|info|warning|error|critical|off")
    try:
        ip = ipaddress.ip_address(str(env["admin_address"]))
        if not ip.is_loopback:
            _err(errors, "envoy.admin_address: interfejs administracyjny Envoy musi być na adresie loopback")
    except ValueError:
        _err(errors, "envoy.admin_address: nieprawidłowy adres")
    for k in ("admin_port", "drain_seconds", "max_connections_per_cluster", "max_downstream_connections", "concurrency"):
        if not isinstance(env.get(k), int) or env[k] < 0:
            _err(errors, f"envoy.{k}: liczba całkowita >= 0")
    for k in ("connect_timeout", "tcp_idle_timeout", "udp_idle_timeout"):
        try:
            env[k] = duration(env[k], f"envoy.{k}")
        except OmnissaError as exc:
            _err(errors, str(exc))

    res = cfg["resources"]
    if res.get("memory_max") and not _SYSTEMD_SIZE_RE.match(str(res["memory_max"])):
        _err(errors, "resources.memory_max: np. 2G, 512M, 80%, infinity")
    if res.get("cpu_quota") and not re.match(r"^\d{1,4}%$", str(res["cpu_quota"])):
        _err(errors, "resources.cpu_quota: np. 200%")
    for k in ("nofile", "tasks_max"):
        if not isinstance(res.get(k), int) or res[k] < 1024:
            _err(errors, f"resources.{k}: liczba >= 1024")

    fw = cfg["firewall"]
    if fw["backend"] not in ("auto", "nftables", "ufw"):
        _err(errors, "firewall.backend: auto | nftables | ufw")
    for src in fw.get("allowed_sources") or []:
        try:
            ipaddress.ip_network(str(src), strict=False)
        except ValueError:
            _err(errors, f"firewall.allowed_sources: nieprawidłowa sieć {src!r}")
    if not fw.get("allowed_sources"):
        _err(errors, "firewall.allowed_sources: lista nie może być pusta (np. 0.0.0.0/0)")

    mon = cfg["monitoring"]
    if not re.match(r"^\d+(s|min|h)$", str(mon.get("interval", ""))):
        _err(errors, "monitoring.interval: np. 60s, 5min, 1h")
    prom = mon["prometheus"]
    if prom.get("enabled"):
        if not isinstance(prom.get("port"), int) or not 1 <= prom["port"] <= 65535:
            _err(errors, "monitoring.prometheus.port: port 1-65535")
        try:
            ipaddress.ip_address(str(prom.get("address")))
        except ValueError:
            _err(errors, "monitoring.prometheus.address: nieprawidłowy adres")

    for k in ("backend_check", "sni_check"):
        if cfg["validation"].get(k) not in ("critical", "warning", "skip"):
            _err(errors, f"validation.{k}: critical | warning | skip")
    for srv in cfg["dns"].get("servers") or []:
        try:
            ipaddress.ip_address(str(srv))
        except ValueError:
            _err(errors, f"dns.servers: nieprawidłowy adres {srv!r}")

    if errors:
        raise OmnissaError(
            "Konfiguracja zawiera błędy:\n  - " + "\n  - ".join(errors),
            EXIT_CONFIG,
            hint="Popraw plik settings.yaml (zob. config/settings.example.yaml).",
        )


def enabled_services(cfg: dict) -> list[str]:
    return [s for s in SERVICES if cfg["services"][s].get("enabled")]


def vip_for(cfg: dict, service: str) -> str:
    vip = cfg["vip"]
    return str(vip["shared"] if vip["mode"] == "shared" else vip[service])


def all_vips(cfg: dict) -> list[str]:
    out = []
    for s in enabled_services(cfg):
        v = vip_for(cfg, s)
        if v not in out:
            out.append(v)
    return out
