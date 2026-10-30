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
4. Kontrola, czy repozytorium Envoy (suite `jammy`, zob. niżej) zawiera pakiet dla
   architektury hosta. Jeśli nie, instalacja kończy się czytelnym komunikatem **przed**
   jakąkolwiek zmianą.
5. Wykrycie istniejącej instalacji: aktualizacja, rekonfiguracja, naprawa lub przerwanie.
6. Analiza zależności i instalacja tylko brakujących pakietów (`--no-install-recommends`).
7. Repozytorium Envoy: klucz pobierany przez HTTPS i weryfikowany (odcisk), wpis
   `/etc/apt/sources.list.d/envoy.list` z `signed-by`, przypięcie origin i opcjonalnie
   wersji, `apt-mark hold envoy`.
8. Instalacja Envoy i weryfikacja integralności (`dpkg --verify`).
9. Użytkownik systemowy `envoy` (nologin) oraz katalogi z ograniczonymi uprawnieniami.
10. Pliki aplikacji do `/opt/omnissa-envoy-lb` i środowisko `venv` z `requirements.txt`.
11. Jednostki systemd (`daemon-reload` tylko przy zmianie).
12. Konfiguracja: walidacja, kreator (tryb interaktywny), instalacja `settings.yaml`.
13. Konfigurator Python: VIP, Netplan, Envoy, firewall, usługa, monitoring.
14. Testy końcowe (`omnissa-lb verify` i `verify-omnissa-lb.sh`). Błąd krytyczny oznacza
    rollback i kod 11.
15. Raport końcowy JSON.

### Metoda instalacji Envoy

Envoy instalowany jest z repozytorium `apt.envoyproxy.io`, suite **`jammy`**, także na
Ubuntu 24.04 i 26.04. Odpowiada to krokom ręcznym:

```bash
curl -fsSL https://apt.envoyproxy.io/signing.key | sudo gpg --dearmor -o /etc/apt/keyrings/envoy-keyring.gpg
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/envoy-keyring.gpg] https://apt.envoyproxy.io jammy main" | sudo tee /etc/apt/sources.list.d/envoy.list
```

```bash
sudo apt update && sudo apt install -y envoy
```

Pakiet `envoy` zawiera statycznie zlinkowaną binarkę, niezależną od bibliotek wydania
Ubuntu. Instalator dodatkowo weryfikuje ją na hoście: `envoy --version`,
`dpkg --verify envoy`, `envoy --mode validate` przed każdym startem oraz testy końcowe.
Gdy którykolwiek krok zawiedzie, wykonywany jest rollback. Różnice wobec ręcznej
instalacji z poradników:

* usługa działa jako dedykowany użytkownik `envoy` z utwardzonym unitem systemd (a nie jako `root`),
* klucz repozytorium jest weryfikowany (odcisk), a wersja może być przypięta i wstrzymana (`apt-mark hold`),
* zamiast `wget` używany jest `curl`. Pakiety `apt-transport-https` i `lsb-release` są zbędne:
  APT obsługuje HTTPS natywnie, a suite jest stały.

Inny suite tylko świadomie: `--envoy-apt-suite <suite>` lub `envoy.apt_suite`.
Istniejący wpis z ręcznej instalacji (`envoy.list`) jest aktualizowany z kopią zapasową.
Wpis repozytorium w innym pliku nie jest duplikowany.

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

## Instalacja z paczki .deb

Narzędzie jest dostępne także jako paczka `omnissa-envoy-lb_<wersja>_all.deb`
(`bash packaging/build-deb.sh` tworzy ją w `dist/`). Paczka instaluje wyłącznie pliki
narzędzia (`/opt/omnissa-envoy-lb`) i polecenia `omnissa-lb`, `omnissa-lb-install`,
`omnissa-lb-update`, `omnissa-lb-uninstall`. Nie zmienia sieci, usług ani Envoy.

```bash
sudo apt install ./omnissa-envoy-lb_1.1.0_all.deb
```

```bash
sudo omnissa-lb-install --unattended --config /opt/omnissa-envoy-lb/config/settings.shared-vip.yaml --dry-run
```

```bash
sudo omnissa-lb-install --unattended --config ./settings.yaml
```

Aktualizacja paczki podmienia pliki narzędzia. Zmiany w systemie stosuje dopiero
`sudo omnissa-lb-update`. `apt remove` nie przerywa ruchu (Envoy, VIP i firewall zostają).
Wdrożenie usuwa `sudo omnissa-lb-uninstall`, a `apt purge` usuwa też venv.

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
