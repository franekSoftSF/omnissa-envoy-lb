#!/usr/bin/env bash
# =============================================================================
#  Omnissa Envoy Load Balancer - instalator (warstwa 1: Bash)
#
#  sudo bash install.sh                                       # interaktywnie
#  sudo bash install.sh --unattended --config ./config/settings.yaml
#  sudo bash install.sh --unattended --config ./config/settings.yaml --dry-run
#  sudo bash install.sh --unattended --interface ens160 --config ./config/settings.yaml
#
#  Warstwa 1 (ten skrypt): system, uprawnienia, repozytoria, zależności APT,
#  Envoy Proxy, środowisko Python (venv), katalogi, systemd, uruchomienie
#  konfiguratora i testów końcowych. Warstwa 2: omnissa-lb.py.
# =============================================================================
set -Eeuo pipefail
umask 027
export LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive

readonly INSTALLER_VERSION="1.0.0"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR

readonly APP_DIR=/opt/omnissa-envoy-lb
readonly VENV_DIR="${APP_DIR}/venv"
readonly ETC_DIR=/etc/omnissa-lb
readonly SETTINGS="${ETC_DIR}/settings.yaml"
readonly STATE_DIR=/var/lib/omnissa-lb
readonly LOG_DIR=/var/log/omnissa-lb
readonly BACKUP_ROOT=/var/backups/omnissa-lb
readonly ENVOY_ETC=/etc/envoy
readonly SYSTEMD_DIR=/etc/systemd/system
readonly CLI_WRAPPER=/usr/local/sbin/omnissa-lb
readonly VERIFY_LINK=/usr/local/sbin/verify-omnissa-lb
readonly LOCK_FILE=/run/lock/omnissa-lb-install.lock

# Oficjalne repozytorium Envoy; --envoy-repo-url pozwala wskazać wewnętrzny mirror (HTTPS)
ENVOY_REPO_URL="https://apt.envoyproxy.io"
ENVOY_KEY_URL=""
readonly ENVOY_KEYRING=/etc/apt/keyrings/envoy-keyring.gpg
readonly ENVOY_SOURCES=/etc/apt/sources.list.d/envoy.sources
readonly ENVOY_PREFS=/etc/apt/preferences.d/omnissa-envoy

readonly SUPPORTED_VERSIONS="24.04 26.04"
readonly SUPPORTED_ARCHS="amd64 arm64"

# Kody wyjścia
readonly E_GENERIC=1 E_USAGE=2 E_OS=3 E_ROOT=4 E_NETWORK=5 E_DEPS=6 E_ENVOY=7 E_PYTHON=8 \
         E_CONFIG=9 E_DEPLOY=10 E_TESTS=11 E_LOCK=12 E_ABORT=13

# ------------------------------------------------------------------ opcje
UNATTENDED=0
DRY_RUN=0
CONFIG=""
INTERFACE=""
MODE="auto"            # auto | install | update | reconfigure | repair
ENVOY_VERSION_OPT=""
APT_SUITE_OPT=""
KEY_FPR_OPT=""
UPGRADE_ENVOY=0
KEEP_ON_FAILURE=0
ASSUME_YES=0
VERBOSE=0
WHEELHOUSE=""
NO_COLOR="${NO_COLOR:-}"

# ------------------------------------------------------------------ stan
TS="$(date +%Y%m%d-%H%M%S)"
TXID="tx-${TS}-$$"
TX_DIR=""
TX_STARTED=0
FAILURE_HANDLED=0
CURRENT_OP="init"
WORK_DIR=""
LOG_FILE="${LOG_DIR}/install.log"
STEPS_FILE=""
REPORT_FILE=""
OS_ID="" OS_VERSION="" OS_CODENAME="" OS_PRETTY="" ARCH=""
ENVOY_SUITE="" ENVOY_KEY_FPR="" ENVOY_TARGET_VERSION="" ENVOY_PRE_VERSION="" ENVOY_FINAL_VERSION=""
ENVOY_BIN=/usr/bin/envoy
EXISTING_INSTALL=0
PKGS_MISSING=()
PKGS_REQUIRED=()
PKGS_INSTALLED_NOW=()
UNITS_CHANGED=0

APT_PROXY=""
DRYRUN_ISSUES=()

# ================================================================== logowanie
if [[ -t 1 && -z "${NO_COLOR}" ]]; then
  C_R=$'\e[31m' C_G=$'\e[32m' C_Y=$'\e[33m' C_B=$'\e[34m' C_D=$'\e[2m' C_0=$'\e[0m'
else
  C_R="" C_G="" C_Y="" C_B="" C_D="" C_0=""
fi

redact() { sed -E 's/((password|passwd|secret|token|api[_-]?key|passphrase)[[:space:]]*[=:][[:space:]]*)[^[:space:],"]+/\1***/Ig'; }

log() {  # log LEVEL MESSAGE...
  local level="$1"; shift
  local msg="$*" ts color
  ts="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  case "$level" in
    ERROR) color="$C_R" ;; WARN) color="$C_Y" ;; OK) color="$C_G" ;; STEP) color="$C_B" ;; DEBUG) color="$C_D" ;; *) color="" ;;
  esac
  if [[ "$level" != "DEBUG" || "$VERBOSE" == 1 ]]; then
    printf '%s%-5s%s %s\n' "$color" "$level" "$C_0" "$msg" >&2
  fi
  if [[ -n "$LOG_FILE" && -w "$(dirname "$LOG_FILE")" ]]; then
    printf '%s [%s] [%s] %s\n' "$ts" "$level" "$CURRENT_OP" "$msg" | redact >>"$LOG_FILE"
  fi
}
info()  { log INFO "$@"; }
warn()  { log WARN "$@"; }
ok()    { log OK "$@"; }
debug() { log DEBUG "$@"; }
section() { CURRENT_OP="$1"; log STEP "==== $2 ===="; }

record_step() {  # record_step OP RESULT RC MESSAGE
  [[ -n "$STEPS_FILE" ]] || return 0
  printf '%s\t%s\t%s\t%s\t%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$1" "$2" "$3" "$4" | redact >>"$STEPS_FILE"
}

# run_step OP "opis" polecenie... - wykonuje, loguje wynik i kod błędu
run_step() {
  local op="$1" desc="$2"; shift 2
  local rc=0 prev_op="$CURRENT_OP"
  CURRENT_OP="$op"
  debug "exec: $*"
  if [[ -n "$LOG_FILE" && -w "$(dirname "$LOG_FILE")" ]]; then
    "$@" >>"$LOG_FILE" 2>&1 || rc=$?
  else
    "$@" >/dev/null 2>&1 || rc=$?
  fi
  if (( rc == 0 )); then
    log OK "$desc"
    record_step "$op" ok 0 "$desc"
  else
    log ERROR "$desc - niepowodzenie (kod $rc)"
    record_step "$op" failed "$rc" "$desc"
  fi
  CURRENT_OP="$prev_op"
  return "$rc"
}

# ================================================================== obsługa błędów
# check_fail KOD KOMUNIKAT [WSKAZÓWKA] - w dry-run zapisuje problem i kontynuuje, inaczej die
check_fail() {
  if [[ "$DRY_RUN" == 1 ]]; then
    log ERROR "$2"
    [[ -n "${3:-}" ]] && log ERROR "Wskazówka: $3"
    record_step "$CURRENT_OP" failed "$1" "$2"
    DRYRUN_ISSUES+=("$2")
    return 0
  fi
  die "$@"
}

die() {  # die KOD KOMUNIKAT [WSKAZÓWKA]
  local code="$1" msg="$2" hint="${3:-}"
  log ERROR "$msg"
  [[ -n "$hint" ]] && log ERROR "Wskazówka: $hint"
  record_step "$CURRENT_OP" failed "$code" "$msg"
  handle_failure "$code" "$msg"
  exit "$code"
}

on_err() {
  local rc=$? line="$1" cmd="$2"
  trap - ERR
  set +e
  log ERROR "Nieoczekiwany błąd (kod $rc) w linii $line: $cmd"
  record_step "$CURRENT_OP" failed "$rc" "linia $line: $cmd"
  handle_failure "$rc" "Błąd w kroku '$CURRENT_OP' (linia $line)"
  exit "$rc"
}

on_signal() {
  trap - ERR INT TERM
  set +e
  log ERROR "Instalacja przerwana sygnałem"
  handle_failure 130 "Przerwano (sygnał)"
  exit 130
}

on_exit() {
  trap - ERR
  if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then rm -rf -- "$WORK_DIR"; fi
}

trap 'on_err "$LINENO" "$BASH_COMMAND"' ERR
trap on_signal INT TERM
trap on_exit EXIT

py() {  # interpreter Python projektu (venv, a w dry-run - systemowy)
  if [[ -x "${VENV_DIR}/bin/python3" ]] && "${VENV_DIR}/bin/python3" -c 'import yaml, jinja2' >/dev/null 2>&1; then
    printf '%s' "${VENV_DIR}/bin/python3"
  else
    printf '%s' "/usr/bin/python3"
  fi
}

