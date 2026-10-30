# Bezpieczeństwo

## Łańcuch dostaw

* Envoy pochodzi wyłącznie z oficjalnego repozytorium `apt.envoyproxy.io` (albo jawnie
  wskazanego mirrora HTTPS). Klucz pobierany jest przez HTTPS (TLS 1.2+), jego odcisk jest
  logowany i może być wymagany (`envoy.apt_key_fingerprint` / `--envoy-key-fingerprint`,
  **zalecane w produkcji**). Keyring jest używany wyłącznie przez to repozytorium
  (`Signed-By`), a APT weryfikuje podpis `InRelease`.
* Zmiana klucza repozytorium wymaga potwierdzenia administratora. W trybie unattended
  wymaga przypiętego odcisku.
* Brak `curl | bash` i niezweryfikowanych binariów. Suite repozytorium (`jammy`) jest stałą,
  udokumentowaną decyzją projektu (pakiet z binarką statyczną), a nie automatycznym doborem.
  Zgodność binarki z hostem jest weryfikowana po instalacji, a błąd oznacza rollback.
* `dpkg --verify envoy` sprawdza integralność plików pakietu. `apt-mark hold envoy`
  blokuje niekontrolowane aktualizacje (np. unattended-upgrades).
* Zależności Python mają przypięte wersje (`requirements.txt`), trafiają do izolowanego
  `venv` (`--require-virtualenv`, najpierw tylko wheel). Nie używamy
  `--break-system-packages`. Instalacja offline: `--wheelhouse`.

## Usługa Envoy

* Dedykowany użytkownik `envoy` (system, `nologin`, bez katalogu domowego).
* Jedyna capability: `CAP_NET_BIND_SERVICE` (porty < 1024).
* `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`,
  `ProtectKernel*`, `ProtectControlGroups`, `RestrictAddressFamilies`, `RestrictNamespaces`,
  `LockPersonality`, `SystemCallArchitectures=native`, `UMask=0027`.
* Konfiguracja `/etc/envoy/envoy.yaml`: `root:envoy 0640`, katalog `0750`.
* Limity: `LimitNOFILE`, `TasksMax`, `MemoryMax`, opcjonalnie `CPUQuota`, globalny limit
  połączeń (overload manager) i circuit breakers per klaster.
* Walidacja konfiguracji przed każdym startem (`ExecStartPre=envoy --mode validate`).
* Admin API wyłącznie na loopback (walidacja odrzuca inny adres).

## Ruch i dane

* TLS passthrough: na LB nie ma kluczy prywatnych ani certyfikatów usług. Istniejące
  certyfikaty (np. `/etc/envoy/certs`) nigdy nie są modyfikowane ani usuwane.
* Nieznane SNI jest domyślnie odrzucane.
* Firewall dotyczy wyłącznie ruchu do VIP: dozwolone są listenery z `allowed_sources`,
  reszta jest odrzucana (`restrict_vip`). SSH i inne usługi hosta pozostają nietknięte.
  Instalator nie włącza ufw samodzielnie (ryzyko odcięcia sesji).
* nftables: własna tabela, bez `flush ruleset`. `nftables.service` nie jest włączany,
  bo jego konfiguracja domyślnie czyści cały ruleset.

## Logi

* Logi instalatora i konfiguratora maskują wartości wyglądające na sekrety (`password`,
  `token`, `secret`, `api_key`, `passphrase`...). `settings.yaml` nie zawiera sekretów.
* Pliki logów i raportów mają uprawnienia `0640`, kopie zapasowe `0700`.
