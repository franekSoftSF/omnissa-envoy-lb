# Omnissa Envoy Load Balancer

Load balancer warstwy 4 dla **Omnissa Tunnel** i **Secure Email Gateway (SEG)** zbudowany na
Envoy Proxy. Działa na Ubuntu Server 24.04 / 26.04 i instaluje się jednym poleceniem.

* TLS passthrough (bez terminacji TLS - na LB nie ma kluczy prywatnych),
* Tunnel i SEG na **wspólnym VIP** (routing po SNI) albo na **osobnych VIP**,
* listenery TCP i UDP (Per-App Tunnel/DTLS), aktywne health checki TCP/HTTP/HTTPS,
* VIP na **jednym istniejącym interfejsie** (bez resetowania konfiguracji sieci),
* firewall (nftables lub ufw) ograniczony do ruchu kierowanego na VIP,
* monitoring (timer systemd, opcjonalnie Prometheus), backup, rollback, diagnostyka.

## Szybki start

Envoy instalowany jest z `apt.envoyproxy.io` (suite `jammy`, wpis `envoy.list`), zob.
[docs/INSTALL.md](docs/INSTALL.md#metoda-instalacji-envoy). Narzędzie można zainstalować
z repozytorium (poniżej) albo z paczki `.deb` (`bash packaging/build-deb.sh`):

```bash
sudo apt install ./dist/omnissa-envoy-lb_1.1.0_all.deb && sudo omnissa-lb-install
```

Instalacja z kopii repozytorium:

```bash
sudo bash install.sh
```

```bash
cp config/settings.shared-vip.yaml config/settings.yaml   # dostosuj FQDN, VIP, backendy
sudo bash install.sh --unattended --config ./config/settings.yaml --dry-run
```

```bash
sudo bash install.sh --unattended --config ./config/settings.yaml
```

```bash
sudo bash install.sh --unattended --interface ens160 --config ./config/settings.yaml
```

Instalator sam instaluje wszystkie zależności (APT, Envoy z podpisanego repozytorium,
venv Python), konfiguruje systemd, uruchamia konfigurator i testy końcowe. Wdrożenie jest
oznaczane jako udane **tylko** wtedy, gdy przejdą wszystkie testy krytyczne. Po błędzie
instalator przywraca poprzedni stan.

## Konfiguracja Tunnel i SEG bez edycji YAML

```bash
python3 omnissa-lb.py init-config --interface ens160 --vip 10.0.0.50 --tunnel-fqdn tunnel.example.com --tunnel-backends 10.0.1.11,10.0.1.12 --seg-fqdn seg.example.com --seg-backends 10.0.1.21,10.0.1.22 --output config/settings.yaml
```

Szczegóły i tryb `dedicated`: [docs/INSTALL.md](docs/INSTALL.md#tworzenie-konfiguracji-tunnel-i-seg).

## Po instalacji

| Polecenie | Opis |
|---|---|
| `omnissa-lb status` | stan usługi, VIP, listenerów i backendów |
| `verify-omnissa-lb` | niezależna weryfikacja (systemd, VIP, listenery, SNI, backendy, firewall) |
| `omnissa-lb verify` | testy końcowe z raportem JSON |
| `omnissa-lb configure --unattended` | ponowne zastosowanie `/etc/omnissa-lb/settings.yaml` |
| `omnissa-lb configure --interactive` | kreator rekonfiguracji |
| `omnissa-lb backup create` / `backup list` | kopie zapasowe |
| `omnissa-lb rollback --backup latest` | przywrócenie kopii |
| `omnissa-lb diagnostics` | pakiet diagnostyczny (`/var/log/omnissa-lb/diagnostics-*.tar.gz`) |
| `sudo bash update.sh` | aktualizacja (konfiguracja zachowana) |
| `sudo bash uninstall.sh` | deinstalacja |

## Struktura

```
install.sh / update.sh / uninstall.sh   warstwa 1 (Bash)
omnissa-lb.py + modules/                 warstwa 2 (Python, venv /opt/omnissa-envoy-lb/venv)
config/                                  przykłady settings.yaml i szablon envoy.yaml
systemd/                                 envoy.service + jednostki firewall/monitoringu
scripts/verify-omnissa-lb.sh             niezależna weryfikacja
packaging/                               budowa paczki .deb (build-deb.sh, debian/)
tests/                                   testy jednostkowe (python3 -m unittest discover -s tests -t .)
docs/                                    INSTALL, ARCHITECTURE, NETWORK, SECURITY, TROUBLESHOOTING
```

Szczegóły: [docs/INSTALL.md](docs/INSTALL.md).
