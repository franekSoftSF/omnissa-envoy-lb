#!/usr/bin/env python3
"""omnissa-lb - konfigurator Omnissa Envoy Load Balancer (Tunnel + SEG).

Warstwa 2 instalatora: konfiguracja interaktywna/unattended, VIP na jednym
interfejsie, generowanie konfiguracji Envoy (SNI, TCP/UDP), health checki,
firewall, monitoring, backup/rollback i diagnostyka.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from modules import backup, diagnostics, dns_manager, envoy_config, firewall, healthcheck, installer  # noqa: E402
from modules import network_manager as net  # noqa: E402
from modules import settings as settings_mod  # noqa: E402
from modules import system_checks, vip_manager, wizard  # noqa: E402
from modules.common import (  # noqa: E402
    APP_VERSION,
    DEFAULT_SETTINGS,
    EXIT_GENERIC,
    EXIT_TESTS,
    LOG_DIR,
    Context,
    OmnissaError,
    State,
    is_root,
    log,
    now_iso,
    save_json,
    setup_logging,
)


def _cfg(args) -> dict:
    return settings_mod.load(Path(args.config), getattr(args, "interface", None))


def _need_root(args) -> None:
    if not is_root() and not getattr(args, "dry_run", False):
        raise OmnissaError("To polecenie wymaga uprawnień root (sudo)", 4)


def _print(data, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    else:
        for k, v in data.items() if isinstance(data, dict) else enumerate(data):
            print(f"{k}: {json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v}")


def cmd_configure(args) -> int:
    _need_root(args)
    cfg_path = Path(args.config)
    if args.interactive and not args.unattended:
        target = cfg_path if not args.dry_run else Path("/tmp/omnissa-lb-wizard.yaml")
        wizard.run_wizard(target, base=cfg_path if cfg_path.exists() else None, interface=args.interface)
        cfg_path = target
    cfg = settings_mod.load(cfg_path, args.interface)
    ctx = Context(dry_run=args.dry_run, unattended=args.unattended, interface_override=args.interface,
                  transaction=args.transaction)
    if args.dry_run:
        ctx.transaction = None
    report = installer.configure(ctx, cfg, str(cfg_path))
    report["config"] = installer.describe(cfg)
    if args.report:
        save_json(Path(args.report), report, 0o640)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_plan(args) -> int:
    """Pełny plan zmian (dry-run) - bez modyfikacji systemu."""
    cfg = _cfg(args)
    ctx = Context(dry_run=True, unattended=True, interface_override=args.interface)
    d = installer.describe(cfg)
    info = system_checks.system_info()
    try:
        pre = system_checks.preflight(cfg)
    except OmnissaError as exc:
        pre = {"error": str(exc), "interface": cfg["network"]["interface"]}
        log.error("Preflight: %s", exc)
    print("\n========== PLAN WDROŻENIA (DRY-RUN) ==========")
    print(f"System:            {info['os']} ({info['codename']}, {info['arch']})")
    print(f"Interfejs:         {pre.get('interface')}")
    print(f"Główny adres IP:   {pre.get('primary_ip')}  (brama {pre.get('gateway')})")
    print(f"Tryb VIP:          {d['vip_mode']}")
    for s, v in d["services"].items():
        print(f"VIP {s:<14} {v['vip']}")
        print(f"FQDN {s:<13} {v['fqdn']}")
    print("\nListenery Envoy:")
    summ = envoy_config.summary(cfg)
    for l in summ["listeners"]:
        sni = "; ".join(f"{c} <- {', '.join(n) or '(bez SNI)'}" for c, n in l["sni"].items())
        print(f"  - {l['protocol'].upper()} {l['address']}:{l['port']} -> {', '.join(l['services'])}  {sni}")
    print("\nBackendy (klastry):")
    for c in summ["clusters"]:
        print(f"  - {c['name']}: {', '.join(c['endpoints'])}  [{c['lb_policy']}, health={c['health_check']}]")
    print("\nPlanowane reguły firewall:")
    backend = firewall.detect_backend(cfg)
    if backend:
        for r in firewall.rules(cfg):
            print(f"  - [{backend}] allow {r['protocol']}/{r['port']} -> {r['vip']} ({r['service']}) "
                  f"z {', '.join(cfg['firewall']['allowed_sources'])}")
        if cfg["firewall"]["restrict_vip"] and backend == "nftables":
            print("  - [nftables] drop: pozostały ruch kierowany na VIP")
    else:
        print("  - brak (firewall.manage=false)")
    print("\nPlanowane operacje (Netplan / VIP / Envoy / systemd):")
    if "error" not in pre:
        try:
            installer.configure(ctx, cfg, args.config)
        except OmnissaError as exc:
            log.error("Symulacja zatrzymana: %s", exc)
            pre["error"] = str(exc)
    print("==============================================\n")
    if args.report:
        save_json(Path(args.report), {"generated_at": now_iso(), "system": info, "preflight": pre,
                                      "config": d, "envoy": summ, "planned": ctx.planned}, 0o644)
    return 1 if "error" in pre else 0


def cmd_wizard(args) -> int:
    wizard.run_wizard(Path(args.output), Path(args.base) if args.base else None, args.interface)
    print(f"Zapisano: {args.output}")
    return 0


def cmd_init_config(args) -> int:
    """Tworzy settings.yaml dla Tunnel i SEG z parametrów (tryb unattended)."""
    split = lambda v: [x for x in (v or "").replace(",", " ").split() if x]  # noqa: E731
    cfg = wizard.build_settings({
        "mode": args.mode, "interface": args.interface, "name": args.name,
        "vip": args.vip, "tunnel_vip": args.tunnel_vip, "seg_vip": args.seg_vip,
        "tunnel": not args.no_tunnel, "seg": not args.no_seg,
        "tunnel_fqdn": args.tunnel_fqdn, "seg_fqdn": args.seg_fqdn,
        "tunnel_backends": split(args.tunnel_backends), "seg_backends": split(args.seg_backends),
        "tunnel_ports": args.tunnel_ports, "seg_ports": args.seg_ports,
        "health_check": args.health_check, "backend_check": args.backend_check,
        "firewall_backend": args.firewall, "allowed_sources": split(args.allowed_sources),
    })
    text = "# Wygenerowano: omnissa-lb init-config\n" + settings_mod.dump_yaml(cfg)
    if args.output == "-":
        sys.stdout.write(text)
        return 0
    out = Path(args.output)
    if out.exists() and not args.force:
        raise OmnissaError(f"Plik {out} już istnieje (użyj --force, aby nadpisać)", 9)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    out.chmod(0o640)
    print(f"Zapisano: {out}", file=sys.stderr)
    return 0


def cmd_validate(args) -> int:
    cfg = _cfg(args)
    print(f"Konfiguracja {args.config} poprawna. Usługi: {', '.join(settings_mod.enabled_services(cfg))}")
    return 0


def cmd_render(args) -> int:
    text = envoy_config.render(_cfg(args))
    if args.output in (None, "-"):
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text)
    return 0


def cmd_summary(args) -> int:
    cfg = _cfg(args)
    d = installer.describe(cfg)
    if args.format == "json":
        print(json.dumps({**d, "envoy": envoy_config.summary(cfg), "admin": f"{cfg['envoy']['admin_address']}:{cfg['envoy']['admin_port']}"}, indent=2))
        return 0
    # Format shell dla scripts/verify-omnissa-lb.sh (wartości bezpiecznie cytowane)
    st = State()
    out = {
        "OLB_INTERFACE": st.get("interface") or cfg["network"]["interface"],
        "OLB_ADMIN": f"{cfg['envoy']['admin_address']}:{cfg['envoy']['admin_port']}",
        "OLB_SERVICES": " ".join(d["services"]),
        "OLB_FIREWALL": firewall.detect_backend(cfg) or "none",
        "OLB_BACKEND_CHECK": cfg["validation"]["backend_check"],
        "OLB_SNI_CHECK": cfg["validation"]["sni_check"],
    }
    for s, v in d["services"].items():
        u = s.upper()
        out[f"OLB_{u}_FQDN"] = v["fqdn"]
        out[f"OLB_{u}_VIP"] = v["vip"]
        out[f"OLB_{u}_TCP_PORTS"] = " ".join(str(l["port"]) for l in v["listeners"] if l["protocol"] == "tcp")
        out[f"OLB_{u}_UDP_PORTS"] = " ".join(str(l["port"]) for l in v["listeners"] if l["protocol"] == "udp")
        out[f"OLB_{u}_BACKENDS"] = " ".join(v["backends"])
    for k, v in out.items():
        print(f"{k}={shlex.quote(str(v))}")
    return 0


def cmd_vip(args) -> int:
    cfg = _cfg(args)
    iface = net.resolve_interface(cfg)
    if args.action == "status":
        _print({"interface": iface, "vips": vip_manager.status(cfg, iface)}, args.json)
        return 0
    _need_root(args)
    ctx = Context(dry_run=args.dry_run)
    ctx.transaction = None if args.dry_run else backup.Transaction.open(None, ctx)
    if args.action == "apply":
        _print(vip_manager.reconcile(ctx, cfg, iface), args.json)
    else:
        _print({"removed": vip_manager.remove_all(ctx)}, args.json)
    return 0


def cmd_firewall(args) -> int:
    cfg = _cfg(args)
    if args.action == "status":
        st = firewall.status(cfg)
        print(st.get("ruleset") or json.dumps(st))
        return 0
    _need_root(args)
    ctx = Context(dry_run=args.dry_run)
    ctx.transaction = None if args.dry_run else backup.Transaction.open(None, ctx)
    if args.action == "apply":
        _print(firewall.apply(ctx, cfg), args.json)
    else:
        firewall.remove(ctx)
    return 0


def cmd_dns(args) -> int:
    res = dns_manager.check(_cfg(args))
    for r in res:
        print(f"[{'OK' if r['match'] else ('--' if r['match'] is None else 'UWAGA')}] {r['service']}: {r['message']}")
    return 0 if all(r["match"] is not False for r in res) else 1


def cmd_verify(args) -> int:
    cfg = _cfg(args)
    log.info("== Testy końcowe ==")
    res = healthcheck.run_checks(cfg, wait_for_health=args.wait)
    d = res.as_dict()
    if args.report:
        save_json(Path(args.report), d, 0o640)
    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
    c = d["counts"]
    log.info("Wynik: pass=%s warn=%s fail=%s skip=%s", c["pass"], c["warn"], c["fail"], c["skip"])
    if not d["success"]:
        log.error("Testy krytyczne NIE przeszły: %s", ", ".join(i["check"] for i in res.critical_failed))
        return EXIT_TESTS
    return 0


def cmd_monitor(args) -> int:
    return healthcheck.monitor_once(_cfg(args))


def cmd_status(args) -> int:
    cfg = _cfg(args)
    st = State()
    a, e = envoy_config.service_state()
    data = {
        "version": APP_VERSION,
        "envoy_version": envoy_config.envoy_version(),
        "service": {"active": a, "enabled": e},
        "interface": st.get("interface"),
        "managed_vips": st.get("managed_vips", []),
        "deployed_at": st.get("deployed_at"),
        "listeners": envoy_config.summary(cfg)["listeners"],
        "backends": envoy_config.cluster_health(cfg),
    }
    _print(data, args.json)
    return 0


def cmd_diagnostics(args) -> int:
    cfg = None
    try:
        cfg = _cfg(args)
    except OmnissaError as exc:
        log.warning("Konfiguracja niedostępna (%s) - zbieram diagnostykę bez admin API", exc)
    out = diagnostics.collect(cfg, Path(args.output) if args.output else LOG_DIR,
                              Path(args.extra) if args.extra else None)
    print(out)
    return 0


def cmd_backup(args) -> int:
    if args.action == "list":
        for b in backup.list_backups():
            flag = " (rollback wykonany)" if b.get("rolled_back") else ""
            print(f"{b.get('id')}\t{b.get('type', '?')}\t{b.get('created_at', '?')}\t{b.get('label', '')}{flag}")
        return 0
    _need_root(args)
    print(backup.create_snapshot(Context(), label=args.label))
    return 0


def cmd_rollback(args) -> int:
    _need_root(args)
    ctx = Context(dry_run=args.dry_run)
    if args.transaction:
        res = backup.rollback_transaction(ctx, args.transaction)
        _print(res, True)
        return 0 if not res["errors"] else EXIT_GENERIC
    bid = args.backup
    if bid == "latest":
        snaps = [b for b in backup.list_backups() if b.get("type") == "snapshot"]
        if not snaps:
            raise OmnissaError("Brak kopii zapasowych typu snapshot")
        bid = snaps[-1]["id"]
    files = backup.restore_snapshot(ctx, bid)
    log.info("Przywrócono %d plików z %s; ponowne zastosowanie konfiguracji", len(files), bid)
    if not args.dry_run and DEFAULT_SETTINGS.exists():
        cfg = settings_mod.load(DEFAULT_SETTINGS)
        installer.configure(Context(transaction=None), cfg, str(DEFAULT_SETTINGS))
    return 0


def cmd_remove_runtime(args) -> int:
    _need_root(args)
    cfg = None
    if Path(args.config).exists():
        try:
            cfg = _cfg(args)
        except OmnissaError:
            pass
    _print(installer.remove_runtime(Context(dry_run=args.dry_run), cfg), True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omnissa-lb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"omnissa-lb {APP_VERSION}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--log-file", default=None, help="dodatkowy plik logu (domyślnie OMNISSA_LB_LOG_FILE)")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp, interface=True):
        sp.add_argument("--config", default=str(DEFAULT_SETTINGS), help="ścieżka settings.yaml")
        if interface:
            sp.add_argument("--interface", default=None, help="nadpisuje network.interface")
        sp.add_argument("--json", action="store_true", help="wynik w formacie JSON")
        return sp

    sp = common(sub.add_parser("configure", help="wdróż/zaktualizuj konfigurację"))
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--unattended", action="store_true")
    g.add_argument("--interactive", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--transaction", default=None, help="identyfikator transakcji (z install.sh)")
    sp.add_argument("--report", default=None)
    sp.set_defaults(func=cmd_configure)

    sp = common(sub.add_parser("plan", help="plan zmian (dry-run)"))
    sp.add_argument("--report", default=None)
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("wizard", help="interaktywny kreator settings.yaml")
    sp.add_argument("--output", required=True)
    sp.add_argument("--base", default=None)
    sp.add_argument("--interface", default=None)
    sp.set_defaults(func=cmd_wizard)

    sp = sub.add_parser("init-config", help="utwórz settings.yaml dla Tunnel i SEG z parametrów (bez pytań)",
                        description="Przykład: omnissa-lb init-config --vip 10.0.0.50 --tunnel-fqdn tunnel.example.com "
                                    "--tunnel-backends 10.0.1.11,10.0.1.12 --seg-fqdn seg.example.com "
                                    "--seg-backends 10.0.1.21,10.0.1.22 --interface ens160 --output settings.yaml")
    sp.add_argument("--output", default="-", help="plik wynikowy (domyślnie stdout)")
    sp.add_argument("--force", action="store_true", help="nadpisz istniejący plik")
    sp.add_argument("--name", default=None, help="nazwa wdrożenia")
    sp.add_argument("--interface", default=None, help="interfejs VIP (domyślnie auto)")
    sp.add_argument("--mode", choices=("shared", "dedicated"), default="shared",
                    help="shared: wspólny VIP (SNI), dedicated: osobne VIP")
    sp.add_argument("--vip", help="wspólny VIP (mode shared)")
    sp.add_argument("--tunnel-vip", help="VIP Tunnel (mode dedicated)")
    sp.add_argument("--seg-vip", help="VIP SEG (mode dedicated)")
    for svc, label in (("tunnel", "Tunnel"), ("seg", "SEG")):
        sp.add_argument(f"--{svc}-fqdn", help=f"FQDN {label}")
        sp.add_argument(f"--{svc}-backends", help=f"backendy {label} (IP/FQDN, przecinki)")
        sp.add_argument(f"--{svc}-ports", help=f"listenery {label}, np. tcp/443,udp/443")
        sp.add_argument(f"--no-{svc}", action="store_true", help=f"wyłącz {label}")
    sp.add_argument("--health-check", choices=("tcp", "http", "https", "none"), default="tcp")
    sp.add_argument("--backend-check", choices=("critical", "warning", "skip"), default="critical",
                    help="czy niedostępne backendy/SNI blokują instalację")
    sp.add_argument("--firewall", choices=("auto", "nftables", "ufw", "none"), default="auto")
    sp.add_argument("--allowed-sources", default=None, help="sieci źródłowe IPv4 (przecinki)")
    sp.set_defaults(func=cmd_init_config)

    common(sub.add_parser("validate", help="walidacja settings.yaml")).set_defaults(func=cmd_validate)
    sp = common(sub.add_parser("render", help="wygeneruj envoy.yaml"))
    sp.add_argument("--output", default="-")
    sp.set_defaults(func=cmd_render)
    sp = common(sub.add_parser("summary", help="podsumowanie konfiguracji"))
    sp.add_argument("--format", choices=("json", "shell"), default="json")
    sp.set_defaults(func=cmd_summary)

    for name, fn, actions in (("vip", cmd_vip, ("status", "apply", "remove")),
                              ("firewall", cmd_firewall, ("status", "apply", "remove"))):
        sp = common(sub.add_parser(name, help=f"zarządzanie: {name}"))
        sp.add_argument("action", choices=actions)
        sp.add_argument("--dry-run", action="store_true")
        sp.set_defaults(func=fn)

    common(sub.add_parser("dns", help="sprawdź DNS FQDN Tunnel/SEG")).set_defaults(func=cmd_dns)
    sp = common(sub.add_parser("verify", help="testy końcowe (krytyczne/ostrzegawcze)"))
    sp.add_argument("--report", default=None)
    sp.add_argument("--wait", type=int, default=45, help="czas oczekiwania na health checki [s]")
    sp.set_defaults(func=cmd_verify)
    common(sub.add_parser("monitor", help="jednorazowy test (timer systemd)")).set_defaults(func=cmd_monitor)
    common(sub.add_parser("status", help="stan usługi, VIP i backendów")).set_defaults(func=cmd_status)
    sp = common(sub.add_parser("diagnostics", help="pakiet diagnostyczny"))
    sp.add_argument("--output", default=None)
    sp.add_argument("--extra", default=None, help=argparse.SUPPRESS)
    sp.set_defaults(func=cmd_diagnostics)

    sp = sub.add_parser("backup", help="kopie zapasowe")
    sp.add_argument("action", choices=("create", "list"))
    sp.add_argument("--label", default="manual")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("rollback", help="przywróć transakcję lub kopię")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--transaction")
    g.add_argument("--backup", help="id kopii lub 'latest'")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_rollback)

    sp = common(sub.add_parser("remove-runtime", help=argparse.SUPPRESS), interface=False)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_remove_runtime, interface=None)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, Path(args.log_file) if args.log_file else None)
    try:
        return args.func(args)
    except OmnissaError as exc:
        log.error("%s", exc)
        if exc.hint:
            log.error("Wskazówka: %s", exc.hint)
        return exc.code
    except KeyboardInterrupt:
        log.error("Przerwano")
        return 130


if __name__ == "__main__":
    sys.exit(main())
