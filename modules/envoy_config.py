"""Generowanie, walidacja i wdrażanie konfiguracji Envoy oraz usługi systemd."""

from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
import time
import urllib.request
from pathlib import Path

from .common import (
    APP_VERSION,
    ENVOY_BIN,
    ENVOY_CONFIG,
    ETC_DIR,
    EXIT_DEPLOY,
    SYSTEMD_DIR,
    Context,
    OmnissaError,
    log,
    now_iso,
    project_file,
    run,
    sha256_bytes,
    which,
    write_managed_file,
)
from .settings import enabled_services, vip_for

LB_POLICY = {
    "least_request": "LEAST_REQUEST",
    "round_robin": "ROUND_ROBIN",
    "random": "RANDOM",
    "ring_hash": "RING_HASH",
    "maglev": "MAGLEV",
}
SERVICE_UNIT = "envoy.service"
LIMITS_DROPIN = SYSTEMD_DIR / "envoy.service.d" / "10-omnissa-limits.conf"
ENV_FILE = ETC_DIR / "envoy.env"
LAST_GOOD = ENVOY_CONFIG.with_name("envoy.yaml.last-good")


def template_path() -> Path:
    p = project_file("config/envoy.yaml.template")
    if not p.exists():
        raise OmnissaError("Nie znaleziono szablonu config/envoy.yaml.template", EXIT_DEPLOY)
    return p


def build_model(cfg: dict) -> dict:
    """Buduje model listenerów i klastrów. Listenery TCP współdzielące ten sam
    adres:port (wspólny VIP) są łączone w jeden listener z łańcuchami SNI."""
    groups: dict = {}
    clusters: dict = {}
    unknown = cfg["sni"]["unknown_sni_action"]
    for svc_name in enabled_services(cfg):
        svc = cfg["services"][svc_name]
        affinity = svc["session_affinity"] == "source_ip"
        policy = "RING_HASH" if affinity and svc["lb_policy"] not in ("ring_hash", "maglev") else LB_POLICY[svc["lb_policy"]]
        tcp_backend_ports = [l["backend_port"] for l in svc["listeners"] if l["protocol"] == "tcp"]
        for lst in svc["listeners"]:
            proto, bport = lst["protocol"], lst["backend_port"]
            cname = f"{svc_name}_{proto}_{bport}"
            hc = dict(svc["health_check"])
            if hc["type"] == "none":
                hc = None
            else:
                hc["host"] = hc.get("host") or svc["fqdn"]
                if proto == "udp" and not hc.get("port"):
                    # UDP nie ma natywnego health checka - sprawdzamy port TCP tej samej usługi
                    hc["port"] = tcp_backend_ports[0] if tcp_backend_ports else None
                    if hc["port"] is None:
                        log.warning("%s: brak portu TCP do health checka UDP - health check wyłączony", cname)
                        hc = None
            clusters.setdefault(cname, {
                "name": cname,
                "service": svc_name,
                "protocol": proto,
                "backend_port": bport,
                "type": "STATIC" if all(b["is_ip"] for b in svc["backends"]) else "STRICT_DNS",
                "lb_policy": policy,
                "health_check": hc,
                "endpoints": [{"address": b["address"], "weight": b["weight"]} for b in svc["backends"]],
            })
            key = (vip_for(cfg, svc_name), proto, lst["port"])
            g = groups.setdefault(key, {"address": key[0], "protocol": proto, "port": key[2], "members": []})
            g["members"].append({"service": svc_name, "cluster": cname, "affinity": affinity})

    listeners = []
    for (addr, proto, port), g in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0])):
        name = f"{proto}_{addr.replace('.', '_')}_{port}"
        services = [m["service"] for m in g["members"]]
        if proto == "udp":
            m = g["members"][0]
            listeners.append({"name": name, "protocol": "udp", "address": addr, "port": port, "services": services,
                              "cluster": m["cluster"], "stat_prefix": f"{m['service']}_udp_{port}",
                              "source_ip_hash": m["affinity"]})
            continue
        shared = len(g["members"]) > 1
        use_sni = shared or cfg["sni"].get("enforce_on_dedicated_vip", True)
        chains = []
        for m in g["members"]:
            svc = cfg["services"][m["service"]]
            chains.append({
                "name": f"{m['service']}_sni",
                "server_names": [svc["fqdn"], *svc["sni_aliases"]] if use_sni else [],
                "cluster": m["cluster"],
                "stat_prefix": f"{m['service']}_tcp_{port}",
                "source_ip_hash": m["affinity"],
            })
        if use_sni and unknown != "reject":
            target = next((m for m in g["members"] if m["service"] == unknown), None)
            if target:
                chains.append({"name": f"default_{unknown}", "server_names": [], "cluster": target["cluster"],
                               "stat_prefix": f"{unknown}_tcp_{port}_default", "source_ip_hash": target["affinity"]})
        listeners.append({"name": name, "protocol": "tcp", "address": addr, "port": port, "services": services,
                          "use_sni": use_sni, "filter_chains": chains})
    return {"listeners": listeners, "clusters": list(clusters.values())}