olb() {  # wywołanie konfiguratora (kod z katalogu aplikacji, w dry-run - ze źródeł)
  local entry="${APP_DIR}/omnissa-lb.py"
  [[ "$DRY_RUN" == 1 || ! -f "$entry" ]] && entry="${SCRIPT_DIR}/omnissa-lb.py"
  OMNISSA_LB_LOG_FILE="$LOG_FILE" "$(py)" "$entry" "$@"
}

handle_failure() {
  local code="$1" msg="$2"
  (( FAILURE_HANDLED )) && return 0
  FAILURE_HANDLED=1
  set +e
  if [[ "$DRY_RUN" == 1 || "$TX_STARTED" != 1 ]]; then
    write_report failed "$code" "$msg"
    return 0
  fi
  CURRENT_OP="failure"
  warn "Zatrzymano instalację. Zapisuję diagnostykę i przywracam poprzedni stan (transakcja ${TXID})."
  collect_failure_diagnostics
  if [[ "$KEEP_ON_FAILURE" == 1 ]]; then
    warn "--keep-on-failure: rollback pominięty. Ręcznie: omnissa-lb rollback --transaction ${TXID}"
  else
    rollback_all
  fi
  write_report failed "$code" "$msg"
  log ERROR "Instalacja NIE powiodła się (kod $code). Log: ${LOG_FILE}, raport: ${REPORT_FILE}"
}

collect_failure_diagnostics() {
  local d="${TX_DIR}/diagnostics"
  mkdir -p "$d"
  {
    ip addr show; echo; ip route show
  } >"$d/network.txt" 2>&1
  ss -lntup >"$d/sockets.txt" 2>&1
  systemctl status envoy.service --no-pager -l >"$d/envoy-status.txt" 2>&1
  journalctl -u envoy.service -n 300 --no-pager >"$d/envoy-journal.txt" 2>&1
  [[ -x "$ENVOY_BIN" && -f "${ENVOY_ETC}/envoy.yaml" ]] && \
    "$ENVOY_BIN" --mode validate -c "${ENVOY_ETC}/envoy.yaml" >"$d/envoy-validate.txt" 2>&1
  tail -n 400 "$LOG_FILE" >"$d/install-log-tail.txt" 2>/dev/null
  if [[ -x "${VENV_DIR}/bin/python3" && -f "${APP_DIR}/omnissa-lb.py" ]]; then
    olb diagnostics --config "$SETTINGS" --output "$d" >/dev/null 2>&1
  fi
  info "Diagnostyka zapisana w ${d}"
}

rollback_all() {
  CURRENT_OP="rollback"
  warn "Rollback: przywracanie plików, VIP, firewall i konfiguracji Envoy"
  # 1. Wersja Envoy (przed restartem usługi ze starą konfiguracją)
  if [[ -n "$ENVOY_PRE_VERSION" && -n "$ENVOY_FINAL_VERSION" && "$ENVOY_PRE_VERSION" != "$ENVOY_FINAL_VERSION" ]]; then
    apt-mark unhold envoy >/dev/null 2>&1
    if apt-get install -y -q --allow-downgrades --no-install-recommends "envoy=${ENVOY_PRE_VERSION}" >>"$LOG_FILE" 2>&1; then
      ok "Przywrócono Envoy ${ENVOY_PRE_VERSION}"
    else
      warn "Nie udało się przywrócić pakietu envoy=${ENVOY_PRE_VERSION} (kopia binarki: ${TX_DIR}/envoy-binary-${ENVOY_PRE_VERSION})"
    fi
    apt-mark hold envoy >/dev/null 2>&1
  fi
  # 2. Pliki + VIP + firewall + usługi (Python, wspólny dziennik transakcji)
  local py_ok=0
  if [[ -x "${VENV_DIR}/bin/python3" ]] && "${VENV_DIR}/bin/python3" -c 'import yaml' >/dev/null 2>&1; then
    if OMNISSA_LB_LOG_FILE="$LOG_FILE" "${VENV_DIR}/bin/python3" "${APP_DIR}/omnissa-lb.py" \
         rollback --transaction "$TXID" >/dev/null; then
      py_ok=1
      ok "Rollback transakcji ${TXID} wykonany"
    fi
  fi
  (( py_ok )) || bash_rollback
  # 3. Środowisko Python
  if [[ -d "${VENV_DIR}.rollback-${TXID}" ]]; then
    rm -rf -- "$VENV_DIR" && mv -- "${VENV_DIR}.rollback-${TXID}" "$VENV_DIR" && ok "Przywrócono poprzednie środowisko venv"
  fi
  systemctl daemon-reload >/dev/null 2>&1
  if (( ${#PKGS_INSTALLED_NOW[@]} )); then
    info "Pakiety doinstalowane w tej sesji pozostawiono (nie są usuwane automatycznie): ${PKGS_INSTALLED_NOW[*]}"
  fi
}

bash_rollback() {
  # Awaryjny rollback bez Pythona: pliki z manifestu (odwrotna kolejność) i VIP.
  warn "Rollback awaryjny (Bash)"
  local manifest="${TX_DIR}/manifest.tsv" actions="${TX_DIR}/actions.tsv"
  if [[ -f "$manifest" ]]; then
    tac "$manifest" | while IFS=$'\t' read -r kind path existed rel; do
      [[ "$kind" == file ]] || continue
      if [[ "$existed" == 1 ]]; then
        cp -a -- "${TX_DIR}/${rel}" "$path" && info "przywrócono $path"
      else
        rm -f -- "$path" && info "usunięto nowy plik $path"
      fi
    done
  fi
  if [[ -f "$actions" ]]; then
    tac "$actions" | while IFS=$'\t' read -r act a1 a2 _; do
      case "$act" in
        vip_added) ip addr del "$a2" dev "$a1" 2>/dev/null && info "usunięto VIP $a2" ;;
        nft_applied) nft delete table inet omnissa_lb 2>/dev/null ;;
        ufw_rule_added) read -r -a spec <<<"$a1"; ufw delete "${spec[@]}" >/dev/null 2>&1 ;;
        unit_enabled) systemctl disable --now "$a1" >/dev/null 2>&1 ;;
      esac
    done
  fi
  systemctl daemon-reload >/dev/null 2>&1
  if grep -q $'^service_state\tenvoy.service\tactive' "$actions" 2>/dev/null; then
    systemctl restart envoy.service >/dev/null 2>&1
  else
    systemctl stop envoy.service >/dev/null 2>&1
  fi
}

