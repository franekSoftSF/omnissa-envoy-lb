"""Backup, dziennik transakcji i rollback.

Format dziennika jest wspólny z install.sh (Bash):
  <BACKUP_DIR>/<id>/manifest.tsv  - file<TAB>ścieżka<TAB>istniał(0/1)<TAB>ścieżka-kopii
  <BACKUP_DIR>/<id>/actions.tsv   - akcja<TAB>arg1<TAB>arg2...
  <BACKUP_DIR>/<id>/files/...     - kopie plików (ścieżki absolutne odwzorowane)
"""

from __future__ import annotations

import os
import shutil
import tarfile
from datetime import datetime
from pathlib import Path

from .common import (
    BACKUP_DIR,
    DEFAULT_SETTINGS,
    ENVOY_CONFIG,
    ETC_DIR,
    NETPLAN_FILE,
    STATE_FILE,
    SYSTEMD_DIR,
    Context,
    OmnissaError,
    log,
    now_iso,
    run,
    save_json,
)

MANAGED_FILES = [
    DEFAULT_SETTINGS,
    ENVOY_CONFIG,
    NETPLAN_FILE,
    ETC_DIR / "nftables.nft",
    ETC_DIR / "envoy.env",
    SYSTEMD_DIR / "envoy.service",
    SYSTEMD_DIR / "envoy.service.d" / "10-omnissa-limits.conf",
    SYSTEMD_DIR / "omnissa-lb-firewall.service",
    SYSTEMD_DIR / "omnissa-lb-healthcheck.service",
    SYSTEMD_DIR / "omnissa-lb-healthcheck.timer",
    STATE_FILE,
]


def _new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


