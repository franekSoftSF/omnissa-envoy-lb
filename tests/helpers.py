"""Wspólne przygotowanie testów: izolowane katalogi (zmienne OMNISSA_LB_*) przed importem modułów."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="omnissa-lb-tests-"))
for key, sub in {
    "OMNISSA_LB_APP_DIR": "opt",
    "OMNISSA_LB_ETC_DIR": "etc/omnissa-lb",
    "OMNISSA_LB_STATE_DIR": "var/lib/omnissa-lb",
    "OMNISSA_LB_LOG_DIR": "var/log/omnissa-lb",
    "OMNISSA_LB_BACKUP_DIR": "var/backups/omnissa-lb",
    "OMNISSA_LB_ENVOY_ETC": "etc/envoy",
    "OMNISSA_LB_SYSTEMD_DIR": "etc/systemd/system",
    "OMNISSA_LB_NETPLAN_DIR": "etc/netplan",
}.items():
    os.environ[key] = str(TMP / sub)
    (TMP / sub).mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))

SHARED = ROOT / "config" / "settings.shared-vip.yaml"
DEDICATED = ROOT / "config" / "settings.dedicated-vip.yaml"
EXAMPLE = ROOT / "config" / "settings.example.yaml"


def write_cfg(text: str) -> Path:
    p = TMP / f"cfg-{abs(hash(text))}.yaml"
    p.write_text(text)
    return p
