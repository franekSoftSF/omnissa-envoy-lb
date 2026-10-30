#!/usr/bin/env bash
# =============================================================================
#  Budowa paczki .deb narzędzia omnissa-envoy-lb (Architecture: all)
#
#  bash packaging/build-deb.sh            -> dist/omnissa-envoy-lb_<wersja>_all.deb
#
#  Paczka zawiera wyłącznie narzędzie (/opt/omnissa-envoy-lb + polecenia w /usr/sbin).
#  Envoy, venv i usługa systemd są konfigurowane przez: sudo omnissa-lb-install
# =============================================================================
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
VERSION="$(sed -n -E 's/^readonly INSTALLER_VERSION="([^"]+)"/\1/p' "$ROOT/install.sh")"
[[ -n "$VERSION" ]] || { echo "Nie można odczytać wersji z install.sh" >&2; exit 1; }
PY_VERSION="$(sed -n -E 's/^APP_VERSION = "([^"]+)"/\1/p' "$ROOT/modules/common.py")"
[[ "$VERSION" == "$PY_VERSION" ]] || { echo "Niezgodne wersje: install.sh=$VERSION modules/common.py=$PY_VERSION" >&2; exit 1; }
command -v dpkg-deb >/dev/null || { echo "Wymagany dpkg-deb" >&2; exit 1; }

OUT_DIR="${ROOT}/dist"
PKG="omnissa-envoy-lb_${VERSION}_all"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
STAGE="${BUILD}/${PKG}"
APP="${STAGE}/opt/omnissa-envoy-lb"
mkdir -p "$APP" "${STAGE}/DEBIAN" "${STAGE}/usr/sbin" "${STAGE}/usr/share/doc/omnissa-envoy-lb"

# Pliki narzędzia: śledzone przez git (lub cała kopia robocza bez artefaktów)
cd "$ROOT"
if git -C "$ROOT" rev-parse >/dev/null 2>&1; then
  mapfile -t FILES < <(git ls-files -- install.sh update.sh uninstall.sh omnissa-lb.py requirements.txt \
                         README.md modules config scripts systemd docs)
else
  mapfile -t FILES < <(find install.sh update.sh uninstall.sh omnissa-lb.py requirements.txt README.md \
                         modules config scripts systemd docs -type f -not -path '*/__pycache__/*')
fi
for f in "${FILES[@]}"; do
  mode=0644
  [[ "$f" == *.sh || "$f" == omnissa-lb.py ]] && mode=0755
  install -D -m "$mode" "$f" "${APP}/${f}"
done

# Polecenia
cat >"${STAGE}/usr/sbin/omnissa-lb" <<'EOF'
#!/bin/sh
# omnissa-lb (pakiet omnissa-envoy-lb)
if [ ! -x /opt/omnissa-envoy-lb/venv/bin/python3 ]; then
  echo "omnissa-lb: środowisko nie jest jeszcze przygotowane - uruchom: sudo omnissa-lb-install" >&2
  exit 8
fi
exec /opt/omnissa-envoy-lb/venv/bin/python3 /opt/omnissa-envoy-lb/omnissa-lb.py "$@"
EOF
for cmd in install update uninstall; do
  printf '#!/bin/sh\n# omnissa-lb-%s (pakiet omnissa-envoy-lb)\nexec bash /opt/omnissa-envoy-lb/%s.sh "$@"\n' "$cmd" "$cmd" \
    >"${STAGE}/usr/sbin/omnissa-lb-${cmd}"
done
chmod 0755 "${STAGE}"/usr/sbin/*

# Dokumentacja Debiana
install -m 0644 packaging/debian/copyright "${STAGE}/usr/share/doc/omnissa-envoy-lb/copyright"
gzip -9n -c packaging/debian/changelog >"${STAGE}/usr/share/doc/omnissa-envoy-lb/changelog.Debian.gz"
chmod 0644 "${STAGE}/usr/share/doc/omnissa-envoy-lb/changelog.Debian.gz"

# Skrypty maintainera i control
for s in postinst prerm postrm; do install -m 0755 "packaging/debian/$s" "${STAGE}/DEBIAN/$s"; done
SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/${VERSION}/" -e "s/@SIZE@/${SIZE}/" packaging/debian/control.in >"${STAGE}/DEBIAN/control"
(cd "$STAGE" && find . -type f -not -path './DEBIAN/*' -printf '%P\0' | sort -z | xargs -0 md5sum >DEBIAN/md5sums)
chmod 0644 "${STAGE}/DEBIAN/control" "${STAGE}/DEBIAN/md5sums"

# Poprawność składni skryptów przed spakowaniem
for s in "${STAGE}"/DEBIAN/post* "${STAGE}"/DEBIAN/prerm "${STAGE}"/usr/sbin/*; do sh -n "$s"; done

mkdir -p "$OUT_DIR"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "${OUT_DIR}/${PKG}.deb" >/dev/null
(cd "$OUT_DIR" && sha256sum "${PKG}.deb" >"${PKG}.deb.sha256")
echo "${OUT_DIR}/${PKG}.deb"
