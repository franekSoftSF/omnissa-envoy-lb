"""Firewall: nftables (własna tabela inet omnissa_lb) lub ufw.

Bezpieczeństwo:
  * Reguły dotyczą wyłącznie ruchu kierowanego na adresy VIP - SSH i pozostałe
    usługi hosta nie są modyfikowane (brak ryzyka odcięcia administratora).
  * nftables: tabela jest atomowo zastępowana (brak duplikatów); NIE włączamy
    nftables.service (jego 'flush ruleset' usunąłby reguły ufw/docker) - trwałość
    zapewnia dedykowana usługa omnissa-lb-firewall.service.
  * ufw: reguły z komentarzem 'omnissa-lb'; ufw sam pomija duplikaty, a stan
    reguł jest śledzony w state.json, więc zbędne reguły są usuwane.
"""

from __future__ import annotations

import ipaddress

from .common import ETC_DIR, EXIT_DEPLOY, Context, OmnissaError, State, log, run, which, write_managed_file
from .settings import all_vips, enabled_services, vip_for

NFT_FILE = ETC_DIR / "nftables.nft"
NFT_TABLE = "omnissa_lb"
FW_UNIT = "omnissa-lb-firewall.service"
UFW_COMMENT = "omnissa-lb"


def detect_backend(cfg: dict) -> str | None:
    fw = cfg["firewall"]
    if not fw.get("manage"):
        return None
    if fw["backend"] != "auto":
        return fw["backend"]
    if which("ufw"):
        r = run(["ufw", "status"], check=False)
        if r.ok and "Status: active" in r.stdout:
            return "ufw"
    return "nftables"


def rules(cfg: dict) -> list[dict]:
    out: list[dict] = []
    for svc in enabled_services(cfg):
        for lst in cfg["services"][svc]["listeners"]:
            key = (vip_for(cfg, svc), lst["protocol"], lst["port"])
            same = next((r for r in out if (r["vip"], r["protocol"], r["port"]) == key), None)
            if same:  # wspólny VIP: jedna reguła dla Tunnel i SEG
                same["service"] += f",{svc}"
            else:
                out.append({"service": svc, "vip": key[0], "protocol": key[1], "port": key[2]})
    prom = cfg["monitoring"]["prometheus"]
    if prom.get("enabled") and not ipaddress.ip_address(prom["address"]).is_loopback:
        out.append({"service": "prometheus", "vip": prom["address"], "protocol": "tcp", "port": prom["port"]})
    return out


def render_nft(cfg: dict) -> str:
    srcs = [str(ipaddress.ip_network(s, strict=False)) for s in cfg["firewall"]["allowed_sources"]]
    any_src = "0.0.0.0/0" in srcs
    src_set = ", ".join(srcs)
    lines = [
        "#!/usr/sbin/nft -f",
        "# Zarządzane przez omnissa-envoy-lb - NIE EDYTOWAĆ RĘCZNIE.",
        "# Tabela jest zastępowana atomowo; inne tabele (ufw, docker) pozostają nienaruszone.",
        f"table inet {NFT_TABLE} {{}}",
        f"delete table inet {NFT_TABLE}",
        f"table inet {NFT_TABLE} {{",
        "  set vips {",
        "    type ipv4_addr",
        f"    elements = {{ {', '.join(all_vips(cfg))} }}",
        "  }",
        "  chain input {",
        "    type filter hook input priority filter - 5; policy accept;",
        "    ip daddr != @vips return",
        "    ct state established,related accept",
        "    ct state invalid drop",
    ]
    for r in rules(cfg):
        if r["service"] == "prometheus":
            continue
        src = "" if any_src else f"ip saddr {{ {src_set} }} "
        lines.append(f"    ip daddr {r['vip']} {src}{r['protocol']} dport {r['port']} accept comment \"{r['service']}\"")
    lines.append("    icmp type { echo-request, destination-unreachable, time-exceeded } limit rate 50/second accept")
    if cfg["firewall"].get("restrict_vip", True):
        lines.append("    counter drop comment \"omnissa-lb: inny ruch do VIP\"")
    lines += ["  }", "}"]
    return "\n".join(lines) + "\n"


def _ufw_specs(cfg: dict) -> list[str]:
    specs = []
    for r in rules(cfg):
        for src in cfg["firewall"]["allowed_sources"]:
            s = "any" if str(src) in ("0.0.0.0/0", "any") else str(src)
            specs.append(f"allow proto {r['protocol']} from {s} to {r['vip']} port {r['port']}")
    return specs