class Transaction:
    def __init__(self, txid: str, base: Path = BACKUP_DIR):
        self.id = txid
        self.dir = base / txid
        self.manifest = self.dir / "manifest.tsv"
        self.actions = self.dir / "actions.tsv"

    @classmethod
    def open(cls, txid: str | None, ctx: Context) -> "Transaction":
        tx = cls(txid or _new_id("tx"))
        if not ctx.dry_run:
            (tx.dir / "files").mkdir(parents=True, exist_ok=True)
            os.chmod(tx.dir, 0o700)
            if not (tx.dir / "meta.json").exists():
                save_json(tx.dir / "meta.json", {"id": tx.id, "created_at": now_iso(), "type": "transaction"}, 0o600)
        return tx

    def _backed_up(self) -> set[str]:
        if not self.manifest.exists():
            return set()
        return {ln.split("\t")[1] for ln in self.manifest.read_text().splitlines() if ln.startswith("file\t")}

    def backup_file(self, path: Path) -> None:
        """Zapisuje stan pliku przed pierwszą modyfikacją w tej transakcji."""
        path = Path(path)
        if str(path) in self._backed_up():
            return
        rel = ""
        existed = path.exists() or path.is_symlink()
        if existed:
            rel = "files" + str(path)
            dst = self.dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dst, follow_symlinks=False)
        with self.manifest.open("a", encoding="utf-8") as fh:
            fh.write(f"file\t{path}\t{1 if existed else 0}\t{rel}\n")

    def record(self, action: str, *args) -> None:
        with self.actions.open("a", encoding="utf-8") as fh:
            fh.write("\t".join([action, *map(str, args)]) + "\n")

    def entries(self, kind: str) -> list[list[str]]:
        f = self.manifest if kind == "file" else self.actions
        if not f.exists():
            return []
        rows = [ln.split("\t") for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return [r for r in rows if kind != "file" or r[0] == "file"]

    def restore_files(self, ctx: Context) -> list[str]:
        restored = []
        for row in reversed(self.entries("file")):
            _, path, existed, rel = (row + ["", "", "", ""])[:4]
            p = Path(path)
            if existed == "1":
                src = self.dir / rel
                ctx.plan("rollback", "restore", path=path)
                if not ctx.dry_run:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    if p.exists() or p.is_symlink():
                        p.unlink()
                    shutil.copy2(src, p, follow_symlinks=False)
            else:
                ctx.plan("rollback", "remove-new-file", path=path)
                if not ctx.dry_run and (p.exists() or p.is_symlink()):
                    p.unlink()
            restored.append(path)
        return restored


def rollback_transaction(ctx: Context, txid: str) -> dict:
    """Przywraca stan sprzed transakcji: pliki, VIP, firewall, usługi."""
    tx = Transaction(txid)
    if not tx.dir.exists():
        raise OmnissaError(f"Nie znaleziono transakcji {txid} w {BACKUP_DIR}")
    log.warning("Rollback transakcji %s", txid)
    result = {"transaction": txid, "files": [], "actions": [], "errors": []}
    actions = tx.entries("action")
    result["files"] = tx.restore_files(ctx)

    units_changed = any(str(SYSTEMD_DIR) in f for f in result["files"])
    if units_changed and not ctx.dry_run:
        run(["systemctl", "daemon-reload"], check=False)

    def _do(desc, cmd):
        ctx.plan("rollback", desc, cmd=" ".join(cmd))
        if ctx.dry_run:
            return
        r = run(cmd, check=False)
        result["actions"].append({"action": desc, "rc": r.rc})
        if not r.ok:
            result["errors"].append(f"{desc}: {r.stderr.strip()[:200]}")

    initial_state = {}
    for row in reversed(actions):
        act, args = row[0], row[1:]
        if act == "vip_added" and len(args) >= 2:
            _do(f"usuń VIP {args[1]} z {args[0]}", ["ip", "addr", "del", args[1], "dev", args[0]])
        elif act == "vip_removed" and len(args) >= 2:
            _do(f"przywróć VIP {args[1]} na {args[0]}", ["ip", "addr", "add", args[1], "dev", args[0]])
        elif act == "ufw_rule_added" and args:
            _do("usuń regułę ufw", ["ufw", "delete", *args[0].split()])
        elif act == "nft_applied":
            nft = ETC_DIR / "nftables.nft"
            if nft.exists():
                _do("przywróć reguły nftables", ["nft", "-f", str(nft)])
            else:
                _do("usuń tabelę nftables omnissa_lb", ["nft", "delete", "table", "inet", "omnissa_lb"])
        elif act == "unit_enabled" and args:
            _do(f"wyłącz {args[0]}", ["systemctl", "disable", "--now", args[0]])
        elif act == "service_state" and len(args) >= 3:
            initial_state[args[0]] = (args[1], args[2])

    # Envoy: przywrócenie poprzedniej działającej konfiguracji
    envoy_touched = any(a[0] in ("envoy_restarted", "envoy_started") for a in actions) or str(ENVOY_CONFIG) in result["files"]
    if envoy_touched:
        was_active = initial_state.get("envoy.service", ("inactive", ""))[0] == "active"
        if was_active and ENVOY_CONFIG.exists():
            _do("restart Envoy z poprzednią konfiguracją", ["systemctl", "restart", "envoy.service"])
        else:
            _do("zatrzymaj Envoy (nie działał przed instalacją)", ["systemctl", "stop", "envoy.service"])
    if not ctx.dry_run:
        save_json(tx.dir / "rollback.json", {**result, "finished_at": now_iso()}, 0o600)
    return result


def create_snapshot(ctx: Context, label: str = "manual") -> str | None:
    """Pełny snapshot plików zarządzanych (niezależny od transakcji)."""
    sid = _new_id("snap")
    target = BACKUP_DIR / sid
    ctx.plan("backup", "snapshot", id=sid, label=label)
    if ctx.dry_run:
        return sid
    (target / "files").mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    with (target / "manifest.tsv").open("w", encoding="utf-8") as fh:
        for p in MANAGED_FILES:
            if p.exists():
                dst = target / ("files" + str(p))
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                fh.write(f"file\t{p}\t1\tfiles{p}\n")
            else:
                fh.write(f"file\t{p}\t0\t\n")
    save_json(target / "meta.json", {"id": sid, "created_at": now_iso(), "type": "snapshot", "label": label}, 0o600)
    with tarfile.open(target / "snapshot.tar.gz", "w:gz") as tar:
        tar.add(target / "files", arcname="files")
    return sid


def list_backups() -> list[dict]:
    from .common import load_json

    out = []
    if not BACKUP_DIR.exists():
        return out
    for d in sorted(BACKUP_DIR.iterdir()):
        if d.is_dir():
            meta = load_json(d / "meta.json", {"id": d.name})
            meta["rolled_back"] = (d / "rollback.json").exists()
            out.append(meta)
    return out


def restore_snapshot(ctx: Context, sid: str) -> list[str]:
    snap = Transaction(sid)
    if not snap.manifest.exists():
        raise OmnissaError(f"Nie znaleziono kopii {sid}")
    # Przed przywróceniem: snapshot stanu bieżącego (możliwość cofnięcia rollbacku)
    create_snapshot(ctx, label=f"pre-restore-{sid}")
    return snap.restore_files(ctx)


def prune(ctx: Context, keep: int) -> None:
    items = sorted((d for d in BACKUP_DIR.glob("*-*") if d.is_dir()), key=lambda d: d.stat().st_mtime) \
        if BACKUP_DIR.exists() else []
    for d in items[:-keep] if keep > 0 else []:
        ctx.plan("backup", "prune", path=str(d))
        if not ctx.dry_run:
            shutil.rmtree(d, ignore_errors=True)