def render(cfg: dict) -> str:
    try:
        import jinja2
    except ImportError as exc:
        raise OmnissaError("Brak biblioteki Jinja2 (środowisko venv nie zostało przygotowane)", EXIT_DEPLOY) from exc
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path().parent)),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )
    env.policies["json.dumps_kwargs"] = {"sort_keys": False, "ensure_ascii": False}
    model = build_model(cfg)
    text = env.get_template(template_path().name).render(
        app_version=APP_VERSION,
        deployment=cfg["deployment"]["name"],
        generated_at=now_iso(),
        node_id=f"{cfg['deployment']['name']}-{socket.gethostname()}",
        envoy=cfg["envoy"],
        prometheus=cfg["monitoring"]["prometheus"],
        **model,
    )
    try:
        import yaml

        yaml.safe_load(text)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001 - błąd szablonu
        raise OmnissaError(f"Wygenerowany YAML Envoy jest niepoprawny: {exc}", EXIT_DEPLOY) from exc
    return text


def _strip_volatile(text: str) -> str:
    return "\n".join(l for l in text.splitlines() if "wygenerowano:" not in l)


def envoy_validate(path: Path) -> tuple[bool, str]:
    bin_ = ENVOY_BIN if Path(ENVOY_BIN).exists() else which("envoy")
    if not bin_:
        return False, "brak binarki envoy"
    r = run([bin_, "--mode", "validate", "-c", str(path), "--log-level", "warn"], check=False, timeout=60)
    return r.ok, (r.stdout + r.stderr).strip()[-2000:]


def envoy_version() -> str | None:
    bin_ = ENVOY_BIN if Path(ENVOY_BIN).exists() else which("envoy")
    if not bin_:
        return None
    r = run([bin_, "--version"], check=False)
    for tok in r.stdout.split():
        if tok.count("/") >= 2 and tok.split("/")[1][:1].isdigit():
            return tok.split("/")[1]
    return r.stdout.strip()[:80] or None


