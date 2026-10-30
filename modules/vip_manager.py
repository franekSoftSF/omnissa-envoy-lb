"""Zarządzanie adresami VIP na jednym, istniejącym interfejsie (idempotentnie)."""

from __future__ import annotations

from . import network_manager as net
from .common import EXIT_DEPLOY, Context, OmnissaError, State, log, run, which
from .settings import all_vips


def desired(cfg: dict, iface: str) -> list[tuple[str, int]]:
    managed = [v["address"] for v in State().get("managed_vips", [])]
    primary = net.primary_address(iface, exclude=all_vips(cfg) + managed)
    return [(v, net.compute_prefix(cfg, v, primary)) for v in all_vips(cfg)]


def duplicate_in_use(iface: str, vip: str) -> bool:
    """Wykrywa, czy adres VIP odpowiada już w sieci (konflikt IP)."""
    if which("arping"):
        r = run(["arping", "-D", "-q", "-c", "2", "-w", "3", "-I", iface, vip], check=False, timeout=10)
        return r.rc == 1  # iputils arping -D: rc=1 gdy ktoś odpowiedział
    r = run(["ping", "-c", "1", "-W", "1", vip], check=False, timeout=5)
    return r.ok


def announce(iface: str, vip: str) -> None:
    if which("arping"):
        run(["arping", "-U", "-q", "-c", "3", "-I", iface, vip], check=False, timeout=10)


def reconcile(ctx: Context, cfg: dict, iface: str) -> dict:
    """Doprowadza stan VIP do zgodności z konfiguracją. Nie duplikuje adresów,
    nie usuwa adresów, które nie zostały dodane przez instalator."""
    state = State()
    managed = state.get("managed_vips", [])
    want = desired(cfg, iface)
    want_addrs = {v for v, _ in want}
    current = net.interface_cidrs(iface)
    primary = net.primary_address(iface, exclude=list(want_addrs) + [m["address"] for m in managed])
    report = {"interface": iface, "primary": primary["address"] if primary else None, "added": [], "present": [], "removed": []}

    if primary and primary["address"] in want_addrs:
        raise OmnissaError(f"VIP {primary['address']} jest adresem głównym interfejsu {iface}", EXIT_DEPLOY)

    for vip, prefix in want:
        owner = net.address_owner(vip)
        if owner and owner != iface:
            raise OmnissaError(f"Adres {vip} jest już skonfigurowany na innym interfejsie ({owner})", EXIT_DEPLOY)
        if vip in current and current[vip] == prefix:
            log.info("VIP %s/%s już obecny na %s - pomijam", vip, prefix, iface)
            report["present"].append(vip)
            continue
        if vip in current:
            if not any(m["address"] == vip for m in managed):
                raise OmnissaError(
                    f"Adres {vip}/{current[vip]} na {iface} nie został dodany przez instalator, "
                    f"a konfiguracja wymaga maski /{prefix}",
                    EXIT_DEPLOY,
                    hint="Usuń adres ręcznie lub dostosuj vip.prefix",
                )
            old = f"{vip}/{current[vip]}"
            ctx.plan("vip", "change-prefix", address=old, new=f"{vip}/{prefix}", interface=iface)
            if not ctx.dry_run:
                run(["ip", "addr", "del", old, "dev", iface])
                ctx.transaction.record("vip_removed", iface, old)
            report["removed"].append(old)
        if cfg["vip"].get("duplicate_check", True) and duplicate_in_use(iface, vip):
            raise OmnissaError(
                f"Adres VIP {vip} odpowiada w sieci (używany przez inne urządzenie) - przerwano",
                EXIT_DEPLOY,
                hint="Wybierz wolny adres lub wyłącz vip.duplicate_check, jeśli to zamierzone",
            )
        ctx.plan("vip", "add", address=f"{vip}/{prefix}", interface=iface)
        if not ctx.dry_run:
            run(["ip", "addr", "add", f"{vip}/{prefix}", "dev", iface])
            ctx.transaction.record("vip_added", iface, f"{vip}/{prefix}")
            if cfg["vip"].get("gratuitous_arp", True):
                announce(iface, vip)
        report["added"].append(f"{vip}/{prefix}")

    keep = []
    for m in managed:
        if m["address"] in want_addrs and m["interface"] == iface:
            continue
        if m["address"] in net.interface_addresses(m["interface"]):
            ctx.plan("vip", "remove", address=m["cidr"], interface=m["interface"])
            if not ctx.dry_run:
                run(["ip", "addr", "del", m["cidr"], "dev", m["interface"]], check=False)
                ctx.transaction.record("vip_removed", m["interface"], m["cidr"])
            report["removed"].append(m["cidr"])
    preexisting = set(state.get("preexisting_vips", [])) & want_addrs
    added = {c.split("/")[0] for c in report["added"]}
    for vip, prefix in want:
        if vip in added:
            preexisting.discard(vip)  # dodany przez instalator - od teraz zarządzany
            keep.append({"address": vip, "cidr": f"{vip}/{prefix}", "interface": iface})
        elif any(m["address"] == vip for m in managed):
            keep.append({"address": vip, "cidr": f"{vip}/{prefix}", "interface": iface})
        else:
            preexisting.add(vip)  # adres istniał przed instalatorem - nie przejmujemy go
    state.set("managed_vips", keep)
    state.set("preexisting_vips", sorted(preexisting))
    state.set("interface", iface)
    state.save(ctx)

    net.persist(ctx, cfg, iface, want, report["primary"])
    return report


def status(cfg: dict, iface: str) -> list[dict]:
    present = net.interface_addresses(iface)
    return [{"vip": v, "prefix": p, "present": v in present} for v, p in desired(cfg, iface)]


def remove_all(ctx: Context) -> list[str]:
    """Usuwa wyłącznie VIP-y dodane przez instalator (uninstall)."""
    removed = []
    for m in State().get("managed_vips", []):
        if m["address"] in net.interface_addresses(m["interface"]):
            ctx.plan("vip", "remove", address=m["cidr"], interface=m["interface"])
            if not ctx.dry_run:
                run(["ip", "addr", "del", m["cidr"], "dev", m["interface"]], check=False)
            removed.append(m["cidr"])
    return removed
