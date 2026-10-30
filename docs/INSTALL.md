# Instalacja

## Wymagania

* Ubuntu Server **24.04 LTS** lub **26.04 LTS**, architektura **amd64** lub **arm64**
  (wyłącznie tam, gdzie oficjalne repozytorium Envoy publikuje pakiety).
* Uprawnienia root (`sudo`).
* Dostęp do repozytoriów Ubuntu, `https://apt.envoyproxy.io` (lub wewnętrznego mirrora)
  oraz PyPI (lub katalogu wheelhouse przy instalacji offline).
* Jeden skonfigurowany interfejs z adresem IPv4 (zalecany adres statyczny) oraz wolny adres
  (lub dwa) na VIP w tej samej podsieci.

Administrator nie instaluje niczego ręcznie. Wszystko robi `install.sh`.

## Tryby uruchomienia

```bash
sudo bash install.sh
```

```bash
sudo bash install.sh --unattended --config ./config/settings.yaml
```

```bash
sudo bash install.sh --unattended --config ./config/settings.yaml --dry-run
```

```bash
sudo bash install.sh --unattended --interface ens160 --config ./config/settings.yaml
```

| Opcja | Znaczenie |
|---|---|
| `--unattended` | bez pytań; przy pierwszej instalacji wymaga `--config` |
| `--config PLIK` | plik ustawień (kopiowany do `/etc/omnissa-lb/settings.yaml`, poprzedni trafia do kopii zapasowej) |
| `--interface IFACE` | interfejs VIP (zapisywany w zainstalowanej konfiguracji) |
| `--dry-run` | pełny plan bez żadnych zmian w systemie |
| `--mode install\|update\|reconfigure\|repair` | wymuszenie trybu (domyślnie wykrywany) |
| `--envoy-version X.Y[.Z]` | przypięcie wersji Envoy (też `envoy.version`) |
| `--upgrade-envoy` | zgoda na zmianę wersji zainstalowanego Envoy |
| `--envoy-key-fingerprint FPR` | wymagany odcisk klucza repozytorium (też `envoy.apt_key_fingerprint`) |
| `--envoy-apt-suite SUITE` | świadome wskazanie innego suite repozytorium |
| `--envoy-repo-url URL` | wewnętrzny mirror `apt.envoyproxy.io` (HTTPS) |
| `--wheelhouse DIR` | instalacja zależności Python offline |
| `--keep-on-failure` | bez rollbacku po błędzie (na potrzeby analizy) |

## Przebieg instalacji (warstwa 1)

1. Wykrycie systemu (`/etc/os-release`) i kontrola wersji 24.04/26.04 oraz architektury.
2. Kontrola uprawnień root i blokada równoległego uruchomienia (`flock`).
3. Test połączenia z każdym repozytorium APT (z uwzględnieniem proxy APT).
4. Kontrola, czy repozytorium Envoy publikuje pakiet dla **tego** wydania i architektury.
   Jeśli nie, instalacja kończy się czytelnym komunikatem **przed** jakąkolwiek zmianą.
5. Wykrycie istniejącej instalacji: aktualizacja, rekonfiguracja, naprawa lub przerwanie.
6. Analiza zależności i instalacja tylko brakujących pakietów (`--no-install-recommends`).
7. Repozytorium Envoy: klucz pobierany przez HTTPS i weryfikowany (odcisk), wpis deb822 z
   `Signed-By`, przypięcie origin i opcjonalnie wersji, `apt-mark hold envoy`.
8. Instalacja Envoy i weryfikacja integralności (`dpkg --verify`).
9. Użytkownik systemowy `envoy` (nologin) oraz katalogi z ograniczonymi uprawnieniami.
10. Pliki aplikacji do `/opt/omnissa-envoy-lb` i środowisko `venv` z `requirements.txt`.
11. Jednostki systemd (`daemon-reload` tylko przy zmianie).
12. Konfiguracja: walidacja, kreator (tryb interaktywny), instalacja `settings.yaml`.
13. Konfigurator Python: VIP, Netplan, Envoy, firewall, usługa, monitoring.
14. Testy końcowe (`omnissa-lb verify` i `verify-omnissa-lb.sh`). Błąd krytyczny oznacza
    rollback i kod 11.
15. Raport końcowy JSON.

### Dlaczego nie instalujemy `python3-pip` i `wget`

`pip` pochodzi z `venv` (moduł `ensurepip` z pakietu `python3-venv`), a globalne
środowisko Python Ubuntu pozostaje nietknięte. Wszystko pobiera `curl`, więc `wget`
jest zbędny. `iputils-arping` służy do wykrywania konfliktu adresu VIP i ogłaszania VIP
(gratuitous ARP).

## Idempotencja

Ponowne uruchomienie `sudo bash install.sh`:

* nie duplikuje VIP (adres sprawdzany na interfejsie przed dodaniem),
* nie duplikuje repozytoriów (wykrywany jest istniejący wpis `apt.envoyproxy.io`),
* nie duplikuje reguł firewall (tabela nftables zastępowana atomowo, ufw śledzony w stanie),
* nie nadpisuje plików bez kopii (każdy plik zarządzany ma kopię w transakcji),
* nie dotyka certyfikatów ani innych plików Netplan,
* nie restartuje usług, gdy konfiguracja się nie zmieniła.

## Logi i raporty

| Ścieżka | Zawartość |
|---|---|
| `/var/log/omnissa-lb/install.log` | znacznik czasu, operacja, wynik, kod błędu, komunikat (bez sekretów) |
| `/var/log/omnissa-lb/install-report-<data>.json` | raport końcowy (link `install-report.json`) |
| `/var/backups/omnissa-lb/tx-*/` | kopie plików, dziennik akcji, raporty testów, diagnostyka błędu |
| `/tmp/omnissa-lb-dryrun-*.log/json` | log i raport trybu dry-run |

## Aktualizacja i deinstalacja

```bash
sudo bash update.sh
```

```bash
sudo bash update.sh --envoy-version 1.34.1 --upgrade-envoy
```

```bash
sudo bash uninstall.sh --dry-run
```

```bash
sudo bash uninstall.sh --purge --remove-envoy
```

`uninstall.sh` usuwa tylko VIP dodane przez instalator. Pakiet `envoy` usuwa wyłącznie
wtedy, gdy zainstalował go instalator. Certyfikaty i kopie zapasowe zawsze zostają.

## Kody wyjścia

`0` OK, `2` użycie, `3` system, `4` root, `5` sieć/repozytoria, `6` zależności, `7` Envoy,
`8` Python, `9` konfiguracja, `10` wdrożenie, `11` testy krytyczne, `12` blokada, `13` przerwano.