def write_config(ctx: Context, cfg: dict) -> bool:
    """Renderuje i waliduje konfigurację; podmienia plik tylko po pozytywnej walidacji."""
    text = render(cfg)
    current = ENVOY_CONFIG.read_text() if ENVOY_CONFIG.exists() else None
    if current is not None and _strip_volatile(current) == _strip_volatile(text):
        log.info("Konfiguracja Envoy bez zmian")
        return False
    if ctx.dry_run:
        ctx.plan("envoy", "write-config", path=str(ENVOY_CONFIG), sha256=sha256_bytes(text.encode())[:16])
        return True
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, dir="/tmp") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        ok, out = envoy_validate(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if not ok:
        raise OmnissaError(f"Envoy odrzucił wygenerowaną konfigurację:\n{out}", EXIT_DEPLOY,
                           hint="Szczegóły: journalctl -u envoy; poprzednia konfiguracja pozostała bez zmian")
    log.info("Walidacja Envoy (--mode validate): OK")
    write_managed_file(ctx, ENVOY_CONFIG, text, mode=0o640, owner="root", group="envoy", category="envoy")
    return True


def write_service_files(ctx: Context, cfg: dict) -> bool:
    env, res = cfg["envoy"], cfg["resources"]
    extra = f"--concurrency {env['concurrency']}" if env["concurrency"] else ""
    env_text = (
        "# Zarządzane przez omnissa-envoy-lb\n"
        f"ENVOY_CONFIG={ENVOY_CONFIG}\n"
        f"ENVOY_LOG_LEVEL={env['log_level']}\n"
        f"ENVOY_DRAIN_SECONDS={env['drain_seconds']}\n"
        f"ENVOY_ADMIN=http://{env['admin_address']}:{env['admin_port']}\n"
        f"ENVOY_EXTRA_ARGS={extra}\n"
    )
    lines = ["# Zarządzane przez omnissa-envoy-lb (resources w settings.yaml)", "[Service]",
             f"LimitNOFILE={res['nofile']}", f"TasksMax={res['tasks_max']}"]
    if res.get("memory_max"):
        lines += [f"MemoryMax={res['memory_max']}"]
    if res.get("cpu_quota"):
        lines += [f"CPUQuota={res['cpu_quota']}"]
    lines += [f"TimeoutStopSec={int(env['drain_seconds']) + 20}"]
    changed = write_managed_file(ctx, ENV_FILE, env_text, mode=0o644, category="systemd")
    changed |= write_managed_file(ctx, LIMITS_DROPIN, "\n".join(lines) + "\n", mode=0o644, category="systemd")
    return changed


def service_state(unit: str = SERVICE_UNIT) -> tuple[str, str]:
    a = run(["systemctl", "is-active", unit], check=False).stdout.strip() or "unknown"
    e = run(["systemctl", "is-enabled", unit], check=False).stdout.strip() or "unknown"
    return a, e


def admin_get(cfg: dict, path: str, timeout: float = 3.0) -> str | None:
    url = f"http://{cfg['envoy']['admin_address']}:{cfg['envoy']['admin_port']}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - tylko loopback
            return resp.read().decode("utf-8", "replace")
    except OSError:
        return None


def wait_ready(cfg: dict, timeout: int = 45) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (admin_get(cfg, "/ready") or "").strip() == "LIVE":
            return True
        time.sleep(1)
    return False


def deploy_service(ctx: Context, cfg: dict, config_changed: bool, units_changed: bool) -> str:
    """Włącza i (re)startuje Envoy tylko wtedy, gdy jest to potrzebne."""
    active, enabled = service_state()
    if units_changed:
        ctx.plan("systemd", "daemon-reload")
        if not ctx.dry_run:
            run(["systemctl", "daemon-reload"])
    if enabled != "enabled":
        ctx.plan("systemd", "enable", unit=SERVICE_UNIT)
        if not ctx.dry_run:
            run(["systemctl", "enable", SERVICE_UNIT])
            ctx.transaction.record("unit_enabled", SERVICE_UNIT)
    action = None
    if active != "active":
        action = "start"
    elif config_changed or units_changed:
        action = "restart"
    if not action:
        log.info("Envoy aktywny, konfiguracja bez zmian - brak restartu")
        return "unchanged"
    ctx.plan("systemd", action, unit=SERVICE_UNIT)
    if ctx.dry_run:
        return action
    ctx.transaction.record("envoy_restarted" if action == "restart" else "envoy_started", SERVICE_UNIT)
    r = run(["systemctl", action, SERVICE_UNIT], check=False, timeout=120)
    if not r.ok or not wait_ready(cfg):
        logs = run(["journalctl", "-u", SERVICE_UNIT, "-n", "60", "--no-pager"], check=False).stdout
        raise OmnissaError(f"Envoy nie osiągnął stanu LIVE po '{action}'.\n{logs[-3000:]}", EXIT_DEPLOY)
    log.info("Envoy: %s OK (stan LIVE)", action)
    shutil.copy2(ENVOY_CONFIG, LAST_GOOD)
    os.chmod(LAST_GOOD, 0o640)
    return action


def summary(cfg: dict) -> dict:
    m = build_model(cfg)
    return {
        "listeners": [
            {"name": l["name"], "protocol": l["protocol"], "address": l["address"], "port": l["port"],
             "services": l["services"],
             "sni": {fc["cluster"]: fc["server_names"] for fc in l.get("filter_chains", [])}}
            for l in m["listeners"]
        ],
        "clusters": [
            {"name": c["name"], "type": c["type"], "lb_policy": c["lb_policy"],
             "health_check": (c["health_check"] or {}).get("type", "none"),
             "endpoints": [f"{e['address']}:{c['backend_port']}" for e in c["endpoints"]]}
            for c in m["clusters"]
        ],
    }


def cluster_health(cfg: dict) -> dict:
    """Stan endpointów z /clusters?format=json: {cluster: [{host, healthy}]}."""
    raw = admin_get(cfg, "/clusters?format=json")
    out: dict = {}
    if not raw:
        return out
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return out
    for c in data.get("cluster_statuses", []):
        hosts = []
        for h in c.get("host_statuses", []):
            sa = h.get("address", {}).get("socket_address", {})
            hs = h.get("health_status", {})
            unhealthy = hs.get("failed_active_health_check") or hs.get("failed_outlier_check") or \
                hs.get("eds_health_status") in ("UNHEALTHY", "DRAINING", "TIMEOUT")
            hosts.append({"host": f"{sa.get('address')}:{sa.get('port_value')}", "healthy": not unhealthy})
        out[c.get("name")] = hosts
    return out
