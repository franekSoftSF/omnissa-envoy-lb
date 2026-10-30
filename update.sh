#!/usr/bin/env bash
# =============================================================================
#  Omnissa Envoy Load Balancer - aktualizacja
#
#  sudo bash update.sh [--dry-run] [--envoy-version X.Y.Z --upgrade-envoy] [--config PLIK]
#
#  Aktualizuje pliki aplikacji, zależności Python (requirements.txt), jednostki
#  systemd i - opcjonalnie - wersję Envoy. Zachowuje istniejącą konfigurację
#  (chyba że podano --config). Restart Envoy następuje tylko przy zmianie.
#  Wszystkie opcje są przekazywane do install.sh (tryb --mode update --unattended).
# =============================================================================
set -Eeuo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

if [[ ! -f /etc/omnissa-lb/settings.yaml && " $* " != *" --config "* ]]; then
  echo "Brak istniejącej instalacji (/etc/omnissa-lb/settings.yaml). Użyj install.sh." >&2
  exit 9
fi
for a in "$@"; do
  [[ "$a" == --mode || "$a" == --mode=* ]] && { echo "update.sh ustawia --mode update samodzielnie" >&2; exit 2; }
done
exec bash "${SCRIPT_DIR}/install.sh" --mode update --unattended "$@"
