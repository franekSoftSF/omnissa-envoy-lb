# Architektura

```
                    tunnel.example.com ─┐            ┌─> UAG-1 :443 (Tunnel)
 Klienci ── VIP 10.0.0.50:443/tcp ──────┼─ Envoy ────┼─> UAG-2 :443 (Tunnel)
                    seg.example.com ────┘   SNI      ├─> SEG-1 :443
            VIP 10.0.0.50:443/udp ─── udp_proxy ─────└─> SEG-2 :443
                                     (tylko Tunnel)
```

## Dwie warstwy

| Warstwa | Plik | Zakres |
|---|---|---|
| 1 - Bash | `install.sh`, `update.sh`, `uninstall.sh` | system, root, repozytoria, APT, Envoy, venv, katalogi, systemd, uruchomienie konfiguratora i testów, rollback, raport |
| 2 - Python | `omnissa-lb.py`, `modules/` | konfiguracja interaktywna/unattended, VIP, Netplan, Envoy, SNI, TCP/UDP, health checki, firewall, monitoring, backup/rollback, diagnostyka |

## Moduły

| Moduł | Odpowiedzialność |
|---|---|
| `settings.py` | wartości domyślne, walidacja całej konfiguracji (wszystkie błędy naraz) |
| `wizard.py` | interaktywny kreator `settings.yaml` |
| `system_checks.py` | informacje o systemie, preflight (interfejs, VIP, konflikty portów) |
| `network_manager.py` | interfejsy, adres główny, brama, Netplan (walidacja scalenia, dead-man switch) |
| `vip_manager.py` | idempotentne VIP na jednym interfejsie, wykrywanie konfliktu IP |
| `dns_manager.py` | weryfikacja rekordów A dla FQDN Tunnel/SEG |
| `envoy_config.py` | model listenerów/klastrów, render szablonu, `envoy --mode validate`, usługa |
| `firewall.py` | nftables (tabela `inet omnissa_lb`) lub ufw |
| `healthcheck.py` | testy krytyczne/ostrzegawcze, monitoring okresowy |
| `diagnostics.py` | pakiet diagnostyczny bez sekretów |
| `backup.py` | transakcje (manifest wspólny z Bash), snapshoty, rollback, retencja |
| `installer.py` | orkiestracja wdrożenia po stronie Python |

## Model konfiguracji Envoy

* Listenery są grupowane po `(VIP, protokół, port)`. Wspólny VIP daje jeden listener TCP
  z `tls_inspector` i osobnym `filter_chain` dla każdego FQDN (`server_names`).
* Nieznane SNI jest domyślnie odrzucane (brak łańcucha domyślnego). Opcja
  `sni.unknown_sni_action` może kierować taki ruch do Tunnel albo SEG.
* UDP nie przenosi SNI, więc dany `VIP:port/udp` może należeć do jednej usługi
  (walidacja to wymusza).
* `session_affinity: source_ip` przełącza klaster na `RING_HASH` z `hash_policy source_ip`.
* Health checki: TCP (connect), HTTP lub HTTPS (`transport_socket_matches`, TLS tylko dla
  health checka, ruch pozostaje passthrough). Klastry UDP sprawdzane są portem TCP usługi.
* `freebind: true` pozwala uruchomić listenery nawet wtedy, gdy VIP pojawi się chwilę
  później (np. przy starcie systemu).
* Admin API tylko na `127.0.0.1:9901`. Prometheus jest opcjonalny i udostępnia wyłącznie
  `/stats/prometheus`.

## Transakcje i rollback

Każde uruchomienie instalatora to transakcja `tx-<data>-<pid>` w `/var/backups/omnissa-lb/`:

* `manifest.tsv`: każdy plik zmieniony przez Bash lub Python (stan sprzed zmiany),
* `actions.tsv`: zmiany runtime (VIP, reguły firewall, włączone jednostki, stan usług, zmiana wersji Envoy),
* `configure-report.json`, `verify.json`, `verify-sh.json`, `diagnostics/`.

Po błędzie kolejność jest taka: diagnostyka, przywrócenie wersji Envoy, przywrócenie plików
(w odwrotnej kolejności), usunięcie dodanych VIP i reguł, restart Envoy z poprzednią
działającą konfiguracją (albo zatrzymanie, jeśli wcześniej nie działał), przywrócenie
venv, kod błędu. Pakiety zainstalowane przed operacją nie są usuwane.
