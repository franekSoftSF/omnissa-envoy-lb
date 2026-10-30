"""Zbieranie pakietu diagnostycznego (bez sekretów)."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

from . import envoy_config
from .common import DEFAULT_SETTINGS, ENVOY_CONFIG, LOG_DIR, STATE_FILE, log, redact, run

COMMANDS = {
    "os-release.txt": ["cat", "/etc/os-release"],
    "ip-addr.txt": ["ip", "addr", "show"],
    "ip-route.txt": ["ip", "route", "show", "table", "all"],
    "ss-listen.txt": ["ss", "-lntup"],
    "systemctl-envoy.txt": ["systemctl", "status", "envoy.service", "--no-pager", "-l"],
    "journal-envoy.txt": ["journalctl", "-u", "envoy.service", "-n", "1000", "--no-pager"],
    "journal-omnissa.txt": ["journalctl", "-t", "omnissa-lb", "-u", "omnissa-lb-healthcheck.service", "-n", "300", "--no-pager"],
    "envoy-version.txt": ["envoy", "--version"],
    "envoy-validate.txt": ["envoy", "--mode", "validate", "-c", str(ENVOY_CONFIG)],
    "dpkg-envoy.txt": ["dpkg-query", "-W", "-f=${Package} ${Version} ${Status}\n", "envoy"],
    "apt-policy-envoy.txt": ["apt-cache", "policy", "envoy"],
    "nft-omnissa.txt": ["nft", "list", "table", "inet", "omnissa_lb"],
    "ufw-status.txt": ["ufw", "status", "verbose"],
    "netplan-get.txt": ["netplan", "get", "all"],
    "resolvectl.txt": ["resolvectl", "status"],
}
ADMIN_PATHS = {"envoy-clusters.json": "/clusters?format=json", "envoy-listeners.json": "/listeners?format=json",
               "envoy-server-info.json": "/server_info", "envoy-stats.txt": "/stats",
               "envoy-config-dump.json": "/config_dump"}
FILES = [DEFAULT_SETTINGS, ENVOY_CONFIG, STATE_FILE, Path("/etc/netplan/60-omnissa-lb-vip.yaml"),
         Path("/etc/omnissa-lb/nftables.nft"), LOG_DIR / "install.log"]


def collect(cfg: dict | None, out_dir: Path = LOG_DIR, extra_dir: Path | None = None) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="omnissa-diag-") as tmp:
        base = Path(tmp) / f"omnissa-lb-diagnostics-{ts}"
        base.mkdir()
        for name, cmd in COMMANDS.items():
            r = run(cmd, check=False, timeout=30)
            (base / name).write_text(redact(f"$ {' '.join(cmd)}\nrc={r.rc}\n{r.stdout}\n{r.stderr}"))
        if cfg:
            for name, path in ADMIN_PATHS.items():
                (base / name).write_text(redact(envoy_config.admin_get(cfg, path, timeout=5) or "brak odpowiedzi admin API"))
        for f in FILES:
            if f.exists():
                (base / f.name).write_text(redact(f.read_text(errors="replace")[-2_000_000:]))
        if extra_dir and extra_dir.exists():
            shutil.copytree(extra_dir, base / "extra", dirs_exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)
        archive = out_dir / f"diagnostics-{ts}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(base, arcname=base.name)
        os.chmod(archive, 0o600)
    log.info("Pakiet diagnostyczny: %s", archive)
    return archive