def apply(ctx: Context, cfg: dict) -> dict:
    backend = detect_backend(cfg)
    state = State()
    report = {"backend": backend, "rules": rules(cfg)}
    if backend is None:
        log.info("Firewall: zarządzanie wyłączone (firewall.manage=false)")
        return report
    prev_backend = state.get("firewall_backend")
    if prev_backend and prev_backend != backend:
        remove(ctx, prev_backend)

    if backend == "nftables":
        if not which("nft"):
            raise OmnissaError("Brak polecenia nft (pakiet nftables)", EXIT_DEPLOY)
        content = render_nft(cfg)
        changed = write_managed_file(ctx, NFT_FILE, content, mode=0o640, category="firewall")
        loaded = run(["nft", "list", "table", "inet", NFT_TABLE], check=False).ok if not ctx.dry_run else False
        if ctx.dry_run:
            for ln in content.splitlines():
                log.info("    | %s", ln)
        if changed or not loaded:
            ctx.plan("firewall", "nft-load", file=str(NFT_FILE))
            if not ctx.dry_run:
                chk = run(["nft", "-c", "-f", str(NFT_FILE)], check=False)
                if not chk.ok:
                    raise OmnissaError(f"Walidacja reguł nftables nie powiodła się: {chk.stderr.strip()}", EXIT_DEPLOY)
                ctx.transaction.record("nft_applied", NFT_TABLE)
                run(["nft", "-f", str(NFT_FILE)])
        _enable_fw_unit(ctx)
        report["changed"] = changed or not loaded
    else:
        if not which("ufw"):
            raise OmnissaError("Brak polecenia ufw", EXIT_DEPLOY)
        st = run(["ufw", "status"], check=False).stdout if not ctx.dry_run else ""
        if not ctx.dry_run and "Status: active" not in st:
            log.warning("ufw jest nieaktywny - reguły zostaną dodane, ale nie będą egzekwowane, "
                        "dopóki administrator nie włączy ufw (instalator nie włącza ufw, aby nie odciąć SSH)")
        want = _ufw_specs(cfg)
        have = state.get("ufw_rules", [])
        for spec in want:
            if spec in have:
                continue
            ctx.plan("firewall", "ufw", rule=spec)
            if not ctx.dry_run:
                run(["ufw", *spec.split(), "comment", UFW_COMMENT])
                ctx.transaction.record("ufw_rule_added", spec)
        for spec in have:
            if spec not in want:
                ctx.plan("firewall", "ufw-delete", rule=spec)
                if not ctx.dry_run:
                    run(["ufw", "delete", *spec.split()], check=False)
        state.set("ufw_rules", want)
        report["changed"] = want != have
    state.set("firewall_backend", backend)
    state.save(ctx)
    return report


def _enable_fw_unit(ctx: Context) -> None:
    r = run(["systemctl", "is-enabled", FW_UNIT], check=False)
    if r.stdout.strip() != "enabled":
        ctx.plan("systemd", "enable", unit=FW_UNIT)
        if not ctx.dry_run:
            run(["systemctl", "daemon-reload"], check=False)
            run(["systemctl", "enable", FW_UNIT])
            ctx.transaction.record("unit_enabled", FW_UNIT)


def remove(ctx: Context, backend: str | None = None) -> None:
    state = State()
    backend = backend or state.get("firewall_backend")
    if backend == "nftables":
        ctx.plan("firewall", "nft-delete-table", table=NFT_TABLE)
        if not ctx.dry_run:
            run(["nft", "delete", "table", "inet", NFT_TABLE], check=False)
            run(["systemctl", "disable", FW_UNIT], check=False)
    elif backend == "ufw":
        for spec in state.get("ufw_rules", []):
            ctx.plan("firewall", "ufw-delete", rule=spec)
            if not ctx.dry_run:
                run(["ufw", "delete", *spec.split()], check=False)
        state.set("ufw_rules", [])
    state.set("firewall_backend", None)
    state.save(ctx)


def status(cfg: dict) -> dict:
    backend = State().get("firewall_backend") or detect_backend(cfg)
    if backend == "nftables":
        r = run(["nft", "list", "table", "inet", NFT_TABLE], check=False)
        return {"backend": backend, "loaded": r.ok, "ruleset": r.stdout if r.ok else ""}
    if backend == "ufw":
        r = run(["ufw", "status", "numbered"], check=False)
        return {"backend": backend, "loaded": UFW_COMMENT in r.stdout, "ruleset": r.stdout}
    return {"backend": None, "loaded": False}
