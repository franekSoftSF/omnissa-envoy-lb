"""Wykrywanie interfejsu, adresu głównego, bramy oraz bezpieczna obsługa Netplan.

Zasady bezpieczeństwa:
  * Nie modyfikujemy istniejących plików Netplan - VIP trafia do osobnego pliku
    60-omnissa-lb-vip.yaml, który Netplan scala z konfiguracją interfejsu.
  * Przed zapisem wynik scalenia jest weryfikowany w izolowanym katalogu
    (netplan generate/get --root-dir) - adres główny, DHCP i trasy muszą pozostać bez zmian.
  * Tryb domyślny 'runtime' nie wywołuje 'netplan apply' (brak restartu sieci).
  * Tryb 'netplan-apply' uzbraja niezależny od instalatora timer systemd
    (dead-man switch), który przywróci poprzedni plik, jeśli łączność nie zostanie potwierdzona.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
import tempfile
import time
from pathlib import Path

from .common import (
    EXIT_CONFIG,
    EXIT_DEPLOY,
    NETPLAN_DIR,
    NETPLAN_FILE,
    STATE_DIR,
    Context,
    OmnissaError,
    log,
    run,
    which,
    write_managed_file,
)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROLLBACK_UNIT = "omnissa-lb-netplan-rollback"
SKIP_PREFIXES = ("lo", "docker", "veth", "virbr", "br-", "cni", "flannel", "cali", "tun", "tap", "wg")


def ip_json(*args: str) -> list:
    res = run(["ip", "-j", *args], check=False)
    if not res.ok or not res.stdout.strip():
        return []
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return []


def list_interfaces() -> list[dict]:
    out = []
    for link in ip_json("addr", "show"):
        name = link.get("ifname", "")
        if not name or name.startswith(SKIP_PREFIXES):
            continue
        addrs = [
            {
                "address": a["local"],
                "prefix": a["prefixlen"],
                "secondary": bool(a.get("secondary")),
                "dynamic": bool(a.get("dynamic")),
            }
            for a in link.get("addr_info", [])
            if a.get("family") == "inet" and a.get("scope") == "global"
        ]
        out.append(
            {
                "name": name,
                "state": link.get("operstate", "UNKNOWN"),
                "mac": link.get("address", ""),
                "mtu": link.get("mtu"),
                "addresses": addrs,
            }
        )
    return out


def default_route_interface() -> str | None:
    routes = ip_json("-4", "route", "show", "default")
    return routes[0].get("dev") if routes else None


def gateway(iface: str) -> str | None:
    for r in ip_json("-4", "route", "show", "default"):
        if r.get("dev") == iface and r.get("gateway"):
            return r["gateway"]
    return None


def resolve_interface(cfg: dict) -> str:
    name = cfg["network"].get("interface") or "auto"
    ifaces = {i["name"]: i for i in list_interfaces()}
    if name == "auto":
        name = default_route_interface()
        if not name:
            raise OmnissaError(
                "Nie można automatycznie wykryć interfejsu (brak trasy domyślnej)",
                EXIT_CONFIG,
                hint="Podaj --interface <nazwa> lub network.interface w settings.yaml",
            )
        log.info("Wykryto interfejs trasy domyślnej: %s", name)
    if name not in ifaces:
        raise OmnissaError(
            f"Interfejs {name} nie istnieje. Dostępne: {', '.join(sorted(ifaces)) or 'brak'}",
            EXIT_CONFIG,
        )
    if ifaces[name]["state"] not in ("UP", "UNKNOWN"):
        raise OmnissaError(f"Interfejs {name} nie jest aktywny (stan {ifaces[name]['state']})", EXIT_CONFIG)
    return name


def primary_address(iface: str, exclude: list[str] | None = None) -> dict | None:
    """Pierwszy niesekundarny adres IPv4 interfejsu, który nie jest VIP-em."""
    exclude = set(exclude or [])
    for i in list_interfaces():
        if i["name"] != iface:
            continue
        cands = [a for a in i["addresses"] if a["address"] not in exclude]
        cands.sort(key=lambda a: a["secondary"])
        return cands[0] if cands else None
    return None


def interface_cidrs(iface: str) -> dict[str, int]:
    for i in list_interfaces():
        if i["name"] == iface:
            return {a["address"]: a["prefix"] for a in i["addresses"]}
    return {}


def interface_addresses(iface: str) -> list[str]:
    for i in list_interfaces():
        if i["name"] == iface:
            return [a["address"] for a in i["addresses"]]
    return []


def address_owner(address: str) -> str | None:
    for i in list_interfaces():
        if address in (a["address"] for a in i["addresses"]):
            return i["name"]
    return None


def compute_prefix(cfg: dict, vip: str, primary: dict | None) -> int:
    if cfg["vip"].get("prefix"):
        return int(cfg["vip"]["prefix"])
    if not primary:
        raise OmnissaError(
            "Interfejs nie ma adresu IPv4 - nie można wyznaczyć maski VIP",
            EXIT_CONFIG,
            hint="Ustaw vip.prefix w settings.yaml",
        )
    net = ipaddress.ip_network(f"{primary['address']}/{primary['prefix']}", strict=False)
    if ipaddress.ip_address(vip) not in net:
        raise OmnissaError(
            f"VIP {vip} nie należy do podsieci interfejsu {net}",
            EXIT_CONFIG,
            hint="Użyj adresu z podsieci interfejsu lub jawnie ustaw vip.prefix (np. 32)",
        )
    if ipaddress.ip_address(vip) in (net.network_address, net.broadcast_address):
        raise OmnissaError(f"VIP {vip} jest adresem sieci/rozgłoszeniowym {net}", EXIT_CONFIG)
    return primary["prefix"]


# --------------------------------------------------------------------------- Netplan


def _netplan_get(root: str | None = None) -> dict:
    cmd = ["netplan", "get", "all"] if root is None else ["netplan", "get", "--root-dir", root, "all"]
    res = run(cmd, check=False)
    if not res.ok or yaml is None:
        return {}
    try:
        data = yaml.safe_load(res.stdout) or {}
    except yaml.YAMLError:
        return {}
    return data.get("network", data) if isinstance(data, dict) else {}


def netplan_device_id(iface: str) -> tuple[str, str]:
    """Zwraca (sekcja, id) definicji interfejsu w Netplan (obsługa set-name)."""
    conf = _netplan_get()
    for section in ("ethernets", "bonds", "vlans", "bridges", "wifis"):
        for dev_id, dev in (conf.get(section) or {}).items():
            if dev_id == iface or (isinstance(dev, dict) and dev.get("set-name") == iface):
                return section, dev_id
    return "ethernets", iface


def render_netplan(cfg: dict, iface: str, vips: list[tuple[str, int]]) -> str:
    section, dev_id = netplan_device_id(iface)
    lines = [
        "# Plik zarządzany przez omnissa-envoy-lb - NIE EDYTOWAĆ RĘCZNIE.",
        "# Dodaje adresy VIP do istniejącego interfejsu; konfiguracja główna pozostaje",
        "# w dotychczasowych plikach Netplan (scalanie konfiguracji).",
        "network:",
        "  version: 2",
        f"  {section}:",
        f"    {dev_id}:",
        "      addresses:",
    ]
    lines += [f"        - {v}/{p}" for v, p in vips]
    return "\n".join(lines) + "\n"


def validate_netplan(content: str, iface: str, vips: list[tuple[str, int]]) -> None:
    """Symuluje wynik scalenia w izolowanym katalogu i weryfikuje, że konfiguracja
    główna interfejsu nie uległa zmianie."""
    if not which("netplan"):
        raise OmnissaError("Brak polecenia netplan (pakiet netplan.io)", EXIT_DEPLOY)
    section, dev_id = netplan_device_id(iface)
    before = (_netplan_get().get(section) or {}).get(dev_id) or {}
    with tempfile.TemporaryDirectory(prefix="omnissa-netplan-") as tmp:
        dst = Path(tmp) / "etc" / "netplan"
        dst.mkdir(parents=True)
        for f in sorted(NETPLAN_DIR.glob("*.yaml")):
            if f.name != NETPLAN_FILE.name:
                shutil.copy2(f, dst / f.name)
        (dst / NETPLAN_FILE.name).write_text(content)
        (dst / NETPLAN_FILE.name).chmod(0o600)
        res = run(["netplan", "generate", "--root-dir", tmp], check=False)
        if not res.ok:
            raise OmnissaError(f"Walidacja Netplan nie powiodła się: {res.stderr.strip()[:800]}", EXIT_DEPLOY)
        after = (_netplan_get(tmp).get(section) or {}).get(dev_id) or {}
    if yaml is None:
        return
    after_addrs = [str(a).split()[0] if not isinstance(a, dict) else list(a)[0] for a in after.get("addresses") or []]
    before_addrs = [str(a).split()[0] if not isinstance(a, dict) else list(a)[0] for a in before.get("addresses") or []]
    old_vip_cidrs = set()
    if NETPLAN_FILE.exists():
        try:
            cur = yaml.safe_load(NETPLAN_FILE.read_text()) or {}
            old_vip_cidrs = set(map(str, ((cur.get("network", {}).get(section) or {}).get(dev_id) or {}).get("addresses") or []))
        except yaml.YAMLError:
            pass
    missing = [a for a in before_addrs if a not in after_addrs and a not in old_vip_cidrs]
    if missing:
        raise OmnissaError(f"Scalona konfiguracja Netplan utraciłaby adresy: {missing} - przerwano", EXIT_DEPLOY)
    for v, p in vips:
        if f"{v}/{p}" not in after_addrs:
            raise OmnissaError(f"Scalona konfiguracja Netplan nie zawiera VIP {v}/{p}", EXIT_DEPLOY)
    for key in ("dhcp4", "dhcp6", "routes", "nameservers", "gateway4", "set-name", "match", "mtu"):
        if before.get(key) != after.get(key):
            raise OmnissaError(f"Scalona konfiguracja Netplan zmieniłaby '{key}' interfejsu {iface} - przerwano", EXIT_DEPLOY)


def connectivity_ok(cfg: dict, iface: str, primary_ip: str | None) -> tuple[bool, list[str]]:
    msgs, ok = [], True
    if primary_ip and primary_ip not in interface_addresses(iface):
        ok = False
        msgs.append(f"adres główny {primary_ip} zniknął z {iface}")
    targets = []
    gw = gateway(iface)
    if gw:
        targets.append(gw)
    targets += [str(t) for t in cfg["network"].get("connectivity_targets") or []]
    for t in targets:
        r = run(["ping", "-c", "2", "-W", "2", "-I", iface, t], check=False, timeout=15)
        if not r.ok:
            ok = False
            msgs.append(f"brak odpowiedzi od {t}")
        else:
            msgs.append(f"{t} osiągalny")
    return ok, msgs


def persist(ctx: Context, cfg: dict, iface: str, vips: list[tuple[str, int]], primary_ip: str | None) -> bool:
    """Zapisuje plik Netplan z VIP; w trybie netplan-apply stosuje go z dead-man switch."""
    if vips:
        content = render_netplan(cfg, iface, vips)
        current = NETPLAN_FILE.read_text() if NETPLAN_FILE.exists() else None
        if current == content:
            log.info("Netplan: plik %s aktualny", NETPLAN_FILE)
            return False
        if ctx.dry_run:
            ctx.plan("netplan", "write", path=str(NETPLAN_FILE), interface=iface, addresses=[f"{v}/{p}" for v, p in vips])
            for ln in content.splitlines():
                log.info("    | %s", ln)
            return True
        validate_netplan(content, iface, vips)
        write_managed_file(ctx, NETPLAN_FILE, content, mode=0o600, category="netplan")
    else:
        if not NETPLAN_FILE.exists():
            return False
        ctx.plan("netplan", "remove", path=str(NETPLAN_FILE))
        if ctx.dry_run:
            return True
        ctx.transaction.backup_file(NETPLAN_FILE)
        NETPLAN_FILE.unlink()

    if cfg["network"]["apply_mode"] == "netplan-apply":
        safe_netplan_apply(ctx, cfg, iface, primary_ip)
    else:
        log.info("Netplan: konfiguracja trwała zapisana (bez 'netplan apply' - VIP dodany w runtime)")
    return True


def safe_netplan_apply(ctx: Context, cfg: dict, iface: str, primary_ip: str | None) -> None:
    timeout = int(cfg["network"].get("confirm_timeout") or 120)
    tx = ctx.transaction
    backup_copy = None
    for row in tx.entries("file"):
        if row[1] == str(NETPLAN_FILE) and row[2] == "1":
            backup_copy = tx.dir / row[3]
    script = STATE_DIR / f"netplan-rollback-{tx.id}.sh"
    restore = f"cp -a '{backup_copy}' '{NETPLAN_FILE}'" if backup_copy else f"rm -f '{NETPLAN_FILE}'"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "#!/bin/bash\n# Dead-man switch: przywrócenie Netplan przy braku potwierdzenia łączności\n"
        f"{restore}\nnetplan apply\nlogger -t omnissa-lb 'Netplan rollback wykonany ({tx.id})'\n"
    )
    script.chmod(0o700)
    run(["systemctl", "stop", f"{ROLLBACK_UNIT}.timer"], check=False)
    run(["systemctl", "reset-failed", f"{ROLLBACK_UNIT}.service", f"{ROLLBACK_UNIT}.timer"], check=False)
    run(["systemd-run", f"--unit={ROLLBACK_UNIT}", f"--on-active={timeout}s", "--timer-property=AccuracySec=1s",
         "--description=Omnissa LB netplan rollback", "/bin/bash", str(script)])
    log.warning("Uzbrojono automatyczny rollback Netplan za %ss, stosuję 'netplan apply'", timeout)
    res = run(["netplan", "apply"], check=False, timeout=timeout // 2)
    time.sleep(5)
    ok, msgs = connectivity_ok(cfg, iface, primary_ip)
    log.info("Test łączności po netplan apply: %s", "; ".join(msgs) or "brak celów")
    if res.ok and ok:
        run(["systemctl", "stop", f"{ROLLBACK_UNIT}.timer"], check=False)
        script.unlink(missing_ok=True)
        log.info("Łączność potwierdzona - rollback Netplan rozbrojony")
        return
    run(["systemctl", "stop", f"{ROLLBACK_UNIT}.timer"], check=False)
    run(["/bin/bash", str(script)], check=False, timeout=120)
    raise OmnissaError("Łączność po 'netplan apply' nie została potwierdzona - przywrócono poprzednią konfigurację Netplan", EXIT_DEPLOY)
