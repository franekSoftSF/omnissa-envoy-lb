#!/usr/bin/env bash
# =============================================================================
#  verify-omnissa-lb.sh - niezależna weryfikacja wdrożenia Omnissa Envoy LB
#  (narzędzia systemowe: systemctl, ss, ip, curl, jq, openssl, dig, nft/ufw)
#
#  verify-omnissa-lb [--config PLIK] [--json RAPORT.json] [--quiet]
#  Kod wyjścia: 0 - testy krytyczne OK, 11 - błąd krytyczny, 2 - błąd użycia
# =============================================================================
set -Eeuo pipefail
export LC_ALL=C.UTF-8

CONFIG=/etc/omnissa-lb/settings.yaml
JSON_OUT=""
QUIET=0
OLB=/usr/local/sbin/omnissa-lb
ENVOY_BIN="$(command -v envoy || echo /usr/bin/envoy)"

while (( $# )); do
  case "$1" in
    --config) CONFIG="$2"; shift ;;
    --json) JSON_OUT="$2"; shift ;;
    --quiet) QUIET=1 ;;
    -h|--help) sed -n '3,8p' "$0"; exit 0 ;;
    *) echo "Nieznana opcja: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ -x "$OLB" ]] || OLB="$(dirname "$(readlink -f "$0")")/../omnissa-lb.py"
command -v jq >/dev/null || { echo "Wymagany jq" >&2; exit 2; }

summary="$("$OLB" summary --config "$CONFIG" --format shell)" || { echo "Nie można odczytać konfiguracji $CONFIG" >&2; exit 2; }
eval "$summary"   # wartości cytowane przez shlex.quote w omnissa-lb.py

RESULTS="$(mktemp)"
trap 'rm -f "$RESULTS"' EXIT
FAILED=0

check() {  # check NAZWA KRYTYCZNY(1/0) WYNIK(0=OK) KOMUNIKAT
  local name="$1" crit="$2" rc="$3" msg="$4" status
  if (( rc == 0 )); then status=pass
  elif (( crit )); then status=fail; FAILED=$((FAILED + 1))
  else status=warn; fi
  if (( ! QUIET )) || [[ "$status" != pass ]]; then
    printf '  [%-4s] %-32s %s\n' "${status^^}" "$name" "$msg"
  fi
  jq -cn --arg c "$name" --arg s "$status" --argjson k "$( ((crit)) && echo true || echo false )" --arg m "$msg" \
    '{check:$c,status:$s,critical:$k,message:$m}' >>"$RESULTS"
}

t() { "$@" >/dev/null 2>&1 && echo 0 || echo 1; }
crit_of() { [[ "$1" == critical ]] && echo 1 || echo 0; }

(( QUIET )) || echo "== Weryfikacja Omnissa Envoy LB =="

# --- usługa systemd
check systemd:active 1 "$(t systemctl is-active --quiet envoy.service)" "envoy.service aktywna"
check systemd:enabled 1 "$(t systemctl is-enabled --quiet envoy.service)" "autostart po restarcie systemu"
restart="$(systemctl show envoy.service -p Restart --value 2>/dev/null || true)"
check systemd:restart-policy 1 "$([[ "$restart" == on-failure || "$restart" == always ]] && echo 0 || echo 1)" "Restart=${restart:-?}"
pid="$(systemctl show envoy.service -p MainPID --value 2>/dev/null || echo 0)"
user="$( [[ "$pid" != 0 ]] && ps -o user= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
check systemd:dedicated-user 1 "$([[ "$user" == envoy ]] && echo 0 || echo 1)" "proces Envoy działa jako '${user:-?}'"
nnp="$(systemctl show envoy.service -p NoNewPrivileges --value 2>/dev/null || true)"
check systemd:hardening 1 "$([[ "$nnp" == yes ]] && echo 0 || echo 1)" "NoNewPrivileges=${nnp:-?}"
check journald 0 "$(t journalctl -u envoy.service -n 1 --no-pager)" "logi dostępne w journald"

# --- Envoy
check envoy:config-valid 1 "$(t "$ENVOY_BIN" --mode validate -c /etc/envoy/envoy.yaml)" "envoy --mode validate"
ready="$(curl -fsS -m 3 "http://${OLB_ADMIN}/ready" 2>/dev/null || true)"
check envoy:ready 1 "$([[ "$ready" == LIVE* ]] && echo 0 || echo 1)" "admin /ready=${ready:-brak}"
admin_port="${OLB_ADMIN##*:}"
admin_bind="$(ss -Hlnt "sport = :${admin_port}" 2>/dev/null | awk '{print $4}' | sort -u | tr '\n' ' ')"
check envoy:admin-loopback 1 "$([[ -n "$admin_bind" && ! "$admin_bind" =~ (0\.0\.0\.0|\*|\[::\]) ]] && echo 0 || echo 1)" "admin nasłuchuje na: ${admin_bind:-?}"