write_report() {  # write_report STATUS KOD KOMUNIKAT
  local status="$1" code="$2" msg="$3"
  [[ -n "$REPORT_FILE" ]] || return 0
  mkdir -p "$(dirname "$REPORT_FILE")" 2>/dev/null || return 0
  R_STATUS="$status" R_CODE="$code" R_MSG="$msg" R_TX="$TXID" R_TXDIR="$TX_DIR" R_STEPS="$STEPS_FILE" \
  R_OS="$OS_PRETTY" R_CODENAME="$OS_CODENAME" R_ARCH="$ARCH" R_MODE="$MODE" R_DRY="$DRY_RUN" \
  R_ENVOY_PRE="$ENVOY_PRE_VERSION" R_ENVOY="$ENVOY_FINAL_VERSION" R_SUITE="$ENVOY_SUITE" R_FPR="$ENVOY_KEY_FPR" \
  R_PKGS="${PKGS_INSTALLED_NOW[*]:-}" R_MISSING="${PKGS_MISSING[*]:-}" R_LOG="$LOG_FILE" R_VER="$INSTALLER_VERSION" \
  /usr/bin/python3 - "$REPORT_FILE" <<'PYEOF' || true
import json, os, sys, datetime
e = os.environ.get
def load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None
steps = []
if e("R_STEPS") and os.path.exists(e("R_STEPS")):
    for line in open(e("R_STEPS"), encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        if len(p) >= 5:
            steps.append({"timestamp": p[0], "operation": p[1], "result": p[2], "code": int(p[3]) if p[3].isdigit() else p[3], "message": p[4]})
txdir = e("R_TXDIR") or ""
report = {
    "installer_version": e("R_VER"), "status": e("R_STATUS"), "exit_code": int(e("R_CODE") or 0),
    "message": e("R_MSG"), "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    "dry_run": e("R_DRY") == "1", "mode": e("R_MODE"), "transaction": e("R_TX"),
    "system": {"os": e("R_OS"), "codename": e("R_CODENAME"), "arch": e("R_ARCH")},
    "envoy": {"previous_version": e("R_ENVOY_PRE") or None, "installed_version": e("R_ENVOY") or None,
              "apt_suite": e("R_SUITE") or None, "repo_key_fingerprint": e("R_FPR") or None},
    "packages": {"installed_now": (e("R_PKGS") or "").split(), "missing_before": (e("R_MISSING") or "").split()},
    "steps": steps, "log_file": e("R_LOG"),
}
if txdir:
    report["configure"] = load(os.path.join(txdir, "configure-report.json"))
    report["tests"] = load(os.path.join(txdir, "verify.json"))
    report["tests_shell"] = load(os.path.join(txdir, "verify-sh.json"))
    report["rollback"] = load(os.path.join(txdir, "rollback.json"))
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
PYEOF
  chmod 0640 "$REPORT_FILE" 2>/dev/null || true
  if [[ "$DRY_RUN" != 1 ]]; then
    ln -sfn "$REPORT_FILE" "${LOG_DIR}/install-report.json" 2>/dev/null || true
  fi
}

# ================================================================== pomocnicze
usage() {
  cat <<EOF
Omnissa Envoy Load Balancer - instalator ${INSTALLER_VERSION}

Użycie: sudo bash install.sh [opcje]

  --unattended              tryb automatyczny (bez pytań)
  --config PLIK             plik settings.yaml (wymagany w trybie unattended przy pierwszej instalacji)
  --interface IFACE         interfejs dla VIP (nadpisuje network.interface)
  --dry-run                 pokaż plan, nie wprowadzaj żadnych zmian
  --mode TRYB               install | update | reconfigure | repair (domyślnie: wykrywany)
  --envoy-version X.Y[.Z]   przypnij wersję Envoy (nadpisuje envoy.version)
  --upgrade-envoy           zezwól na zmianę wersji już zainstalowanego Envoy
  --envoy-apt-suite SUITE   jawnie wskaż suite repozytorium Envoy (domyślnie codename Ubuntu)
  --envoy-key-fingerprint F wymagany odcisk klucza repozytorium Envoy
  --envoy-repo-url URL      wewnętrzny mirror apt.envoyproxy.io (HTTPS, ten sam klucz podpisu)
  --wheelhouse KATALOG      instalacja zależności Python offline (pip --no-index)
  --keep-on-failure         nie wykonuj rollbacku po błędzie (diagnostyka)
  -y, --yes                 akceptuj pytania potwierdzające (tryb interaktywny)
  -v, --verbose             szczegółowe logi
  -h, --help                pomoc

Kody wyjścia: 0 OK, 2 użycie, 3 system, 4 root, 5 sieć/repozytoria, 6 zależności,
7 Envoy, 8 Python, 9 konfiguracja, 10 wdrożenie, 11 testy krytyczne, 12 blokada, 13 przerwano.
EOF
}

parse_args() {
  while (( $# )); do
    case "$1" in
      --unattended) UNATTENDED=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --config) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; CONFIG="$2"; shift ;;
      --config=*) CONFIG="${1#*=}" ;;
      --interface) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; INTERFACE="$2"; shift ;;
      --interface=*) INTERFACE="${1#*=}" ;;
      --mode) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; MODE="$2"; shift ;;
      --mode=*) MODE="${1#*=}" ;;
      --envoy-version) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; ENVOY_VERSION_OPT="$2"; shift ;;
      --envoy-apt-suite) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; APT_SUITE_OPT="$2"; shift ;;
      --envoy-key-fingerprint) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; KEY_FPR_OPT="$2"; shift ;;
      --envoy-repo-url) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; ENVOY_REPO_URL="${2%/}"; shift ;;
      --wheelhouse) [[ $# -ge 2 ]] || { usage; exit "$E_USAGE"; }; WHEELHOUSE="$2"; shift ;;
      --upgrade-envoy) UPGRADE_ENVOY=1 ;;
      --keep-on-failure) KEEP_ON_FAILURE=1 ;;
      -y|--yes) ASSUME_YES=1 ;;
      -v|--verbose) VERBOSE=1 ;;
      --no-color) C_R="" C_G="" C_Y="" C_B="" C_D="" C_0="" ;;
      -h|--help) usage; exit 0 ;;
      *) echo "Nieznana opcja: $1" >&2; usage; exit "$E_USAGE" ;;
    esac
    shift
  done
  case "$MODE" in auto|install|update|reconfigure|repair) ;; *) echo "Nieprawidłowy --mode: $MODE" >&2; exit "$E_USAGE" ;; esac
  if [[ -n "$INTERFACE" && ! "$INTERFACE" =~ ^[A-Za-z0-9_.:@-]{1,15}$ ]]; then
    echo "Nieprawidłowa nazwa interfejsu: $INTERFACE" >&2; exit "$E_USAGE"
  fi
  if [[ -n "$ENVOY_VERSION_OPT" && ! "$ENVOY_VERSION_OPT" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
    echo "Nieprawidłowy --envoy-version (format X.Y lub X.Y.Z)" >&2; exit "$E_USAGE"
  fi
  if [[ -n "$APT_SUITE_OPT" && ! "$APT_SUITE_OPT" =~ ^[a-z0-9-]+$ ]]; then
    echo "Nieprawidłowy --envoy-apt-suite" >&2; exit "$E_USAGE"
  fi
  [[ "$ENVOY_REPO_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?$ ]] || {
    echo "--envoy-repo-url musi być adresem HTTPS" >&2; exit "$E_USAGE"; }
  ENVOY_KEY_URL="${ENVOY_REPO_URL}/signing.key"
  readonly ENVOY_REPO_URL ENVOY_KEY_URL
  if [[ -n "$CONFIG" ]]; then
    [[ -f "$CONFIG" ]] || { echo "Nie znaleziono pliku konfiguracji: $CONFIG" >&2; exit "$E_CONFIG"; }
    CONFIG="$(cd -- "$(dirname -- "$CONFIG")" && pwd -P)/$(basename -- "$CONFIG")"
  fi
  if [[ "$UNATTENDED" == 0 && ! -t 0 && "$DRY_RUN" == 0 ]]; then
    echo "Brak terminala - użyj --unattended --config <plik>" >&2; exit "$E_USAGE"
  fi
}

confirm() {  # confirm "pytanie" [domyślnie T|N]
  local q="$1" def="${2:-T}" ans
  [[ "$UNATTENDED" == 1 || "$ASSUME_YES" == 1 ]] && return 0
  read -r -p "  ${q} [$([[ $def == T ]] && echo T/n || echo t/N)]: " ans </dev/tty || return 1
  ans="${ans:-$def}"
  [[ "${ans,,}" =~ ^(t|tak|y|yes)$ ]]
}

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed$'; }

http_check() {  # http_check URL -> 0 gdy osiągalny
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --proto '=https,http' -m 15 -o /dev/null -r 0-0 ${APT_PROXY:+-x "$APT_PROXY"} "$url" 2>/dev/null
  else
    /usr/bin/python3 - "$url" <<'PYEOF' >/dev/null 2>&1
import sys, urllib.request
urllib.request.urlopen(urllib.request.Request(sys.argv[1], headers={"Range": "bytes=0-0"}), timeout=15)
PYEOF
  fi
}

http_get() {  # http_get URL PLIK
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --proto '=https' --tlsv1.2 -m 60 --retry 3 ${APT_PROXY:+-x "$APT_PROXY"} -o "$2" "$1"
  else
    /usr/bin/python3 - "$1" "$2" <<'PYEOF'
import sys, urllib.request
if not sys.argv[1].startswith("https://"): sys.exit(1)
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2], "wb") as f:
    f.write(r.read())
PYEOF
  fi
}

# ---------------------------------------------------------------- transakcja / pliki zarządzane
start_transaction() {
  TX_DIR="${BACKUP_ROOT}/${TXID}"
  install -d -m 0700 "$BACKUP_ROOT" "$TX_DIR" "${TX_DIR}/files"
  : >"${TX_DIR}/manifest.tsv"
  : >"${TX_DIR}/actions.tsv"
  printf '{"id": "%s", "created_at": "%s", "type": "transaction", "label": "install.sh %s"}\n' \
    "$TXID" "$(date -Iseconds)" "$MODE" >"${TX_DIR}/meta.json"
  local a e
  a="$(systemctl is-active envoy.service 2>/dev/null || true)"; e="$(systemctl is-enabled envoy.service 2>/dev/null || true)"
  tx_record service_state envoy.service "${a:-inactive}" "${e:-disabled}"
  TX_STARTED=1
  info "Transakcja ${TXID} (kopie: ${TX_DIR})"
}

tx_record() { local IFS=$'\t'; printf '%s\n' "$*" >>"${TX_DIR}/actions.tsv"; }

tx_backup() {  # zapis stanu pliku przed pierwszą modyfikacją
  local path="$1"
  grep -q -F -x -- "$(printf 'file\t%s' "$path")" <(cut -f1,2 "${TX_DIR}/manifest.tsv") && return 0
  if [[ -e "$path" || -L "$path" ]]; then
    install -d -m 0700 "${TX_DIR}/files$(dirname -- "$path")"
    cp -a -- "$path" "${TX_DIR}/files${path}"
    printf 'file\t%s\t1\tfiles%s\n' "$path" "$path" >>"${TX_DIR}/manifest.tsv"
  else
    printf 'file\t%s\t0\t\n' "$path" >>"${TX_DIR}/manifest.tsv"
  fi
}

