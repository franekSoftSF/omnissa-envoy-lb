"""Testy stanu wdrożenia (krytyczne i ostrzegawcze) oraz monitoring okresowy."""

from __future__ import annotations

import time

from . import dns_manager, envoy_config, firewall
from . import network_manager as net
from .common import ENVOY_CONFIG, State, log, now_iso, run, which
from .settings import enabled_services, vip_for


class Results:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok: bool | None, critical: bool, message: str) -> None:
        status = "pass" if ok else ("skip" if ok is None else ("fail" if critical else "warn"))
        self.items.append({"check": name, "status": status, "critical": critical, "message": message})
        lvl = {"pass": log.info, "skip": log.info, "warn": log.warning, "fail": log.error}[status]
        lvl("  [%s] %s: %s", status.upper(), name, message)

    @property
    def critical_failed(self) -> list[dict]:
        return [i for i in self.items if i["status"] == "fail"]

    def as_dict(self) -> dict:
        counts = {s: sum(1 for i in self.items if i["status"] == s) for s in ("pass", "warn", "fail", "skip")}
        return {"timestamp": now_iso(), "success": not self.critical_failed, "counts": counts, "checks": self.items}


def listening_sockets() -> set[tuple[str, str, int]]:
    r = run(["ss", "-H", "-lntu"], check=False)
    out = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        proto = parts[0]
        local = parts[4]
        host, _, port = local.rpartition(":")
        host = host.strip("[]").split("%")[0]
        if port.isdigit():
            out.add(("tcp" if proto == "tcp" else "udp", host, int(port)))
    return out


def tls_probe(address: str, port: int, sni: str, timeout: int = 8) -> tuple[bool, str]:
    if not which("openssl"):
        return False, "brak openssl"
    r = run(["timeout", str(timeout), "openssl", "s_client", "-connect", f"{address}:{port}",
             "-servername", sni, "-brief", "-verify_quiet"], check=False, timeout=timeout + 5, input_text="")
    text = r.stdout + r.stderr
    ok = "CONNECTION ESTABLISHED" in text or "Protocol version" in text
    detail = next((l.strip() for l in text.splitlines() if "Peer certificate" in l or "subject=" in l), "")
    return ok, detail or (text.strip().splitlines()[-1] if text.strip() else f"rc={r.rc}")


def run_checks(cfg: dict, wait_for_health: int = 30) -> Results:
    res = Results()
    val = cfg["validation"]
    iface = State().get("interface") or net.resolve_interface(cfg)

    active, enabled = envoy_config.service_state()
    res.add("systemd:envoy-active", active == "active", True, f"envoy.service is-active={active}")
    res.add("systemd:envoy-enabled", enabled == "enabled", True, f"envoy.service is-enabled={enabled}")
    ok, out = envoy_config.envoy_validate(ENVOY_CONFIG)
    res.add("envoy:config-valid", ok, True, "envoy --mode validate OK" if ok else out[-300:])
    ready = (envoy_config.admin_get(cfg, "/ready") or "").strip()
    res.add("envoy:ready", ready == "LIVE", True, f"/ready={ready or 'brak odpowiedzi'}")
    res.add("envoy:admin-loopback", cfg["envoy"]["admin_address"].startswith("127."), True,
            f"admin na {cfg['envoy']['admin_address']}:{cfg['envoy']['admin_port']}")

    present = net.interface_addresses(iface)
    for svc in enabled_services(cfg):
        vip = vip_for(cfg, svc)
        res.add(f"vip:{svc}", vip in present, True, f"VIP {vip} {'obecny' if vip in present else 'BRAK'} na {iface}")
    vips = {vip_for(cfg, s) for s in enabled_services(cfg)}
    res.add("vip:single-interface", all(net.address_owner(v) in (iface, None) for v in vips), True,
            f"wszystkie VIP na interfejsie {iface}")

    socks = listening_sockets()
    for svc in enabled_services(cfg):
        for lst in cfg["services"][svc]["listeners"]:
            vip = vip_for(cfg, svc)
            bound = (lst["protocol"], vip, lst["port"]) in socks
            res.add(f"listener:{svc}:{lst['protocol']}/{lst['port']}", bound, True,
                    f"{lst['protocol'].upper()} {vip}:{lst['port']} {'nasłuchuje' if bound else 'NIE nasłuchuje'}")

    fw_backend = firewall.detect_backend(cfg)
    if fw_backend:
        st = firewall.status(cfg)
        res.add(f"firewall:{fw_backend}", st.get("loaded"), True, f"reguły {fw_backend} {'załadowane' if st.get('loaded') else 'NIE załadowane'}")
    else:
        res.add("firewall", None, False, "zarządzanie firewall wyłączone")

    # Backendy (aktywne health checki Envoy) - z oczekiwaniem na pierwsze wyniki
    if val["backend_check"] != "skip":
        crit = val["backend_check"] == "critical"
        clusters = envoy_config.build_model(cfg)["clusters"]
        names = {c["name"] for c in clusters}
        deadline = time.time() + wait_for_health
        while True:
            health = envoy_config.cluster_health(cfg)
            pending = [c for c in names if not any(h["healthy"] for h in health.get(c, []))]
            if not pending or time.time() > deadline:
                break
            time.sleep(3)
        for c in clusters:
            hosts = health.get(c["name"], [])
            healthy = [h["host"] for h in hosts if h["healthy"]]
            res.add(f"backend:{c['name']}", bool(healthy), crit,
                    f"zdrowe {len(healthy)}/{len(hosts)}: {', '.join(healthy) or 'brak'}")
    else:
        res.add("backend", None, False, "pominięto (validation.backend_check=skip)")

    # SNI: Tunnel i SEG na swoich FQDN (także na wspólnym VIP)
    if val["sni_check"] != "skip":
        crit = val["sni_check"] == "critical"
        for svc in enabled_services(cfg):
            s = cfg["services"][svc]
            fqdn = s["fqdn"]
            if fqdn.startswith("*."):
                fqdn = "probe" + fqdn[1:]
            for lst in s["listeners"]:
                if lst["protocol"] != "tcp":
                    continue
                ok, detail = tls_probe(vip_for(cfg, svc), lst["port"], fqdn)
                res.add(f"sni:{svc}:{lst['port']}", ok, crit, f"TLS {fqdn} via {vip_for(cfg, svc)}:{lst['port']}: {detail}")
        if cfg["sni"]["unknown_sni_action"] == "reject":
            svc = enabled_services(cfg)[0]
            port = next((l["port"] for l in cfg["services"][svc]["listeners"] if l["protocol"] == "tcp"), None)
            if port:
                ok, _ = tls_probe(vip_for(cfg, svc), port, "unknown-sni.invalid", timeout=5)
                res.add("sni:unknown-rejected", not ok, False,
                        "nieznane SNI odrzucane" if not ok else "nieznane SNI NIE zostało odrzucone")

    if cfg["dns"].get("verify", True):
        for d in dns_manager.check(cfg):
            res.add(f"dns:{d['service']}", d["match"] if d["match"] is not None else None,
                    bool(cfg["dns"].get("require_match")), d["message"])
    return res


def monitor_once(cfg: dict) -> int:
    """Wywoływane przez omnissa-lb-healthcheck.timer - wynik trafia do journald."""
    res = run_checks(cfg, wait_for_health=0)
    d = res.as_dict()
    log.info("monitoring: pass=%s warn=%s fail=%s", d["counts"]["pass"], d["counts"]["warn"], d["counts"]["fail"])
    return 0 if d["success"] else 1
