#!/usr/bin/env bash
# =============================================================================
#  Omnissa Envoy Load Balancer - deinstalacja
#
#  sudo bash uninstall.sh [--dry-run] [--yes] [--purge] [--remove-envoy]
#
#  Domyślnie: zatrzymuje Envoy, usuwa VIP dodane przez instalator (runtime i plik
#  Netplan), reguły firewall, jednostki systemd i aplikację z /opt.
#  Zachowuje: konfigurację /etc/omnissa-lb, kopie zapasowe, certyfikaty, pakiet envoy.
#  --purge         usuwa również konfigurację, stan i logi (kopie zapasowe pozostają)
#  --remove-envoy  usuwa pakiet envoy i repozytorium - tylko jeśli zainstalował je instalator
# =============================================================================
set -Eeuo pipefail
export LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive

APP_DIR=/opt/omnissa-envoy-lb
ETC_DIR=/etc/omnissa-lb
STATE_DIR=/var/lib/omnissa-lb
LOG_DIR=/var/log/omnissa-lb
SYSTEMD_DIR=/etc/systemd/system
DRY_RUN=0 ASSUME_YES=0 PURGE=0 REMOVE_ENVOY=0

while (( $# )); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -y|--yes|--unattended) ASSUME_YES=1 ;;
    --purge) PURGE=1 ;;
    --remove-envoy) REMOVE_ENVOY=1 ;;
    -h|--help) sed -n '3,13p' "$0"; exit 0 ;;
    *) echo "Nieznana opcja: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ "$(id -u)" -eq 0 || "$DRY_RUN" == 1 ]] || { echo "Wymagane uprawnienia root (sudo)" >&2; exit 4; }
install -d -m 0750 "$LOG_DIR" 2>/dev/null || true
LOG_FILE="${LOG_DIR}/uninstall.log"
[[ "$DRY_RUN" == 1 ]] && LOG_FILE=/dev/null

