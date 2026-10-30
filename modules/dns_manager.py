"""Weryfikacja DNS dla FQDN Tunnel i SEG (rekordy A wskazujące na VIP)."""

from __future__ import annotations

import socket

from .common import run, which
from .settings import enabled_services, vip_for


def resolve(fqdn: str, servers: list[str] | None = None) -> list[str]:
    if servers and which("dig"):
        addrs: list[str] = []
        for srv in servers:
            r = run(["dig", "+short", "+time=2", "+tries=1", f"@{srv}", fqdn, "A"], check=False, timeout=10)
            addrs += [l.strip() for l in r.stdout.splitlines() if l.strip() and l.strip()[0].isdigit()]
        return sorted(set(addrs))
    try:
        infos = socket.getaddrinfo(fqdn, None, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return []
    return sorted({i[4][0] for i in infos})


def check(cfg: dict) -> list[dict]:
    """Zwraca wynik dla każdej usługi: czy FQDN rozwiązuje się na właściwy VIP."""
    out = []
    servers = cfg["dns"].get("servers") or []
    for svc in enabled_services(cfg):
        fqdn = cfg["services"][svc]["fqdn"]
        vip = vip_for(cfg, svc)
        if fqdn.startswith("*."):
            out.append({"service": svc, "fqdn": fqdn, "vip": vip, "resolved": [], "match": None,
                        "message": "nazwa wieloznaczna - pominięto test DNS"})
            continue
        addrs = resolve(fqdn, servers)
        match = vip in addrs
        if not addrs:
            msg = f"{fqdn} nie rozwiązuje się (brak rekordu A)"
        elif match:
            msg = f"{fqdn} -> {', '.join(addrs)} (zawiera VIP {vip})"
        else:
            msg = f"{fqdn} -> {', '.join(addrs)} - NIE wskazuje na VIP {vip} (możliwy NAT/GSLB)"
        out.append({"service": svc, "fqdn": fqdn, "vip": vip, "resolved": addrs, "match": match, "message": msg})
    return out