# managed_install ŹRÓDŁO CEL TRYB [WŁAŚCICIEL:GRUPA] -> 0 = zmieniono, 1 = bez zmian
managed_install() {
  local src="$1" dst="$2" mode="$3" owner="${4:-root:root}"
  if [[ -f "$dst" ]] && cmp -s -- "$src" "$dst"; then
    chmod "$mode" "$dst"; chown "$owner" "$dst"
    return 1
  fi
  tx_backup "$dst"
  install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" -- "$src" "$dst"
  debug "zapisano $dst"
  return 0
}

# ================================================================== kroki instalacji
init_logging() {
  if [[ "$DRY_RUN" == 1 ]]; then
    WORK_DIR="$(mktemp -d /tmp/omnissa-lb-dryrun.XXXXXX)"
    LOG_FILE="/tmp/omnissa-lb-dryrun-${TS}.log"
    : >"$LOG_FILE"
    REPORT_FILE="/tmp/omnissa-lb-dryrun-report-${TS}.json"
  else
    install -d -m 0750 "$LOG_DIR"
    touch "$LOG_FILE"; chmod 0640 "$LOG_FILE"
    WORK_DIR="$(mktemp -d /tmp/omnissa-lb-install.XXXXXX)"
    REPORT_FILE="${LOG_DIR}/install-report-${TS}.json"
  fi
  STEPS_FILE="${WORK_DIR}/steps.tsv"
  : >"$STEPS_FILE"
  CURRENT_OP="start"
  info "Omnissa Envoy LB installer ${INSTALLER_VERSION} (tryb: $([[ $UNATTENDED == 1 ]] && echo unattended || echo interaktywny)$([[ $DRY_RUN == 1 ]] && echo ', DRY-RUN'))"
  info "Log: ${LOG_FILE}"
  local args_safe="unattended=$UNATTENDED dry_run=$DRY_RUN config=${CONFIG:-brak} interface=${INTERFACE:-z konfiguracji} mode=$MODE"
  record_step start ok 0 "$args_safe"
}

require_root() {
  CURRENT_OP="root"
  if [[ "$(id -u)" -ne 0 ]]; then
    if [[ "$DRY_RUN" == 1 ]]; then
      warn "Dry-run bez uprawnień root - część kontroli (netplan, firewall) może być niepełna"
      return 0
    fi
    die "$E_ROOT" "Instalator wymaga uprawnień root" "Uruchom: sudo bash install.sh ..."
  fi
  record_step root ok 0 "uprawnienia root"
}

acquire_lock() {
  [[ "$DRY_RUN" == 1 ]] && return 0
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "$E_LOCK" "Inna instancja instalatora jest uruchomiona (${LOCK_FILE})"
}

detect_os() {
  section os "Wykrywanie systemu"
  [[ -r /etc/os-release ]] || die "$E_OS" "Brak /etc/os-release - nieobsługiwany system"
  # shellcheck disable=SC1091
  OS_ID="$(. /etc/os-release && echo "${ID:-}")"
  OS_VERSION="$(. /etc/os-release && echo "${VERSION_ID:-}")"
  OS_CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}")"
  OS_PRETTY="$(. /etc/os-release && echo "${PRETTY_NAME:-}")"
  ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m)"
  info "System: ${OS_PRETTY} (codename ${OS_CODENAME}, architektura ${ARCH}, kernel $(uname -r))"
  [[ "$OS_ID" == ubuntu ]] || die "$E_OS" "Nieobsługiwana dystrybucja: ${OS_ID}. Wymagane Ubuntu Server ${SUPPORTED_VERSIONS// / lub }."
  [[ " $SUPPORTED_VERSIONS " == *" $OS_VERSION "* ]] || \
    die "$E_OS" "Nieobsługiwana wersja Ubuntu: ${OS_VERSION}. Obsługiwane: ${SUPPORTED_VERSIONS}."
  [[ " $SUPPORTED_ARCHS " == *" $ARCH "* ]] || \
    die "$E_OS" "Nieobsługiwana architektura: ${ARCH}. Obsługiwane: ${SUPPORTED_ARCHS}."
  [[ -n "$OS_CODENAME" ]] || die "$E_OS" "Nie można ustalić nazwy kodowej wydania Ubuntu"
  command -v systemctl >/dev/null || die "$E_OS" "Wymagany systemd"
  [[ -x /usr/bin/python3 ]] || die "$E_OS" "Brak /usr/bin/python3 (element instalacji Ubuntu Server)"
  record_step os ok 0 "${OS_PRETTY} ${ARCH}"
}

