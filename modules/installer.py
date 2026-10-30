"""Orkiestracja wdrożenia po stronie Python (wywoływana przez install.sh).

Kolejność: preflight -> VIP + Netplan -> konfiguracja Envoy -> pliki systemd ->
firewall -> (re)start Envoy -> monitoring -> zapis stanu. Każda zmiana pliku jest
backupowana w transakcji, a zmiany runtime (VIP, firewall) są zapisywane w dzienniku,
dzięki czemu install.sh może wykonać pełny rollback.
"""

from __future__ import annotations

from pathlib import Path

from . import backup, envoy_config, firewall, system_checks, vip_manager
from .common import SYSTEMD_DIR, project_file, Context, OmnissaError, State, log, now_iso, run, sha256_file, write_managed_file
from .settings import enabled_services, vip_for

TIMER_UNIT = "omnissa-lb-healthcheck.timer"


def _monitoring(ctx: Context, cfg: dict) -> bool:
    mon = cfg["monitoring"]
    src = project_file("systemd/omnissa-lb-healthcheck.timer")
    changed = False
    if src.exists():
        content = src.read_text().replace("@INTERVAL@", str(mon["interval"]))
        changed = write_managed_file(ctx, SYSTEMD_DIR / TIMER_UNIT, content, category="systemd")
    if changed and not ctx.dry_run:
        run(["systemctl", "daemon-reload"])
    enabled = run(["systemctl", "is-enabled", TIMER_UNIT], check=False).stdout.strip() == "enabled"
    if mon.get("healthcheck_timer") and not enabled:
        ctx.plan("systemd", "enable --now", unit=TIMER_UNIT)
        if not ctx.dry_run:
            run(["systemctl", "enable", "--now", TIMER_UNIT])
            ctx.transaction.record("unit_enabled", TIMER_UNIT)
    elif not mon.get("healthcheck_timer") and enabled:
        ctx.plan("systemd", "disable --now", unit=TIMER_UNIT)
        if not ctx.dry_run:
            run(["systemctl", "disable", "--now", TIMER_UNIT], check=False)
    elif changed and mon.get("healthcheck_timer") and not ctx.dry_run:
        run(["systemctl", "restart", TIMER_UNIT], check=False)
    return changed


def configure(ctx: Context, cfg: dict, settings_path: str) -> dict:
    report: dict = {"started_at": now_iso(), "dry_run": ctx.dry_run}
    report["system"] = system_checks.system_info()
    report["preflight"] = system_checks.preflight(cfg)
    iface = report["preflight"]["interface"]

    if not ctx.dry_run:
        ctx.transaction = backup.Transaction.open(ctx.transaction if isinstance(ctx.transaction, str) else None, ctx)
        for unit in ("envoy.service", firewall.FW_UNIT, TIMER_UNIT):
            a, e = envoy_config.service_state(unit)
            ctx.transaction.record("service_state", unit, a, e)
        ctx.transaction.backup_file(State().path)
        report["transaction"] = ctx.transaction.id

    log.info("== Adresy VIP (interfejs %s) ==", iface)
    report["vip"] = vip_manager.reconcile(ctx, cfg, iface)

    log.info("== Konfiguracja Envoy ==")
    try:
        config_changed = envoy_config.write_config(ctx, cfg)
    except OmnissaError as exc:
        if not ctx.dry_run:
            raise
        log.warning("Podgląd konfiguracji Envoy niedostępny w dry-run: %s", exc)
        config_changed = True
    units_changed = envoy_config.write_service_files(ctx, cfg)

    log.info("== Firewall ==")
    report["firewall"] = firewall.apply(ctx, cfg)

    log.info("== Usługa systemd ==")
    report["service_action"] = envoy_config.deploy_service(ctx, cfg, config_changed, units_changed)
    report["monitoring_changed"] = _monitoring(ctx, cfg)

    state = State()
    if not ctx.dry_run:
        state.set("settings_path", settings_path)
        state.set("settings_sha256", sha256_file(Path(settings_path)))
        state.set("envoy_config_sha256", sha256_file(envoy_config.ENVOY_CONFIG))
        state.set("envoy_version", envoy_config.envoy_version())
        state.set("deployed_at", now_iso())
        state.set("last_transaction", ctx.transaction.id)
        state.save(ctx)
        backup.prune(ctx, int(cfg["backup"]["keep"]))
    report["summary"] = envoy_config.summary(cfg)
    report["planned"] = ctx.planned
    report["finished_at"] = now_iso()
    return report


def describe(cfg: dict) -> dict:
    """Zwięzły opis konfiguracji (dry-run, raport, verify-omnissa-lb.sh)."""
    return {
        "interface": cfg["network"]["interface"],
        "vip_mode": cfg["vip"]["mode"],
        "services": {
            s: {
                "fqdn": cfg["services"][s]["fqdn"],
                "vip": vip_for(cfg, s),
                "listeners": cfg["services"][s]["listeners"],
                "backends": [b["address"] for b in cfg["services"][s]["backends"]],
            }
            for s in enabled_services(cfg)
        },
    }


def remove_runtime(ctx: Context, cfg: dict | None) -> dict:
    """Usuwa elementy runtime utworzone przez instalator (używane przez uninstall.sh)."""
    from . import network_manager as net

    out = {"vips": vip_manager.remove_all(ctx)}
    if net.NETPLAN_FILE.exists():
        ctx.plan("netplan", "remove", path=str(net.NETPLAN_FILE))
        if not ctx.dry_run:
            net.NETPLAN_FILE.unlink()
    firewall.remove(ctx)
    for unit in (TIMER_UNIT, "envoy.service"):
        ctx.plan("systemd", "disable --now", unit=unit)
        if not ctx.dry_run:
            run(["systemctl", "disable", "--now", unit], check=False)
    state = State()
    state.set("managed_vips", [])
    state.save(ctx)
    return out
