# Sieć, VIP i Netplan

## Jeden interfejs

Wszystkie VIP konfigurowane są na **jednym istniejącym** interfejsie (`network.interface`,
`--interface` albo `auto`, czyli interfejs trasy domyślnej). Instalator:

* nie tworzy nowych interfejsów, mostów ani VLAN-ów,
* nie zmienia adresu głównego, DHCP, tras, DNS ani MTU,
* nie modyfikuje istniejących plików Netplan.

## Tryby VIP

| Tryb | Opis | Przykład |
|---|---|---|
| `shared` | Tunnel i SEG na jednym VIP, rozdział ruchu po SNI | `config/settings.shared-vip.yaml` |
| `dedicated` | osobny VIP dla każdej usługi (na tym samym interfejsie) | `config/settings.dedicated-vip.yaml` |

Maska VIP jest domyślnie maską podsieci adresu głównego. VIP spoza tej podsieci wymaga
jawnego `vip.prefix` (np. `32`).

## Jak dodawany jest VIP

1. Kontrola: czy adres nie jest już na interfejsie (idempotencja), na innym interfejsie
   (błąd) albo w sieci (`arping -D`, konflikt IP oznacza błąd).
2. `ip addr add VIP/prefix dev IFACE` (natychmiast, bez restartu sieci) i gratuitous ARP.
3. Trwałość: osobny plik `/etc/netplan/60-omnissa-lb-vip.yaml` (0600), który Netplan scala
   z konfiguracją interfejsu:

   ```yaml
   network:
     version: 2
     ethernets:
       ens160:
         addresses:
           - 10.0.0.50/24
   ```

4. Przed zapisem wynik scalenia jest symulowany w izolowanym katalogu
   (`netplan generate --root-dir`, `netplan get --root-dir`). Jeśli scalenie zmieniłoby adres
   główny, DHCP, trasy, DNS, MTU lub dopasowanie interfejsu, operacja jest przerywana.
   Interfejsy z `set-name`/`match` oraz bondy i VLAN-y rozpoznawane są po identyfikatorze Netplan.

## `network.apply_mode`

| Wartość | Działanie |
|---|---|
| `runtime` (domyślnie) | VIP w runtime + trwały plik Netplan, **bez** `netplan apply` (zero przerw w ruchu) |
| `netplan-apply` | `netplan apply` z mechanizmem potwierdzenia łączności |

W trybie `netplan-apply` przed `netplan apply` uzbrajany jest niezależny timer systemd
(`omnissa-lb-netplan-rollback`, `confirm_timeout` sekund). Po zastosowaniu sprawdzane są:
adres główny, brama i `connectivity_targets`. Timer jest rozbrajany tylko po pozytywnym
teście. Jeśli test się nie powiedzie albo sesja instalatora zostanie zerwana, timer sam
przywraca poprzedni plik i wykonuje `netplan apply`.

## Porty Omnissa

Porty są w pełni konfigurowalne (`services.*.listeners`). Typowe układy:

* wspólny VIP: TCP 443 (Tunnel + SEG po SNI), UDP 443 (Tunnel),
* osobne VIP: Tunnel TCP/UDP 8443, SEG TCP 443.

## DNS

Rekordy A FQDN Tunnel i SEG powinny wskazywać na właściwy VIP (lub adres NAT/GSLB przed
nim). `omnissa-lb dns` sprawdza to samo co testy końcowe. `dns.require_match: true` robi
z tego test krytyczny.