detect_apt_proxy() {
  APT_PROXY="$(apt-config dump 2>/dev/null | sed -n -E 's/^Acquire::https?::Proxy "([^"]+)";$/\1/p' | head -n1 || true)"
  [[ "$APT_PROXY" == DIRECT || "$APT_PROXY" == false ]] && APT_PROXY=""
  [[ -n "$APT_PROXY" ]] && info "Wykryto proxy APT - zostanie użyte także do testów łączności" || true
}

check_repositories() {
  section repos "Weryfikacja połączenia z repozytoriami"
  detect_apt_proxy
  local entries=() uri suite failed=0
  # Format deb822 (*.sources) i klasyczny (sources.list)
  while IFS=$'\t' read -r uri suite; do
    [[ -n "$uri" && -n "$suite" ]] && entries+=("${uri%/}/dists/${suite}/InRelease")
  done < <(
    {
      for f in /etc/apt/sources.list.d/*.sources; do
        [[ -f "$f" ]] || continue
        awk 'BEGIN{RS="";FS="\n"} !/Enabled:[[:space:]]*no/ {u="";s="";for(i=1;i<=NF;i++){if($i~/^URIs:/){split($i,a," ");u=a[2]} if($i~/^Suites:/){split($i,b," ");s=b[2]}} if(u!=""&&s!="")print u"\t"s}' "$f"
      done
      for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list; do
        [[ -f "$f" ]] || continue
        awk '/^deb[[:space:]]/ {sub(/\[[^]]*\][[:space:]]*/,""); print $2"\t"$3}' "$f"
      done
    } | { grep -v -F "$ENVOY_REPO_URL" || true; } | sort -u
  )
  if (( ${#entries[@]} == 0 )); then
    die "$E_NETWORK" "Nie znaleziono skonfigurowanych repozytoriów APT Ubuntu"
  fi
  for uri in "${entries[@]}"; do
    if http_check "$uri"; then ok "Repozytorium osiągalne: ${uri%/dists/*}"
    else warn "Repozytorium NIEOSIĄGALNE: $uri"; failed=$((failed + 1)); fi
  done
  (( failed < ${#entries[@]} )) || check_fail "$E_NETWORK" "Brak połączenia z repozytoriami APT Ubuntu" \
    "Sprawdź DNS, trasę domyślną i proxy (Acquire::http::Proxy)"
  record_step repos ok 0 "${#entries[@]} repozytoriów, niedostępne: $failed"
}

load_config_hints() {
  # Wersja Envoy / suite / odcisk klucza z settings.yaml (CLI ma pierwszeństwo)
  local cfg="${CONFIG:-}"
  [[ -z "$cfg" && -f "$SETTINGS" ]] && cfg="$SETTINGS"
  [[ -n "$cfg" ]] || return 0
  local out
  out="$(/usr/bin/python3 - "$cfg" <<'PYEOF' 2>/dev/null || true
import sys, json
try:
    import yaml
    d = yaml.safe_load(open(sys.argv[1])) or {}
except ImportError:
    try:
        d = json.load(open(sys.argv[1]))
    except Exception:
        d = {}
e = d.get("envoy") or {}
print(str(e.get("version") or ""), str(e.get("apt_suite") or ""), str(e.get("apt_key_fingerprint") or "").replace(" ", ""), sep="\t")
PYEOF
)"
  local v s f
  IFS=$'\t' read -r v s f <<<"$out"
  [[ -z "$ENVOY_VERSION_OPT" && "$v" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] && ENVOY_VERSION_OPT="$v"
  [[ -z "$APT_SUITE_OPT" && "$s" =~ ^[a-z0-9-]+$ ]] && APT_SUITE_OPT="$s"
  [[ -z "$KEY_FPR_OPT" && "$f" =~ ^[0-9A-Fa-f]{40}$ ]] && KEY_FPR_OPT="$f"
  return 0
}

check_envoy_repo() {
  section envoy-repo "Weryfikacja dostępności pakietu Envoy dla Ubuntu ${OS_VERSION}"
  ENVOY_SUITE="${APT_SUITE_OPT:-$OS_CODENAME}"
  if [[ -n "$APT_SUITE_OPT" && "$APT_SUITE_OPT" != "$OS_CODENAME" ]]; then
    warn "Jawnie wskazano suite '${APT_SUITE_OPT}' (inny niż ${OS_CODENAME}) - decyzja administratora (--envoy-apt-suite)"
  fi
  if ! http_check "${ENVOY_REPO_URL}/dists/${ENVOY_SUITE}/InRelease"; then
    check_fail "$E_ENVOY" "Oficjalne repozytorium Envoy (${ENVOY_REPO_URL}) nie publikuje wydania '${ENVOY_SUITE}' dla Ubuntu ${OS_VERSION} lub jest nieosiągalne." \
      "Instalacja przerwana bez zmian w systemie. Pakiety z innych wydań Ubuntu NIE są stosowane automatycznie. Sprawdź łączność z ${ENVOY_REPO_URL} lub - świadomie - wskaż suite: --envoy-apt-suite <suite>."
    ENVOY_TARGET_VERSION="(niedostępna)"
    return 0
  fi
  local pk="${WORK_DIR}/envoy-packages"
  if http_get "${ENVOY_REPO_URL}/dists/${ENVOY_SUITE}/main/binary-${ARCH}/Packages.gz" "${pk}.gz" 2>/dev/null; then
    gzip -dc "${pk}.gz" >"$pk" 2>/dev/null || : >"$pk"
  elif ! http_get "${ENVOY_REPO_URL}/dists/${ENVOY_SUITE}/main/binary-${ARCH}/Packages" "$pk" 2>/dev/null; then
    : >"$pk"
  fi
  local versions
  versions="$(awk '/^Package: envoy$/{p=1;next} /^Package:/{p=0} p && /^Version:/{print $2}' "$pk" | sort -V -u | tr '\n' ' ')"
  if [[ -z "${versions// /}" ]]; then
    check_fail "$E_ENVOY" "Repozytorium Envoy '${ENVOY_SUITE}' nie zawiera pakietu envoy dla architektury ${ARCH}." \
      "Obsługiwane są wyłącznie architektury, dla których dostępne są oficjalne pakiety."
    ENVOY_TARGET_VERSION="(niedostępna)"
    return 0
  fi
  info "Dostępne wersje Envoy (${ENVOY_SUITE}/${ARCH}): ${versions}"
  if [[ -n "$ENVOY_VERSION_OPT" ]]; then
    ENVOY_TARGET_VERSION="$(tr ' ' '\n' <<<"$versions" | grep -E "^${ENVOY_VERSION_OPT//./\\.}([.~+-]|$)" | sort -V | tail -n1 || true)"
    if [[ -z "$ENVOY_TARGET_VERSION" ]]; then
      check_fail "$E_ENVOY" "Przypięta wersja Envoy ${ENVOY_VERSION_OPT} nie jest dostępna w repozytorium" "Dostępne: ${versions}"
      ENVOY_TARGET_VERSION="(niedostępna)"
      return 0
    fi
    info "Przypięta wersja Envoy: ${ENVOY_VERSION_OPT} -> ${ENVOY_TARGET_VERSION}"
  else
    ENVOY_TARGET_VERSION="$(tr ' ' '\n' <<<"$versions" | sed '/^$/d' | sort -V | tail -n1)"
  fi
  record_step envoy-repo ok 0 "suite=${ENVOY_SUITE} arch=${ARCH} target=${ENVOY_TARGET_VERSION}"
}

detect_existing() {
  section existing "Wykrywanie istniejącej instalacji"
  if [[ -f "${APP_DIR}/.install-info" || -f "$SETTINGS" ]]; then
    EXISTING_INSTALL=1
    info "Wykryto istniejącą instalację: $(grep -h '^version=' "${APP_DIR}/.install-info" 2>/dev/null || echo 'wersja nieznana'), konfiguracja: $([[ -f $SETTINGS ]] && echo "$SETTINGS" || echo brak)"
  fi
  if pkg_installed envoy; then
    ENVOY_PRE_VERSION="$(dpkg-query -W -f='${Version}' envoy)"
    ENVOY_BIN="$(dpkg -L envoy 2>/dev/null | grep -E '/bin/envoy$' | head -n1 || echo /usr/bin/envoy)"
    info "Envoy zainstalowany z pakietu: ${ENVOY_PRE_VERSION} (${ENVOY_BIN})"
  elif command -v envoy >/dev/null 2>&1; then
    warn "Wykryto Envoy spoza menedżera pakietów: $(command -v envoy) ($(envoy --version 2>/dev/null | head -n1)). Zostanie pozostawiony; usługa użyje pakietu z oficjalnego repozytorium."
  fi
  if [[ "$MODE" == auto ]]; then
    if (( EXISTING_INSTALL )); then
      if [[ "$UNATTENDED" == 1 || "$DRY_RUN" == 1 ]]; then
        MODE=$([[ -n "$CONFIG" ]] && echo reconfigure || echo update)
      else
        echo
        echo "  Wykryto istniejącą instalację. Wybierz operację:"
        echo "    1) aktualizacja (zachowaj konfigurację, odśwież komponenty)"
        echo "    2) rekonfiguracja (kreator / nowy plik konfiguracji)"
        echo "    3) naprawa (ponowne zastosowanie bieżącej konfiguracji i testy)"
        echo "    4) przerwij"
        local choice; read -r -p "  Wybór [1]: " choice </dev/tty || choice=4
        case "${choice:-1}" in
          1) MODE=update ;; 2) MODE=reconfigure ;; 3) MODE=repair ;;
          *) CURRENT_OP=abort; info "Przerwano przez administratora"; exit "$E_ABORT" ;;
        esac
      fi
    else
      MODE=install
    fi
  fi
  info "Tryb operacji: ${MODE}"
  if [[ "$MODE" == install && -z "$CONFIG" && "$UNATTENDED" == 1 && ! -f "$SETTINGS" ]]; then
    die "$E_USAGE" "Tryb unattended wymaga --config <settings.yaml> przy pierwszej instalacji"
  fi
  if [[ "$MODE" == update || "$MODE" == repair ]] && [[ ! -f "$SETTINGS" && -z "$CONFIG" ]]; then
    die "$E_CONFIG" "Brak istniejącej konfiguracji ${SETTINGS} - użyj --mode install --config <plik>"
  fi
  record_step existing ok 0 "mode=${MODE} existing=${EXISTING_INSTALL} envoy=${ENVOY_PRE_VERSION:-brak}"
}

plan_dependencies() {
  section deps "Analiza zależności"
  PKGS_REQUIRED=(python3 python3-venv ca-certificates curl gnupg iproute2 iputils-ping iputils-arping
                 dnsutils netplan.io openssl jq)
  # Firewall: jeśli ufw jest aktywny - używamy ufw, w przeciwnym razie nftables
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    info "ufw aktywny - reguły firewall będą zarządzane przez ufw"
  else
    PKGS_REQUIRED+=(nftables)
  fi
  PKGS_MISSING=()
  local p
  for p in "${PKGS_REQUIRED[@]}"; do
    if pkg_installed "$p"; then debug "zainstalowany: $p"; else PKGS_MISSING+=("$p"); fi
  done
  info "Wymagane pakiety: ${PKGS_REQUIRED[*]}"
  info "Do instalacji: ${PKGS_MISSING[*]:-brak (wszystkie obecne)}"
  debug "python3-pip/wget nie są wymagane: pip pochodzi z venv (ensurepip), pobieranie realizuje curl"
  record_step deps ok 0 "brakujące: ${PKGS_MISSING[*]:-brak}"
}

print_dry_run_plan() {
  section dry-run "PLAN (DRY-RUN) - żadne zmiany nie zostaną wprowadzone"
  cat >&2 <<EOF

  Wykryty system:      ${OS_PRETTY} (${OS_CODENAME}, ${ARCH})
  Tryb operacji:       ${MODE}  (istniejąca instalacja: $([[ $EXISTING_INSTALL == 1 ]] && echo tak || echo nie))
  Wymagane zależności: ${PKGS_REQUIRED[*]}
  Brakujące pakiety:   ${PKGS_MISSING[*]:-brak}

  Planowane operacje APT:
    - apt-get update
$( (( ${#PKGS_MISSING[@]} )) && echo "    - apt-get install -y --no-install-recommends ${PKGS_MISSING[*]}" )
    - klucz repozytorium Envoy: ${ENVOY_KEY_URL} -> ${ENVOY_KEYRING} (weryfikacja odcisku${KEY_FPR_OPT:+: $KEY_FPR_OPT})
    - repozytorium: ${ENVOY_SOURCES} (deb822, ${ENVOY_REPO_URL} ${ENVOY_SUITE} main, Signed-By)
    - przypięcie: ${ENVOY_PREFS} (origin ${ENVOY_REPO_URL#https://}${ENVOY_VERSION_OPT:+, wersja ${ENVOY_VERSION_OPT}})
    - Envoy: $( if [[ -z "$ENVOY_PRE_VERSION" ]]; then echo "instalacja envoy=${ENVOY_TARGET_VERSION}";
                elif [[ -n "$ENVOY_VERSION_OPT" && "$ENVOY_PRE_VERSION" != "$ENVOY_TARGET_VERSION" ]]; then echo "zmiana ${ENVOY_PRE_VERSION} -> ${ENVOY_TARGET_VERSION} (wymaga --upgrade-envoy, backup binarki)";
                else echo "pozostaje ${ENVOY_PRE_VERSION}"; fi ); apt-mark hold envoy

  Środowisko Python:
    - ${VENV_DIR} ($([[ -x ${VENV_DIR}/bin/python3 ]] && echo "istnieje - aktualizacja wg requirements.txt" || echo "utworzenie"))
    - pip install -r requirements.txt: $(grep -Ev '^\s*(#|$)' "${SCRIPT_DIR}/requirements.txt" | tr '\n' ' ')

  Katalogi: ${APP_DIR} ${ETC_DIR} ${ENVOY_ETC} ${STATE_DIR} ${LOG_DIR} ${BACKUP_ROOT}
  Użytkownik systemowy: envoy ($(getent passwd envoy >/dev/null && echo istnieje || echo "do utworzenia, nologin"))

  Planowane operacje systemd:
    - ${SYSTEMD_DIR}/envoy.service (User=envoy, CAP_NET_BIND_SERVICE, ProtectSystem=strict, Restart=on-failure)
    - ${SYSTEMD_DIR}/envoy.service.d/10-omnissa-limits.conf (limity zasobów)
    - ${SYSTEMD_DIR}/omnissa-lb-firewall.service, omnissa-lb-healthcheck.{service,timer}
    - systemctl daemon-reload (tylko przy zmianie), enable envoy.service, start/restart tylko przy zmianie konfiguracji
EOF
}

run_python_plan() {
  local cfg="${CONFIG:-$SETTINGS}"
  if [[ ! -f "$cfg" ]]; then
    warn "Brak pliku konfiguracji - plan sieci/Envoy zostanie wyznaczony po uruchomieniu kreatora (tryb interaktywny)"
    return 0
  fi
  if ! "$(py)" -c 'import yaml' >/dev/null 2>&1; then
    warn "PyYAML niedostępny przed instalacją (brak python3-yaml) - pomijam szczegółowy plan konfiguracji"
    return 0
  fi
  local args=(plan --config "$cfg" --report "${WORK_DIR}/plan.json")
  [[ -n "$INTERFACE" ]] && args+=(--interface "$INTERFACE")
  if olb "${args[@]}"; then
    cp -f "${WORK_DIR}/plan.json" "${REPORT_FILE%.json}-plan.json" 2>/dev/null || true
    record_step plan ok 0 "plan konfiguracji wygenerowany"
  else
    record_step plan failed 1 "plan wykazał problemy"
    warn "Plan wykazał problemy (powyżej) - rzeczywista instalacja zostałaby przerwana"
    return 1
  fi
}

install_packages() {
  section apt "Instalacja zależności systemowych"
  run_step apt-update "apt-get update" apt-get update -q -o DPkg::Lock::Timeout=300 || \
    die "$E_DEPS" "apt-get update nie powiodło się" "Sprawdź ${LOG_FILE}"
  if (( ${#PKGS_MISSING[@]} )); then
    run_step apt-install "Instalacja: ${PKGS_MISSING[*]}" \
      apt-get install -y -q --no-install-recommends -o DPkg::Lock::Timeout=300 "${PKGS_MISSING[@]}" || \
      die "$E_DEPS" "Instalacja pakietów nie powiodła się: ${PKGS_MISSING[*]}"
    local p
    for p in "${PKGS_MISSING[@]}"; do
      pkg_installed "$p" && { PKGS_INSTALLED_NOW+=("$p"); tx_record package_installed "$p"; }
    done
  fi
  for p in "${PKGS_REQUIRED[@]}"; do
    pkg_installed "$p" || die "$E_DEPS" "Pakiet $p nie jest zainstalowany po operacji APT"
  done
}

setup_envoy_repo() {
  section envoy-repo "Konfiguracja zaufanego repozytorium Envoy"
  local key="${WORK_DIR}/envoy-signing.key" ring="${WORK_DIR}/envoy-keyring.gpg" fprs
  run_step envoy-key "Pobranie klucza podpisu repozytorium (HTTPS)" http_get "$ENVOY_KEY_URL" "$key" || \
    die "$E_ENVOY" "Nie można pobrać klucza repozytorium Envoy"
  fprs="$(gpg --batch --show-keys --with-colons "$key" 2>/dev/null | awk -F: '/^fpr:/{print $10}')"
  [[ -n "$fprs" ]] || die "$E_ENVOY" "Pobrany plik nie jest prawidłowym kluczem OpenPGP"
  ENVOY_KEY_FPR="$(head -n1 <<<"$fprs")"
  info "Odcisk klucza repozytorium Envoy: ${ENVOY_KEY_FPR}"
  if [[ -n "$KEY_FPR_OPT" ]]; then
    grep -qix -- "${KEY_FPR_OPT// /}" <<<"$fprs" || \
      die "$E_ENVOY" "Odcisk klucza repozytorium nie zgadza się z oczekiwanym (${KEY_FPR_OPT})" "Możliwa podmiana klucza - instalacja przerwana"
    ok "Odcisk klucza zgodny z przypiętą wartością"
  else
    warn "Odcisk klucza nie jest przypięty (envoy.apt_key_fingerprint) - zalecane przypięcie w środowisku produkcyjnym"
  fi
  gpg --batch --yes --dearmor -o "$ring" "$key"
  install -d -m 0755 /etc/apt/keyrings
  if [[ -f "$ENVOY_KEYRING" ]] && ! cmp -s "$ring" "$ENVOY_KEYRING"; then
    local old_fpr
    old_fpr="$(gpg --batch --show-keys --with-colons "$ENVOY_KEYRING" 2>/dev/null | awk -F: '/^fpr:/{print $10; exit}')"
    warn "Klucz repozytorium Envoy zmienił się (${old_fpr:-?} -> ${ENVOY_KEY_FPR}) - poprzedni zachowano w kopii transakcji"
    if [[ -z "$KEY_FPR_OPT" ]]; then
      if [[ "$UNATTENDED" == 1 ]]; then
        die "$E_ENVOY" "Zmiana klucza repozytorium Envoy w trybie unattended wymaga przypięcia odcisku" \
          "Zweryfikuj nowy klucz i podaj --envoy-key-fingerprint ${ENVOY_KEY_FPR} (lub envoy.apt_key_fingerprint)"
      fi
      confirm "Zaakceptować nowy klucz repozytorium (${ENVOY_KEY_FPR})?" N || die "$E_ABORT" "Odrzucono nowy klucz repozytorium"
    fi
  fi
  managed_install "$ring" "$ENVOY_KEYRING" 0644 && info "Zapisano ${ENVOY_KEYRING}" || debug "klucz bez zmian"

  # Bez duplikowania repozytoriów: jeśli inny plik już definiuje apt.envoyproxy.io - nie dodajemy drugiego
  local others
  others="$(grep -rlsF "$ENVOY_REPO_URL" /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null | grep -vxF "$ENVOY_SOURCES" || true)"
  if [[ -n "$others" ]]; then
    warn "Repozytorium Envoy jest już zdefiniowane w: ${others//$'\n'/, } - nie dodaję duplikatu ${ENVOY_SOURCES}"
  else
    cat >"${WORK_DIR}/envoy.sources" <<EOF
# Zarządzane przez omnissa-envoy-lb - oficjalne repozytorium Envoy Proxy
Types: deb
URIs: ${ENVOY_REPO_URL}
Suites: ${ENVOY_SUITE}
Components: main
Architectures: ${ARCH}
Signed-By: ${ENVOY_KEYRING}
EOF
    managed_install "${WORK_DIR}/envoy.sources" "$ENVOY_SOURCES" 0644 && info "Zapisano ${ENVOY_SOURCES}" || debug "repozytorium bez zmian"
  fi

  {
    echo "# Zarządzane przez omnissa-envoy-lb"
    echo "Package: envoy"
    echo "Pin: origin \"$(sed -E 's#^https://([^/:]+).*#\1#' <<<"$ENVOY_REPO_URL")\""
    echo "Pin-Priority: 600"
    if [[ -n "$ENVOY_VERSION_OPT" ]]; then
      echo
      echo "Package: envoy"
      echo "Pin: version ${ENVOY_TARGET_VERSION}"
      echo "Pin-Priority: 1001"
    fi
  } >"${WORK_DIR}/omnissa-envoy.pref"
  managed_install "${WORK_DIR}/omnissa-envoy.pref" "$ENVOY_PREFS" 0644 && info "Zapisano ${ENVOY_PREFS}" || true

  local out="${WORK_DIR}/apt-update-envoy.log"
  apt-get update -q -o DPkg::Lock::Timeout=300 >"$out" 2>&1 || true
  cat "$out" >>"$LOG_FILE"
  if grep -F "$ENVOY_REPO_URL" "$out" | grep -qiE "^(Err|E:|W:.*(GPG|NO_PUBKEY|not signed|invalid signature))"; then
    die "$E_ENVOY" "Weryfikacja podpisu repozytorium Envoy nie powiodła się" "Szczegóły w ${LOG_FILE}"
  fi
  apt-cache policy envoy 2>/dev/null | grep -qF "$ENVOY_REPO_URL" || \
    die "$E_ENVOY" "Pakiet envoy nie jest dostępny z podpisanego repozytorium po apt-get update"
  record_step envoy-repo ok 0 "repozytorium Envoy ${ENVOY_SUITE} (klucz ${ENVOY_KEY_FPR})"
}

install_envoy() {
  section envoy "Instalacja Envoy Proxy"
  local target="$ENVOY_TARGET_VERSION"
  if [[ -n "$ENVOY_PRE_VERSION" ]]; then
    if [[ "$ENVOY_PRE_VERSION" == "$target" || ( -z "$ENVOY_VERSION_OPT" && "$UPGRADE_ENVOY" == 0 ) ]]; then
      info "Envoy ${ENVOY_PRE_VERSION} zainstalowany - bez zmiany wersji$([[ -z $ENVOY_VERSION_OPT && $ENVOY_PRE_VERSION != "$target" ]] && echo " (dostępna ${target}; użyj --upgrade-envoy)")"
      ENVOY_FINAL_VERSION="$ENVOY_PRE_VERSION"
      apt-mark hold envoy >/dev/null
      record_step envoy ok 0 "envoy ${ENVOY_FINAL_VERSION} (bez zmian)"
      return 0
    fi
    if [[ "$UPGRADE_ENVOY" == 0 ]]; then
      if [[ "$UNATTENDED" == 1 ]] || ! confirm "Zmienić wersję Envoy ${ENVOY_PRE_VERSION} -> ${target}?" N; then
        die "$E_ENVOY" "Zainstalowany Envoy ${ENVOY_PRE_VERSION} różni się od wymaganego ${target}" \
          "Zmiana wersji wymaga --upgrade-envoy (wykonywany jest backup i kontrola wersji)"
      fi
    fi
    install -d -m 0700 "$TX_DIR"
    cp -a -- "$ENVOY_BIN" "${TX_DIR}/envoy-binary-${ENVOY_PRE_VERSION}"
    tx_record envoy_version_changed "$ENVOY_PRE_VERSION" "$target"
    info "Backup binarki Envoy ${ENVOY_PRE_VERSION}: ${TX_DIR}/envoy-binary-${ENVOY_PRE_VERSION}"
    apt-mark unhold envoy >/dev/null 2>&1 || true
  fi
  run_step envoy-install "apt-get install envoy=${target}" \
    apt-get install -y -q --no-install-recommends --allow-downgrades -o DPkg::Lock::Timeout=300 "envoy=${target}" || \
    die "$E_ENVOY" "Instalacja pakietu envoy=${target} nie powiodła się"
  [[ -z "$ENVOY_PRE_VERSION" ]] && { PKGS_INSTALLED_NOW+=(envoy); tx_record package_installed envoy; }
  apt-mark hold envoy >/dev/null
  ENVOY_FINAL_VERSION="$(dpkg-query -W -f='${Version}' envoy)"
  ENVOY_BIN="$(dpkg -L envoy | grep -E '/bin/envoy$' | head -n1)"
  [[ -x "$ENVOY_BIN" ]] || die "$E_ENVOY" "Brak binarki envoy po instalacji pakietu"
  run_step envoy-verify "Weryfikacja integralności plików pakietu (dpkg --verify)" dpkg --verify envoy || \
    die "$E_ENVOY" "Pliki pakietu envoy nie przeszły weryfikacji integralności"
  "$ENVOY_BIN" --version >>"$LOG_FILE" 2>&1 || die "$E_ENVOY" "Binarka envoy nie uruchamia się na tej platformie"
  ok "Envoy ${ENVOY_FINAL_VERSION} zainstalowany (${ENVOY_BIN}), pakiet wstrzymany (apt-mark hold)"
  record_step envoy ok 0 "envoy ${ENVOY_FINAL_VERSION}"
}

setup_user_and_dirs() {
  section dirs "Użytkownik systemowy i katalogi"
  if ! getent group envoy >/dev/null; then groupadd --system envoy; tx_record group_created envoy; fi
  if ! getent passwd envoy >/dev/null; then
    useradd --system --gid envoy --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin \
      --comment "Envoy Proxy (omnissa-envoy-lb)" envoy
    tx_record user_created envoy
    ok "Utworzono użytkownika systemowego envoy"
  fi
  install -d -m 0755 -o root -g root "$APP_DIR"
  install -d -m 0750 -o root -g root "$ETC_DIR" "$STATE_DIR"
  install -d -m 0750 -o root -g envoy "$ENVOY_ETC"
  install -d -m 0750 -o root -g root "$LOG_DIR"
  install -d -m 0700 -o root -g root "$BACKUP_ROOT"
  # Istniejące certyfikaty (jeśli są) nie są modyfikowane ani usuwane
  [[ -d "${ENVOY_ETC}/certs" ]] && info "Wykryto ${ENVOY_ETC}/certs - pozostawiono bez zmian"
  record_step dirs ok 0 "katalogi aplikacji"
}

deploy_app_files() {
  section app "Wdrożenie plików aplikacji do ${APP_DIR}"
  local f changed=0
  while IFS= read -r -d '' f; do
    local rel="${f#"${SCRIPT_DIR}"/}" mode=0644
    [[ "$rel" == *.sh || "$rel" == omnissa-lb.py ]] && mode=0755
    managed_install "$f" "${APP_DIR}/${rel}" "$mode" && changed=$((changed + 1))
  done < <(cd "$SCRIPT_DIR" && find "$SCRIPT_DIR" \
             \( -name venv -o -name __pycache__ -o -name .git -o -name tests \) -prune -o \
             -type f \( -name '*.py' -o -name '*.sh' -o -name '*.yaml' -o -name '*.template' -o -name '*.md' \
                        -o -name '*.service' -o -name '*.timer' -o -name requirements.txt \) -print0)
  printf '#!/bin/sh\n# Zarządzane przez omnissa-envoy-lb\nexec %s/bin/python3 %s/omnissa-lb.py "$@"\n' "$VENV_DIR" "$APP_DIR" \
    >"${WORK_DIR}/omnissa-lb"
  managed_install "${WORK_DIR}/omnissa-lb" "$CLI_WRAPPER" 0755 && changed=$((changed + 1))
  if [[ "$(readlink -f "$VERIFY_LINK" 2>/dev/null)" != "${APP_DIR}/scripts/verify-omnissa-lb.sh" ]]; then
    tx_backup "$VERIFY_LINK"; ln -sfn "${APP_DIR}/scripts/verify-omnissa-lb.sh" "$VERIFY_LINK"
  fi
  info "Pliki aplikacji: zmienionych ${changed}"
  record_step app ok 0 "zmienione pliki: ${changed}"
}

setup_venv() {
  section python "Środowisko Python (${VENV_DIR})"
  local sys_ver venv_ver stamp want
  sys_ver="$(/usr/bin/python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  venv_ver="$("${VENV_DIR}/bin/python3" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  if [[ -d "$VENV_DIR" && "$venv_ver" != "$sys_ver" ]]; then
    warn "venv utworzony dla Python ${venv_ver:-?}, system ma ${sys_ver} - odtwarzam środowisko"
    mv -- "$VENV_DIR" "${VENV_DIR}.rollback-${TXID}"
  fi
  if [[ ! -x "${VENV_DIR}/bin/python3" ]]; then
    run_step venv-create "python3 -m venv ${VENV_DIR}" /usr/bin/python3 -m venv "$VENV_DIR" || \
      die "$E_PYTHON" "Nie można utworzyć środowiska venv" "Wymagany pakiet python3-venv"
  fi
  want="$(cat "${APP_DIR}/requirements.txt" <(echo "$sys_ver") <(echo "${WHEELHOUSE}") | sha256sum | cut -d' ' -f1)"
  stamp="$(cat "${VENV_DIR}/.requirements.sha256" 2>/dev/null || true)"
  if [[ "$stamp" != "$want" ]] || ! "${VENV_DIR}/bin/python3" -c 'import yaml, jinja2' >/dev/null 2>&1; then
    local pip_args=(install --no-input --disable-pip-version-check --no-cache-dir --require-virtualenv
                    --only-binary=:all: -r "${APP_DIR}/requirements.txt")
    [[ -n "$WHEELHOUSE" ]] && pip_args+=(--no-index --find-links "$WHEELHOUSE")
    if ! run_step pip-install "pip install -r requirements.txt (venv)" "${VENV_DIR}/bin/python3" -m pip "${pip_args[@]}"; then
      # PyYAML/MarkupSafe mają wersje czysto-Pythonowe - ponowna próba z dopuszczeniem sdist
      local fallback=()
      for a in "${pip_args[@]}"; do [[ "$a" == --only-binary=:all: ]] || fallback+=("$a"); done
      run_step pip-install "pip install (dopuszczone pakiety źródłowe)" "${VENV_DIR}/bin/python3" -m pip "${fallback[@]}" || \
        die "$E_PYTHON" "Instalacja zależności Python nie powiodła się" "Sprawdź dostęp do PyPI/proxy lub użyj --wheelhouse"
    fi
    echo "$want" >"${VENV_DIR}/.requirements.sha256"
  else
    info "Zależności Python aktualne (requirements.txt bez zmian)"
  fi
  "${VENV_DIR}/bin/python3" -m pip check >>"$LOG_FILE" 2>&1 || die "$E_PYTHON" "pip check: niespójne zależności w venv"
  "${VENV_DIR}/bin/python3" -c 'import yaml, jinja2' || die "$E_PYTHON" "Biblioteki Python niedostępne w venv"
  ok "Środowisko Python gotowe ($("${VENV_DIR}/bin/python3" --version 2>&1))"
  record_step python ok 0 "venv $sys_ver"
}

install_systemd_units() {
  section systemd "Usługa systemd"
  local u src tmp
  for u in envoy.service omnissa-lb-firewall.service omnissa-lb-healthcheck.service; do
    src="${APP_DIR}/systemd/${u}"
    tmp="${WORK_DIR}/${u}"
    sed "s#/usr/bin/envoy#${ENVOY_BIN}#g" "$src" >"$tmp"
    if managed_install "$tmp" "${SYSTEMD_DIR}/${u}" 0644; then UNITS_CHANGED=1; info "Zapisano ${SYSTEMD_DIR}/${u}"; fi
  done
  if (( UNITS_CHANGED )); then
    if command -v systemd-analyze >/dev/null; then
      systemd-analyze verify "${SYSTEMD_DIR}/envoy.service" >>"$LOG_FILE" 2>&1 || warn "systemd-analyze verify zgłosił uwagi (szczegóły w logu)"
    fi
    run_step daemon-reload "systemctl daemon-reload" systemctl daemon-reload
  else
    info "Jednostki systemd bez zmian - brak daemon-reload"
  fi
  record_step systemd ok 0 "units_changed=${UNITS_CHANGED}"
}

prepare_settings() {
  section config "Konfiguracja (${SETTINGS})"
  local src=""
  if [[ -n "$CONFIG" ]]; then
    src="$CONFIG"
  elif [[ "$MODE" == install || "$MODE" == reconfigure ]] && [[ "$UNATTENDED" == 0 ]]; then
    src="${WORK_DIR}/settings.wizard.yaml"
    local base_args=()
    [[ -f "$SETTINGS" ]] && base_args=(--base "$SETTINGS")
    [[ -n "$INTERFACE" ]] && base_args+=(--interface "$INTERFACE")
    olb wizard --output "$src" "${base_args[@]}" </dev/tty || die "$E_ABORT" "Kreator konfiguracji przerwany"
  elif [[ -f "$SETTINGS" ]]; then
    info "Zachowuję istniejącą konfigurację ${SETTINGS}"
  else
    die "$E_CONFIG" "Brak konfiguracji - podaj --config <plik>"
  fi
  if [[ -n "$src" ]]; then
    local staged="${WORK_DIR}/settings.yaml"
    cp -- "$src" "$staged"
    if [[ -n "$INTERFACE" ]]; then
      "${VENV_DIR}/bin/python3" - "$staged" "$INTERFACE" <<'PYEOF'
import sys, yaml
p, iface = sys.argv[1], sys.argv[2]
d = yaml.safe_load(open(p)) or {}
d.setdefault("network", {})["interface"] = iface
open(p, "w").write("# Zarządzane przez omnissa-envoy-lb (interfejs ustawiony opcją --interface)\n"
                   + yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
PYEOF
      info "Ustawiono network.interface=${INTERFACE} (opcja --interface)"
    fi
    run_step config-validate "Walidacja konfiguracji" olb validate --config "$staged" || \
      die "$E_CONFIG" "Konfiguracja jest nieprawidłowa" "Szczegóły powyżej / w ${LOG_FILE}"
    if managed_install "$staged" "$SETTINGS" 0640; then
      info "Zainstalowano konfigurację ${SETTINGS} (poprzednia wersja w kopii transakcji)"
    else
      info "Konfiguracja bez zmian"
    fi
  else
    run_step config-validate "Walidacja konfiguracji" olb validate --config "$SETTINGS" || \
      die "$E_CONFIG" "Istniejąca konfiguracja jest nieprawidłowa"
  fi
  record_step config ok 0 "$SETTINGS"
}

run_configurator() {
  section configure "Konfigurator Python (VIP, Envoy, firewall, monitoring)"
  local args=(configure --unattended --config "$SETTINGS" --transaction "$TXID" --report "${TX_DIR}/configure-report.json")
  [[ "$VERBOSE" == 1 ]] && args=(-v "${args[@]}")
  if ! OMNISSA_LB_ENVOY_BIN="$ENVOY_BIN" olb "${args[@]}"; then
    die "$E_DEPLOY" "Konfigurator zakończył się błędem" "Szczegóły powyżej i w ${LOG_FILE}"
  fi
  record_step configure ok 0 "konfiguracja zastosowana"
}

run_tests() {
  section tests "Testy końcowe"
  local rc=0
  OMNISSA_LB_ENVOY_BIN="$ENVOY_BIN" olb verify --config "$SETTINGS" --report "${TX_DIR}/verify.json" || rc=$?
  if (( rc != 0 )); then
    die "$E_TESTS" "Testy krytyczne nie przeszły - wdrożenie NIE jest oznaczone jako udane" \
      "Raport testów: ${TX_DIR}/verify.json"
  fi
  rc=0
  bash "${APP_DIR}/scripts/verify-omnissa-lb.sh" --quiet --json "${TX_DIR}/verify-sh.json" --config "$SETTINGS" || rc=$?
  if (( rc != 0 )); then
    die "$E_TESTS" "Niezależna weryfikacja (verify-omnissa-lb.sh) wykazała błędy krytyczne" "Raport: ${TX_DIR}/verify-sh.json"
  fi
  record_step tests ok 0 "testy krytyczne zaliczone"
}

finalize() {
  section finalize "Zakończenie"
  {
    echo "version=${INSTALLER_VERSION}"
    echo "installed_at=$(date -Iseconds)"
    echo "transaction=${TXID}"
    echo "envoy=${ENVOY_FINAL_VERSION}"
    echo "os=${OS_VERSION}"
  } >"${WORK_DIR}/install-info"
  managed_install "${WORK_DIR}/install-info" "${APP_DIR}/.install-info" 0644 || true
  # Pakiety doinstalowane przez instalator (uninstall.sh usuwa wyłącznie te, na żądanie)
  if (( ${#PKGS_INSTALLED_NOW[@]} )); then
    printf '%s\n' "${PKGS_INSTALLED_NOW[@]}" | cat - "${STATE_DIR}/installed-packages.txt" 2>/dev/null | sort -u \
      >"${WORK_DIR}/installed-packages.txt"
    install -m 0640 "${WORK_DIR}/installed-packages.txt" "${STATE_DIR}/installed-packages.txt"
  fi
  if grep -q $'^user_created\tenvoy' "${TX_DIR}/actions.tsv" 2>/dev/null; then
    echo envoy >>"${STATE_DIR}/created-users.txt"
  fi
  [[ -d "${VENV_DIR}.rollback-${TXID}" ]] && rm -rf -- "${VENV_DIR}.rollback-${TXID}"
  write_report success 0 "Instalacja zakończona powodzeniem"
  cp -f "$REPORT_FILE" "${TX_DIR}/install-report.json" 2>/dev/null || true
  local summary
  summary="$(olb summary --config "$SETTINGS" --format shell 2>/dev/null || true)"
  echo >&2
  ok "Wdrożenie zakończone powodzeniem - wszystkie testy krytyczne zaliczone."
  printf '%s\n' "$summary" | sed 's/^/      /' >&2
  info "Raport JSON: ${REPORT_FILE} (link: ${LOG_DIR}/install-report.json)"
  info "Status: omnissa-lb status | Testy: verify-omnissa-lb | Logi: journalctl -u envoy -f"
}

# ================================================================== main
main() {
  parse_args "$@"
  init_logging
  require_root
  acquire_lock
  detect_os
  check_repositories
  load_config_hints
  check_envoy_repo
  detect_existing
  plan_dependencies

  if [[ "$DRY_RUN" == 1 ]]; then
    print_dry_run_plan
    local prc=0
    run_python_plan || prc=$?
    if (( ${#DRYRUN_ISSUES[@]} )); then
      prc=$E_GENERIC
      warn "Dry-run wykrył problemy, które przerwałyby instalację:"
      local i; for i in "${DRYRUN_ISSUES[@]}"; do warn "  - $i"; done
    fi
    write_report "$([[ $prc == 0 ]] && echo dry-run || echo dry-run-issues)" "$prc" "Dry-run: brak zmian w systemie"
    info "Raport dry-run: ${REPORT_FILE}"
    info "DRY-RUN zakończony - nie wprowadzono żadnych zmian w systemie."
    exit "$prc"
  fi

  if [[ "$UNATTENDED" == 0 ]]; then
    confirm "Kontynuować ${MODE} na ${OS_PRETTY}?" T || { info "Przerwano"; exit "$E_ABORT"; }
  fi
  start_transaction
  install_packages
  setup_envoy_repo
  install_envoy
  setup_user_and_dirs
  deploy_app_files
  setup_venv
  install_systemd_units
  prepare_settings
  run_configurator
  run_tests
  finalize
}

main "$@"
