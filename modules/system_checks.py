"""Kontrole wstępne systemu (wykonywane także w trybie dry-run - tylko odczyt)."""

from __future__ import annotations

import platform
import re
import sys
from pathlib import Path

from . import network_manager as net
from .common import EXIT_CONFIG, OmnissaError, is_root, log, run
from .settings import all_vips, enabled_services, vip_for

SUPPORTED = ("24.04", "26.04")


def os_release() -> dict:
    data = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                data[k] = v.strip().strip('"')
    except FileNotFoundError:
        pass
    return data


def system_info() -> dict:
    osr = os_release()
    arch = run(["dpkg", "--print-architecture"], check=False).stdout.strip() or platform.machine()
    return {
        "os": osr.get("PRETTY_NAME", "unknown"),
        "id": osr.get("ID"),
        "version": osr.get("VERSION_ID"),
        "codename": osr.get("VERSION_CODENAME"),
        "arch": arch,
        "kernel": platform.release(),
        "python": sys.version.split()[0],
        "hostname": platform.node(),
        "root": is_root(),
    }


def check_supported() -> dict:
    info = system_info()
    if info["id"] != "ubuntu" or info["version"] not in SUPPORTED:
        raise OmnissaError(f"Nieobsługiwany system: {info['os']} (wymagane Ubuntu {' / '.join(SUPPORTED)})")
    return info


def port_conflicts(cfg: dict) -> list[str]:
    """Wykrywa inne procesy nasłuchujące na portach VIP (np. nginx na 0.0.0.0:443)."""
    r = run(["ss", "-H", "-lntup"], check=False)
    main_pid = run(["systemctl", "show", "-p", "MainPID", "--value", "envoy.service"], check=False).stdout.strip()
    conflicts = []
    vips = set(all_vips(cfg))
    wanted = {(l["protocol"], l["port"]) for s in enabled_services(cfg) for l in cfg["services"][s]["listeners"]}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = "tcp" if parts[0] == "tcp" else "udp"
        host, _, port = parts[4].rpartition(":")
        host = host.strip("[]").split("%")[0]
        if not port.isdigit() or (proto, int(port)) not in wanted:
            continue
        if host in vips or host in ("0.0.0.0", "*", "::"):
            proc = " ".join(parts[6:]) if len(parts) > 6 else ""
            pids = set(re.findall(r"pid=(\d+)", proc))
            if '"envoy"' not in proc and not (main_pid not in ("", "0") and main_pid in pids):
                conflicts.append(f"{proto}/{port} zajęty przez {host} {proc}".strip())
    return conflicts


def preflight(cfg: dict) -> dict:
    """Weryfikacja zgodności konfiguracji ze stanem hosta (bez zmian)."""
    iface = net.resolve_interface(cfg)
    vips = all_vips(cfg)
    primary = net.primary_address(iface, exclude=vips)
    if primary and primary["address"] in vips:
        raise OmnissaError(f"VIP nie może być równy adresowi głównemu {primary['address']}", EXIT_CONFIG)
    for v in vips:
        net.compute_prefix(cfg, v, primary)
        owner = net.address_owner(v)
        if owner and owner != iface:
            raise OmnissaError(f"VIP {v} jest już przypisany do interfejsu {owner}", EXIT_CONFIG)
    conflicts = port_conflicts(cfg)
    if conflicts:
        raise OmnissaError("Porty wymagane przez Envoy są zajęte:\n  - " + "\n  - ".join(conflicts), EXIT_CONFIG,
                           hint="Zatrzymaj kolidującą usługę lub zmień porty listenerów")
    info = {
        "interface": iface,
        "primary_ip": f"{primary['address']}/{primary['prefix']}" if primary else None,
        "primary_dynamic": bool(primary and primary["dynamic"]),
        "gateway": net.gateway(iface),
        "vips": {s: vip_for(cfg, s) for s in enabled_services(cfg)},
    }
    if info["primary_dynamic"]:
        log.warning("Adres główny %s pochodzi z DHCP - zalecany jest adres statyczny dla serwera LB", info["primary_ip"])
    return info