log() { printf '%s [%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" "$2" | tee -a "$LOG_FILE" >&2; }
trap 'log ERROR "Błąd (kod $?) w linii $LINENO: $BASH_COMMAND"' ERR

act() {  # act "opis" polecenie...
  local desc="$1"; shift
  if [[ "$DRY_RUN" == 1 ]]; then log PLAN "$desc"; return 0; fi
  if "$@" >>"$LOG_FILE" 2>&1; then log OK "$desc"; else log WARN "$desc - niepowodzenie (kontynuuję)"; fi
}

olb() {
  if [[ -x "${APP_DIR}/venv/bin/python3" && -f "${APP_DIR}/omnissa-lb.py" ]]; then
    OMNISSA_LB_LOG_FILE="$LOG_FILE" "${APP_DIR}/venv/bin/python3" "${APP_DIR}/omnissa-lb.py" "$@"
  else
    return 127
  fi
}

if [[ "$ASSUME_YES" == 0 && "$DRY_RUN" == 0 ]]; then
  read -r -p "Usunąć Omnissa Envoy LB z tego hosta? [t/N]: " a </dev/tty
  [[ "${a,,}" =~ ^(t|tak|y|yes)$ ]] || { echo "Przerwano"; exit 13; }
fi

log INFO "Deinstalacja (dry-run=${DRY_RUN}, purge=${PURGE}, remove-envoy=${REMOVE_ENVOY})"

# 1. Kopia zapasowa przed usunięciem
if [[ "$DRY_RUN" == 0 ]]; then
  olb backup create --label pre-uninstall >>"$LOG_FILE" 2>&1 && log OK "Snapshot konfiguracji (pre-uninstall)" || log WARN "Nie wykonano snapshotu"
fi

# 2. Elementy runtime: VIP (tylko dodane przez instalator), Netplan, firewall, usługi
rr_args=(remove-runtime)
[[ "$DRY_RUN" == 1 ]] && rr_args+=(--dry-run)
if ! olb "${rr_args[@]}" >>"$LOG_FILE" 2>&1; then
  log WARN "Konfigurator niedostępny - usuwanie awaryjne"
  if [[ -f "${STATE_DIR}/state.json" ]] && command -v jq >/dev/null; then
    while IFS=$'\t' read -r cidr dev; do
      act "Usunięcie VIP ${cidr} z ${dev}" ip addr del "$cidr" dev "$dev"
    done < <(jq -r '.managed_vips[]? | [.cidr, .interface] | @tsv' "${STATE_DIR}/state.json")
  fi
  [[ -f /etc/netplan/60-omnissa-lb-vip.yaml ]] && act "Usunięcie pliku Netplan VIP" rm -f /etc/netplan/60-omnissa-lb-vip.yaml
  act "Usunięcie tabeli nftables omnissa_lb" nft delete table inet omnissa_lb
  act "Zatrzymanie Envoy" systemctl disable --now envoy.service
fi
[[ "$DRY_RUN" == 1 ]] && log PLAN "VIP/Netplan/firewall/usługi - szczegóły w planie konfiguratora"

# 3. Jednostki systemd
for u in omnissa-lb-healthcheck.timer omnissa-lb-healthcheck.service omnissa-lb-firewall.service envoy.service; do
  if [[ -f "${SYSTEMD_DIR}/${u}" ]] && grep -q "omnissa-envoy-lb" "${SYSTEMD_DIR}/${u}"; then
    act "Wyłączenie ${u}" systemctl disable --now "$u"
    act "Usunięcie ${SYSTEMD_DIR}/${u}" rm -f "${SYSTEMD_DIR}/${u}"
  fi
done
[[ -d "${SYSTEMD_DIR}/envoy.service.d" ]] && act "Usunięcie drop-in limitów" rm -f "${SYSTEMD_DIR}/envoy.service.d/10-omnissa-limits.conf"
act "systemctl daemon-reload" systemctl daemon-reload

# 4. Aplikacja (przy instalacji z paczki .deb pliki należą do dpkg - usuwa je apt)
if dpkg-query -W -f='${Status}' omnissa-envoy-lb 2>/dev/null | grep -q 'install ok installed'; then
  act "Usunięcie środowiska venv" rm -rf --one-file-system "${APP_DIR}/venv" "${APP_DIR}/.install-info"
  log INFO "Pliki aplikacji należą do pakietu omnissa-envoy-lb - usuń go: sudo apt purge omnissa-envoy-lb"
else
  act "Usunięcie ${APP_DIR}" rm -rf --one-file-system "$APP_DIR"
fi
act "Usunięcie poleceń /usr/local/sbin" rm -f /usr/local/sbin/omnissa-lb /usr/local/sbin/verify-omnissa-lb

# 5. Pakiet Envoy (tylko jeśli został zainstalowany przez instalator)
if [[ "$REMOVE_ENVOY" == 1 ]]; then
  if grep -qx envoy "${STATE_DIR}/installed-packages.txt" 2>/dev/null; then
    act "apt-mark unhold envoy" apt-mark unhold envoy
    act "Usunięcie pakietu envoy" apt-get purge -y -q envoy
    act "Usunięcie repozytorium Envoy" rm -f /etc/apt/sources.list.d/envoy.list /etc/apt/sources.list.d/envoy.sources /etc/apt/preferences.d/omnissa-envoy /etc/apt/keyrings/envoy-keyring.gpg
    act "apt-get update" apt-get update -q
  else
    log WARN "Pakiet envoy był zainstalowany przed instalatorem - pozostawiono"
  fi
fi

# 6. Purge: konfiguracja, stan, logi (certyfikaty i kopie zapasowe pozostają)
if [[ "$PURGE" == 1 ]]; then
  act "Usunięcie konfiguracji ${ETC_DIR}" rm -rf --one-file-system "$ETC_DIR"
  act "Usunięcie wygenerowanej konfiguracji Envoy" rm -f /etc/envoy/envoy.yaml /etc/envoy/envoy.yaml.last-good
  if grep -qx envoy "${STATE_DIR}/created-users.txt" 2>/dev/null && [[ "$REMOVE_ENVOY" == 1 ]]; then
    act "Usunięcie użytkownika envoy" userdel envoy
  fi
  act "Usunięcie stanu ${STATE_DIR}" rm -rf --one-file-system "$STATE_DIR"
  [[ -d /etc/envoy/certs ]] && log INFO "Pozostawiono certyfikaty w /etc/envoy/certs"
fi

log INFO "Pakiety systemowe (python3, nftables, ...) nie są usuwane. Kopie zapasowe: /var/backups/omnissa-lb"
log OK "Deinstalacja zakończona$([[ $DRY_RUN == 1 ]] && echo ' (DRY-RUN - bez zmian)')"
