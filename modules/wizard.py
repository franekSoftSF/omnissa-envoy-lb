"""Interaktywny kreator settings.yaml."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

from . import network_manager as net
from .common import EXIT_ABORT, OmnissaError
from .settings import DEFAULTS, deep_merge, dump_yaml, read_file, validate


def _ask(prompt: str, default=None, validator=None, allow_empty=False) -> str:
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            val = input(f"  {prompt}{suffix}: ").strip()
        except EOFError as exc:
            raise OmnissaError("Przerwano kreator (brak wejścia)", EXIT_ABORT) from exc
        if not val and default is not None:
            val = str(default)
        if not val and not allow_empty:
            print("    ! wartość wymagana")
            continue
        if validator:
            err = validator(val)
            if err:
                print(f"    ! {err}")
                continue
        return val


def _yes(prompt: str, default: bool = True) -> bool:
    d = "T/n" if default else "t/N"
    while True:
        try:
            v = input(f"  {prompt} [{d}]: ").strip().lower()
        except EOFError as exc:
            raise OmnissaError("Przerwano kreator (brak wejścia)", EXIT_ABORT) from exc
        if not v:
            return default
        if v in ("t", "tak", "y", "yes"):
            return True
        if v in ("n", "nie", "no"):
            return False


def _ip(v: str) -> str | None:
    try:
        ipaddress.IPv4Address(v)
    except ValueError:
        return "podaj adres IPv4"
    return None


def _ips(v: str) -> str | None:
    for part in v.replace(",", " ").split():
        try:
            ipaddress.ip_address(part)
        except ValueError:
            if "." not in part:
                return f"nieprawidłowy adres/FQDN: {part}"
    return None


def _ports(spec: str) -> list[dict]:
    """'tcp/443,udp/443' -> lista listenerów."""
    out = []
    for item in spec.replace(" ", "").split(","):
        proto, _, port = item.partition("/")
        out.append({"protocol": proto.lower(), "port": int(port)})
    return out


def _ports_valid(spec: str) -> str | None:
    try:
        for l in _ports(spec):
            if l["protocol"] not in ("tcp", "udp") or not 1 <= l["port"] <= 65535:
                return "format: tcp/443,udp/443"
    except ValueError:
        return "format: tcp/443,udp/443"
    return None


def run_wizard(output: Path, base: Path | None = None, interface: str | None = None) -> Path:
    if not sys.stdin.isatty():
        raise OmnissaError("Tryb interaktywny wymaga terminala. Użyj --unattended --config <plik>", EXIT_ABORT)
    cur = deep_merge(DEFAULTS, read_file(base)) if base and base.exists() else deep_merge(DEFAULTS, {})
    print("\n=== Omnissa Envoy Load Balancer - kreator konfiguracji ===\n")
    print("Dostępne interfejsy:")
    ifaces = net.list_interfaces()
    for i in ifaces:
        addrs = ", ".join(f"{a['address']}/{a['prefix']}" for a in i["addresses"]) or "brak IPv4"
        print(f"   - {i['name']:<12} {i['state']:<6} {addrs}")
    default_if = interface or (cur["network"]["interface"] if cur["network"]["interface"] != "auto" else None) \
        or net.default_route_interface()
    names = {i["name"] for i in ifaces}
    iface = _ask("Interfejs dla VIP (jeden, istniejący)", default_if,
                 lambda v: None if v in names else "interfejs nie istnieje")
    cur["network"]["interface"] = iface
    primary = net.primary_address(iface)
    if primary:
        print(f"    adres główny: {primary['address']}/{primary['prefix']}, brama: {net.gateway(iface) or 'brak'}")

    print("\nTryb VIP: shared = Tunnel i SEG na jednym VIP (routing SNI), dedicated = osobne VIP")
    mode = _ask("Tryb VIP (shared/dedicated)", cur["vip"]["mode"],
                lambda v: None if v in ("shared", "dedicated") else "shared lub dedicated")
    cur["vip"]["mode"] = mode

    for svc, label, defports in (("tunnel", "Tunnel", "tcp/443,udp/443"), ("seg", "SEG", "tcp/443")):
        s = cur["services"][svc]
        print(f"\n--- {label} ---")
        s["enabled"] = _yes(f"Włączyć {label}?", bool(s.get("enabled", True)))
        if not s["enabled"]:
            continue
        s["fqdn"] = _ask(f"FQDN {label}", s.get("fqdn"))
        old_l = {(l["protocol"], l["port"]): l for l in s.get("listeners") or []}
        cur_ports = ",".join(f"{p}/{n}" for p, n in old_l) or defports
        s["listeners"] = []
        for l in _ports(_ask(f"Listenery {label} (proto/port, przecinki)", cur_ports, _ports_valid)):
            prev = old_l.get((l["protocol"], l["port"]))
            if prev and prev.get("backend_port"):
                l["backend_port"] = prev["backend_port"]  # zachowanie mapowania portu backendu
            s["listeners"].append(l)
        old_b = {(b if isinstance(b, str) else b["address"]): ({} if isinstance(b, str) else b)
                 for b in s.get("backends") or []}
        cur_b = " ".join(old_b)
        s["backends"] = []
        for a in _ask(f"Backendy {label} (IP/FQDN węzłów UAG)", cur_b or None, _ips).replace(",", " ").split():
            entry = {"address": a}
            if old_b.get(a, {}).get("weight"):
                entry["weight"] = old_b[a]["weight"]  # zachowanie wag
            s["backends"].append(entry)
        if mode == "dedicated":
            cur["vip"][svc] = _ask(f"VIP {label}", cur["vip"].get(svc), _ip)
    if mode == "shared":
        cur["vip"]["shared"] = _ask("Wspólny VIP", cur["vip"].get("shared"), _ip)

    hc = cur["health_check_defaults"]
    print("\n--- Health check ---")
    hc["type"] = _ask("Typ (tcp/https/http/none)", hc["type"],
                      lambda v: None if v in ("tcp", "http", "https", "none") else "tcp|https|http|none")
    if hc["type"] in ("http", "https"):
        hc["path"] = _ask("Ścieżka", hc["path"])

    print("\n--- Firewall i monitoring ---")
    cur["firewall"]["manage"] = _yes("Zarządzać regułami firewall dla VIP?", cur["firewall"]["manage"])
    if cur["firewall"]["manage"]:
        cur["firewall"]["backend"] = _ask("Backend (auto/nftables/ufw)", cur["firewall"]["backend"],
                                          lambda v: None if v in ("auto", "nftables", "ufw") else "auto|nftables|ufw")
        srcs = _ask("Dozwolone sieci źródłowe", " ".join(cur["firewall"]["allowed_sources"]))
        cur["firewall"]["allowed_sources"] = srcs.replace(",", " ").split()
    cur["monitoring"]["healthcheck_timer"] = _yes("Włączyć okresowy health check (timer systemd)?",
                                                  cur["monitoring"]["healthcheck_timer"])
    crit = _yes("Traktować niedostępne backendy jako błąd krytyczny instalacji?",
                cur["validation"]["backend_check"] == "critical")
    cur["validation"]["backend_check"] = "critical" if crit else "warning"
    cur["validation"]["sni_check"] = cur["validation"]["backend_check"]

    to_save = _clean(cur)
    validate(deep_merge(DEFAULTS, to_save))  # walidacja kopii (validate normalizuje dane)
    print("\n=== Podsumowanie ===\n" + dump_yaml(to_save))
    if not _yes("Zapisać konfigurację i kontynuować wdrożenie?", True):
        raise OmnissaError("Anulowano przez administratora", EXIT_ABORT)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("# Wygenerowano kreatorem omnissa-lb\n" + dump_yaml(to_save))
    output.chmod(0o640)
    return output


def _clean(cfg: dict) -> dict:
    """Usuwa pola wewnętrzne dodawane podczas walidacji."""
    import copy

    out = copy.deepcopy(cfg)
    for s in out["services"].values():
        for b in s.get("backends") or []:
            if isinstance(b, dict):
                b.pop("is_ip", None)
    return out