# --- VIP i listenery
for svc in $OLB_SERVICES; do
  U="${svc^^}"
  vip_var="OLB_${U}_VIP"; fqdn_var="OLB_${U}_FQDN"; tcp_var="OLB_${U}_TCP_PORTS"; udp_var="OLB_${U}_UDP_PORTS"
  vip="${!vip_var}"; fqdn="${!fqdn_var}"
  count="$(ip -4 -o addr show 2>/dev/null | awk -v v="$vip" '{split($4,a,"/"); if (a[1]==v) print $2}' | wc -l)"
  on_if="$(ip -4 -o addr show dev "$OLB_INTERFACE" 2>/dev/null | awk -v v="$vip" '{split($4,a,"/"); if (a[1]==v) print "yes"}')"
  check "vip:${svc}" 1 "$([[ "$on_if" == yes ]] && echo 0 || echo 1)" "VIP ${vip} na ${OLB_INTERFACE}"
  check "vip:${svc}:unique" 1 "$([[ "$count" -le 1 ]] && echo 0 || echo 1)" "VIP ${vip} skonfigurowany ${count}x (bez duplikatów)"
  for port in ${!tcp_var}; do
    check "listen:${svc}:tcp/${port}" 1 "$(ss -Hlnt "sport = :${port}" | awk -v a="${vip}:${port}" '$4 == a {f=1} END {exit !f}' && echo 0 || echo 1)" "TCP ${vip}:${port}"
    if [[ "$OLB_SNI_CHECK" != skip ]]; then
      probe="$fqdn"; [[ "$probe" == \*.* ]] && probe="probe${probe#\*}"
      out="$(timeout 8 openssl s_client -connect "${vip}:${port}" -servername "$probe" -brief </dev/null 2>&1 || true)"
      check "sni:${svc}:${port}" "$(crit_of "$OLB_SNI_CHECK")" "$(grep -q 'CONNECTION ESTABLISHED' <<<"$out" && echo 0 || echo 1)" \
        "TLS SNI ${probe} -> ${vip}:${port} ($(grep -m1 -E 'Peer certificate|Verification' <<<"$out" | sed 's/^ *//' || echo 'brak uzgodnienia TLS'))"
    fi
  done
  for port in ${!udp_var}; do
    check "listen:${svc}:udp/${port}" 1 "$(ss -Hlnu "sport = :${port}" | awk -v a="${vip}:${port}" '$4 == a {f=1} END {exit !f}' && echo 0 || echo 1)" "UDP ${vip}:${port}"
  done
  if command -v dig >/dev/null; then
    res="$(dig +short +time=2 +tries=1 "$fqdn" A 2>/dev/null | tr '\n' ' ')"
    check "dns:${svc}" 0 "$(grep -qw -- "$vip" <<<"$res" && echo 0 || echo 1)" "${fqdn} -> ${res:-brak rekordu}"
  fi
done

# --- backendy (aktywne health checki Envoy)
if [[ "$OLB_BACKEND_CHECK" != skip ]]; then
  clusters_json="$(curl -fsS -m 5 "http://${OLB_ADMIN}/clusters?format=json" 2>/dev/null || echo '{}')"
  while IFS=$'\t' read -r cname healthy total; do
    [[ -n "$cname" ]] || continue
    check "backend:${cname}" "$(crit_of "$OLB_BACKEND_CHECK")" "$([[ "$healthy" -gt 0 ]] && echo 0 || echo 1)" "zdrowe ${healthy}/${total}"
  done < <(jq -r '.cluster_statuses[]? | select(.name != "envoy_admin") |
      [ .name,
        ([.host_statuses[]? | select((.health_status.failed_active_health_check // false | not)
                                     and (.health_status.failed_outlier_check // false | not)
                                     and ((.health_status.eds_health_status // "HEALTHY") == "HEALTHY"))] | length),
        ([.host_statuses[]?] | length) ] | @tsv' <<<"$clusters_json")
fi

# --- firewall
case "$OLB_FIREWALL" in
  nftables) check firewall:nftables 1 "$(t nft list table inet omnissa_lb)" "tabela inet omnissa_lb załadowana"
            check firewall:persistent 1 "$(t systemctl is-enabled --quiet omnissa-lb-firewall.service)" "omnissa-lb-firewall.service włączona" ;;
  ufw) ufw_out="$(ufw status 2>/dev/null || true)"
       for svc in $OLB_SERVICES; do
         vip_var="OLB_${svc^^}_VIP"
         check "firewall:ufw:${svc}" 1 "$(grep -qF -- "${!vip_var}" <<<"$ufw_out" && echo 0 || echo 1)" "reguły ufw dla ${!vip_var} (ufw $(head -n1 <<<"$ufw_out"))"
       done ;;
  *) check firewall 0 0 "zarządzanie firewall wyłączone" ;;
esac

if [[ -n "$JSON_OUT" ]]; then
  jq -s --arg ts "$(date -Iseconds)" --argjson failed "$FAILED" \
    '{timestamp:$ts, success:($failed==0), critical_failures:$failed, checks:.}' "$RESULTS" >"$JSON_OUT"
  chmod 0640 "$JSON_OUT"
fi
total="$(wc -l <"$RESULTS")"
if (( FAILED )); then
  echo "WYNIK: ${FAILED} krytycznych błędów (z ${total} testów)" >&2
  exit 11
fi
(( QUIET )) || echo "WYNIK: OK (${total} testów, brak błędów krytycznych)"
exit 0
