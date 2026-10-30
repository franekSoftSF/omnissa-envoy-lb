"""Wspólne narzędzia: logowanie, uruchamianie poleceń, kody błędów, ścieżki."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

APP_NAME = "omnissa-envoy-lb"
APP_VERSION = "1.0.0"

APP_DIR = Path(os.environ.get("OMNISSA_LB_APP_DIR", "/opt/omnissa-envoy-lb"))
ETC_DIR = Path(os.environ.get("OMNISSA_LB_ETC_DIR", "/etc/omnissa-lb"))
STATE_DIR = Path(os.environ.get("OMNISSA_LB_STATE_DIR", "/var/lib/omnissa-lb"))
LOG_DIR = Path(os.environ.get("OMNISSA_LB_LOG_DIR", "/var/log/omnissa-lb"))
BACKUP_DIR = Path(os.environ.get("OMNISSA_LB_BACKUP_DIR", "/var/backups/omnissa-lb"))
ENVOY_ETC = Path(os.environ.get("OMNISSA_LB_ENVOY_ETC", "/etc/envoy"))
SYSTEMD_DIR = Path(os.environ.get("OMNISSA_LB_SYSTEMD_DIR", "/etc/systemd/system"))
NETPLAN_DIR = Path(os.environ.get("OMNISSA_LB_NETPLAN_DIR", "/etc/netplan"))

DEFAULT_SETTINGS = ETC_DIR / "settings.yaml"
ENVOY_CONFIG = ENVOY_ETC / "envoy.yaml"
STATE_FILE = STATE_DIR / "state.json"
NETPLAN_FILE = NETPLAN_DIR / "60-omnissa-lb-vip.yaml"
ENVOY_BIN = os.environ.get("OMNISSA_LB_ENVOY_BIN", "/usr/bin/envoy")

# Kody wyjścia współdzielone z install.sh
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_CONFIG = 9
EXIT_DEPLOY = 10
EXIT_TESTS = 11
EXIT_ABORT = 13

_SECRET_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|passphrase|private[_-]?key)"
    r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\",]+)"
)


def redact(text: str) -> str:
    """Maskuje wartości wyglądające na sekrety przed zapisem do logu."""
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class OmnissaError(Exception):
    """Błąd z kodem wyjścia i czytelnym komunikatem dla administratora."""

    def __init__(self, message: str, code: int = EXIT_GENERIC, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(verbose: bool = False, log_file: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("omnissa-lb")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(_RedactingFormatter("[%(levelname)s] %(message)s"))
    logger.addHandler(console)
    if log_file is None:
        env_log = os.environ.get("OMNISSA_LB_LOG_FILE")
        log_file = Path(env_log) if env_log else None
    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                _RedactingFormatter(
                    "%(asctime)s [%(levelname)s] [python:%(module)s] %(message)s",
                    "%Y-%m-%dT%H:%M:%S%z",
                )
            )
            logger.addHandler(fh)
        except OSError as exc:  # brak uprawnień (np. dry-run jako zwykły użytkownik)
            logger.debug("Nie można otworzyć logu %s: %s", log_file, exc)
    return logger


log = logging.getLogger("omnissa-lb")


@dataclass
class CmdResult:
    cmd: list[str]
    rc: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class Context:
    """Kontekst wykonania przekazywany do modułów."""

    dry_run: bool = False
    unattended: bool = False
    interface_override: str | None = None
    transaction: "object | None" = None  # backup.Transaction
    planned: list[dict] = field(default_factory=list)

    def plan(self, category: str, action: str, **details) -> None:
        entry = {"category": category, "action": action, **details}
        self.planned.append(entry)
        prefix = "[DRY-RUN] " if self.dry_run else ""
        extra = " ".join(f"{k}={v}" for k, v in details.items() if v not in (None, "", []))
        log.info("%s%s: %s %s", prefix, category, action, extra)


def run(
    cmd: Sequence[str],
    check: bool = True,
    timeout: int = 120,
    input_text: str | None = None,
    env: dict | None = None,
) -> CmdResult:
    """Uruchamia polecenie bez powłoki. Nie loguje stdin (może zawierać dane wrażliwe)."""
    cmd = [str(c) for c in cmd]
    log.debug("exec: %s", " ".join(shlex.quote(c) for c in cmd))
    try:
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
        )
    except FileNotFoundError as exc:
        if check:
            raise OmnissaError(f"Brak polecenia: {cmd[0]}", EXIT_DEPLOY) from exc
        return CmdResult(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        if check:
            raise OmnissaError(f"Przekroczono czas wykonania ({timeout}s): {cmd[0]}", EXIT_DEPLOY) from exc
        return CmdResult(cmd, 124, "", "timeout")
    res = CmdResult(cmd, proc.returncode, proc.stdout, proc.stderr)
    if proc.returncode != 0:
        log.debug("rc=%s stderr=%s", proc.returncode, proc.stderr.strip()[:2000])
        if check:
            raise OmnissaError(
                f"Polecenie zakończone błędem (rc={proc.returncode}): {' '.join(cmd[:4])} ... "
                f"{proc.stderr.strip()[:500]}",
                EXIT_DEPLOY,
            )
    return res


def project_file(rel: str) -> Path:
    """Plik projektu: najpierw obok uruchomionego kodu (źródła/dry-run), potem APP_DIR."""
    for base in (Path(__file__).resolve().parent.parent, APP_DIR):
        p = base / rel
        if p.exists():
            return p
    return APP_DIR / rel


def which(name: str) -> str | None:
    for d in os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin").split(":"):
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except FileNotFoundError:
        return None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def is_root() -> bool:
    return os.geteuid() == 0


def write_managed_file(
    ctx: Context,
    path: Path,
    content: str,
    mode: int = 0o644,
    owner: str | None = None,
    group: str | None = None,
    category: str = "file",
) -> bool:
    """Zapisuje plik tylko gdy treść się zmieniła. Przed nadpisaniem wykonuje backup
    w bieżącej transakcji. Zwraca True, jeśli plik został (lub zostałby) zmieniony."""
    new = content.encode("utf-8")
    current = path.read_bytes() if path.exists() else None
    if current == new:
        log.debug("Bez zmian: %s", path)
        return False
    ctx.plan(category, "write" if current is None else "update", path=str(path))
    if ctx.dry_run:
        return True
    if ctx.transaction is not None:
        ctx.transaction.backup_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.omnissa-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(new)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    if owner or group:
        import grp
        import pwd

        uid = pwd.getpwnam(owner).pw_uid if owner else -1
        gid = grp.getgrnam(group).gr_gid if group else -1
        os.chown(tmp, uid, gid)
    os.replace(tmp, path)
    return True


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


class State:
    """Trwały stan instalacji (co zostało utworzone przez instalator)."""

    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.data = load_json(path, {})

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value) -> None:
        self.data[key] = value

    def save(self, ctx: Context) -> None:
        if ctx.dry_run:
            return
        self.data["updated_at"] = now_iso()
        save_json(self.path, self.data)


def unique(items: Iterable) -> list:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out
