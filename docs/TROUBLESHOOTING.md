# Rozwiązywanie problemów

## Pierwsze kroki

```bash
omnissa-lb status
```

```bash
verify-omnissa-lb
```

```bash
journalctl -u envoy -n 200 --no-pager
```

```bash
sudo omnissa-lb diagnostics
```

Raport ostatniej instalacji: `/var/log/omnissa-lb/install-report.json`. Szczegóły
nieudanej transakcji: `/var/backups/omnissa-lb/tx-*/diagnostics/`.

## Typowe problemy

| Objaw (komunikat) | Przyczyna / rozwiązanie |
|---|---|
| `Nieobsługiwana wersja Ubuntu` | wspierane są tylko 24.04 i 26.04 |
| `repozytorium Envoy ... nie publikuje wydania '<codename>'` | brak oficjalnego pakietu dla tego wydania lub brak łączności z `apt.envoyproxy.io`. Instalacja kończy się bez zmian. Sprawdź proxy/firewall. Inne suite tylko świadomie: `--envoy-apt-suite` |
| `Odcisk klucza repozytorium nie zgadza się` | możliwa podmiana klucza. Zweryfikuj odcisk w dokumentacji Envoy |
| `Zainstalowany Envoy X różni się od wymaganego Y` | dodaj `--upgrade-envoy` (wykonywany jest backup binarki) |
| `Adres VIP ... odpowiada w sieci` | konflikt IP. Wybierz wolny adres |
| `VIP ... nie należy do podsieci interfejsu` | ustaw `vip.prefix` albo użyj adresu z podsieci |
| `Porty wymagane przez Envoy są zajęte` | inna usługa (np. nginx) nasłuchuje na 443. Zatrzymaj ją lub zmień porty |
| `Scalona konfiguracja Netplan utraciłaby adresy` | nietypowy układ Netplan. VIP nie został zapisany. Sprawdź `netplan get` |
| `Envoy odrzucił wygenerowaną konfigurację` | poprzednia konfiguracja działa dalej. Szczegóły w logu |
| `Testy krytyczne nie przeszły` (kod 11) | zobacz `verify.json`. Zwykle niedostępne backendy lub SNI. Dla instalacji przed uruchomieniem UAG ustaw `validation.backend_check: warning` |
| `backend:...: zdrowe 0/N` | sprawdź routing i firewall LB → UAG oraz `health_check` (typ, port, ścieżka) |
| `sni:...` błąd | TLS do backendu przez VIP nie dochodzi do skutku. Sprawdź FQDN i certyfikat na UAG |
| `Inna instancja instalatora jest uruchomiona` | poczekaj lub usuń proces trzymający `/run/lock/omnissa-lb-install.lock` |

## Ręczny rollback

```bash
sudo omnissa-lb backup list
```

```bash
sudo omnissa-lb rollback --transaction tx-20261030-120000-1234
```

```bash
sudo omnissa-lb rollback --backup latest
```

## Weryfikacja ruchu

```bash
openssl s_client -connect 10.0.0.50:443 -servername tunnel.example.com -brief </dev/null
```

```bash
curl -s 127.0.0.1:9901/clusters | grep health_flags
```

```bash
curl -s 127.0.0.1:9901/stats | grep -E 'downstream_cx_active|upstream_cx_connect_fail'
```

## Netplan rollback (tryb netplan-apply)

Jeśli po zmianie sieci zostanie zerwana sesja SSH, nie rób nic. Po `confirm_timeout`
sekundach timer `omnissa-lb-netplan-rollback` sam przywróci poprzednią konfigurację.
Stan timera:

```bash
systemctl list-timers 'omnissa-lb-netplan-rollback*'
```
