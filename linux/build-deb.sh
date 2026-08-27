#!/usr/bin/env bash
# Construye bothscreen_<version>_all.deb a partir de este árbol.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$HERE/bothscreen/__init__.py")"
PKG="bothscreen"
STAGE="$HERE/build/${PKG}_${VERSION}_all"
APK="${APK:-$HERE/../android/bothscreen.apk}"

rm -rf "$HERE/build"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/lib/python3/dist-packages/bothscreen" \
         "$STAGE/usr/share/$PKG" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/256x256/apps" \
         "$STAGE/usr/share/doc/$PKG"

install -m 644 "$HERE/bothscreen"/*.py \
        "$STAGE/usr/lib/python3/dist-packages/bothscreen/"

cat > "$STAGE/usr/bin/$PKG" <<'EOF'
#!/usr/bin/python3
import sys

from bothscreen.app import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 755 "$STAGE/usr/bin/$PKG"

if [ -f "$APK" ]; then
    install -m 644 "$APK" "$STAGE/usr/share/$PKG/bothscreen.apk"
else
    echo "AVISO: no encuentro el APK en $APK; el .deb irá sin él." >&2
fi

for readme in "$HERE/README.md" "$HERE/../README.md"; do
    if [ -f "$readme" ]; then
        install -m 644 "$readme" "$STAGE/usr/share/doc/$PKG/README.md"
        break
    fi
done

cat > "$STAGE/usr/share/applications/$PKG.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=BothScreen
GenericName=Monitor externo por USB
Comment=Usa una tablet Android como segunda pantalla a través del cable USB-C
Exec=$PKG --gui
Icon=$PKG
Terminal=false
Categories=Utility;System;
Keywords=pantalla;monitor;tablet;android;usb;display;
StartupNotify=true
EOF

python3 - "$STAGE/usr/share/icons/hicolor/256x256/apps/$PKG.png" <<'PY'
import os, struct, sys, zlib

path = sys.argv[1]
size = 256
px = [[(0, 0, 0, 0)] * size for _ in range(size)]
bg, fg, dim = (32, 96, 176, 255), (255, 255, 255, 255), (170, 205, 240, 255)
r = size // 6
for y in range(size):
    for x in range(size):
        inx, iny = min(x, size - 1 - x), min(y, size - 1 - y)
        if inx >= r or iny >= r or ((r - inx) ** 2 + (r - iny) ** 2) <= r * r:
            px[y][x] = bg

def rect(x0, y0, x1, y1, col):
    for y in range(max(0, y0), min(size, y1)):
        for x in range(max(0, x0), min(size, x1)):
            px[y][x] = col

u = size / 24.0
rect(int(3 * u), int(6 * u), int(14 * u), int(14 * u), fg)
rect(int(4 * u), int(7 * u), int(13 * u), int(13 * u), bg)
rect(int(5 * u), int(14 * u), int(12 * u), int(15 * u), fg)
rect(int(14 * u), int(9 * u), int(21 * u), int(18 * u), dim)
rect(int(15 * u), int(10 * u), int(20 * u), int(17 * u), bg)

raw = b''.join(b'\x00' + b''.join(bytes(p) for p in row) for row in px)

def chunk(t, d):
    c = t + d
    return struct.pack('>I', len(d)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)

os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'wb') as fh:
    fh.write(b'\x89PNG\r\n\x1a\n')
    fh.write(chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)))
    fh.write(chunk(b'IDAT', zlib.compress(raw, 9)))
    fh.write(chunk(b'IEND', b''))
PY

# La ventana busca el logo primero en el tema de iconos y luego aquí, por si
# el tema no se ha refrescado todavía tras la instalación.
cp "$STAGE/usr/share/icons/hicolor/256x256/apps/$PKG.png" \
   "$STAGE/usr/share/$PKG/logo.png"

cat > "$STAGE/usr/share/doc/$PKG/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: bothscreen

Files: *
Copyright: 2026 Danko Leiva
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in
 all copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 THE SOFTWARE.
EOF

{
cat <<EOF
$PKG (1.1.0) unstable; urgency=medium

  * El proyecto pasa a llamarse BothScreen. Cambian el nombre del paquete, el
    ejecutable, el modulo de Python, el applicationId de Android y la carpeta
    de configuracion.
  * Al instalar se retira el paquete antiguo segunda-pantalla, y al conectar la
    tablet se desinstala sola la app anterior: no quedan dos iconos que hacen
    lo mismo.
  * Los ajustes guardados por la version anterior se recuperan la primera vez.

 -- Danko Leiva <danko.leiva236@gmail.com>  $(date -R)

$PKG (1.0.4) unstable; urgency=medium

  * Arreglado el fallo de la 1.0.3: fijar el framerate en los caps rompia la
    negociacion con pipewiresrc ("no more input formats" / "error set output
    format: -22") y no arrancaba ningun pipeline. Mutter anuncia framerate
    variable, asi que no se le puede exigir un valor; la cadencia se controla
    en el refresh-rate del monitor virtual.
  * Un candidato de pipeline que falla ya no cierra la sesion entera: los
    errores del bus durante la busqueda se tratan como informacion, no como
    averia, y los candidatos siguientes conservan el monitor virtual.
  * Sin avisos de GStreamer al cambiar de candidato (sondas y bus).

 -- Danko Leiva <danko.leiva236@gmail.com>  $(date -R)

$PKG (1.0.3) unstable; urgency=medium

  * La pantalla ya se actualiza con cambios pequenos y aislados, como el
    puntero cruzando un escritorio quieto. Tres causas: pipewiresrc retenia
    los buffers de Mutter (always-copy=false) y Mutter descarta el fotograma
    sin reintentarlo cuando no le queda ninguno libre; videorate se quedaba
    con fotogramas sueltos y ensuciaba la negociacion de caps; y el framerate
    no iba fijado en los caps, del que depende cada cuanto graba Mutter.
  * El tope de fps se aplica ahora con una sonda en un pad, sin retener nada.

 -- Danko Leiva <danko.leiva236@gmail.com>  $(date -R)

$PKG (1.0.2) unstable; urgency=medium

  * El puntero del ratón ya se ve. No era una propiedad de RecordVirtual sino
    la negociación de buffers: pidiendo memory:DMABuf, Mutter sirve el
    fotograma del monitor virtual con un blit que no dibuja el cursor. Con el
    puntero activado ya no se ofrece ese pipeline.
  * --diagnostico ahora compara los dos caminos de captura.

 -- Danko Leiva <danko.leiva236@gmail.com>  $(date -R)

$PKG (1.0.1) unstable; urgency=medium

  * Iniciar vuelve a funcionar tras Detener: el hilo de aceptación se cierra
    de verdad y libera el puerto.
  * Detener corta la sesión también para la tablet; ya no queda un servidor
    fantasma atendiendo conexiones.
  * El monitor virtual deja de declararse "de plataforma", que es lo que
    impedía que Mutter incrustara el puntero del ratón.
  * Nueva orden --diagnostico para averiguar qué ajuste muestra el puntero.
  * Los ajustes de la ventana se recuerdan entre sesiones.
  * El servidor adb se apaga al salir si lo habíamos levantado nosotros.
  * La app de la tablet se desconecta al pasar a segundo plano y deja que la
    pantalla se apague cuando no hay transmisión.
  * Logo y autoría en la ventana de control.

 -- Danko Leiva <danko.leiva236@gmail.com>  $(date -R)

$PKG (1.0.0) unstable; urgency=medium

  * Primera versión.

 -- Danko Leiva <danko.leiva236@gmail.com>  $(date -R)
EOF
} | gzip -9n > "$STAGE/usr/share/doc/$PKG/changelog.Debian.gz"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.10),
 python3-gi,
 gir1.2-glib-2.0,
 gir1.2-gtk-4.0,
 gir1.2-adw-1,
 gir1.2-gstreamer-1.0,
 gir1.2-gst-plugins-base-1.0,
 gstreamer1.0-plugins-base,
 gstreamer1.0-plugins-good,
 gstreamer1.0-plugins-bad,
 gstreamer1.0-pipewire,
 adb
Recommends: mesa-va-drivers, va-driver-all, vainfo
Suggests: gstreamer1.0-plugins-ugly
Replaces: segunda-pantalla
Conflicts: segunda-pantalla
Provides: segunda-pantalla
Maintainer: Danko Leiva <danko.leiva236@gmail.com>
Installed-Size: $(du -ks "$STAGE" | cut -f1)
Description: Usa una tablet Android como segunda pantalla por USB-C
 Crea un monitor virtual real en GNOME/Wayland mediante la API de
 screencast de Mutter, lo codifica en H.264 o HEVC con la GPU (VA-API)
 y lo envía a una app Android por un tunel adb sobre el cable USB-C.
 .
 El caudal se ajusta solo segun lo que aguante el enlace y la pantalla
 solo transmite cuando algo cambia, asi que en reposo el consumo de
 ancho de banda es practicamente cero.
EOF

( cd "$STAGE" && find usr -type f -print0 \
    | xargs -0 md5sum > DEBIAN/md5sums )

fakeroot dpkg-deb --build --root-owner-group "$STAGE" \
    "$HERE/${PKG}_${VERSION}_all.deb" >/dev/null 2>&1 \
    || dpkg-deb --build --root-owner-group "$STAGE" \
       "$HERE/${PKG}_${VERSION}_all.deb"

echo "Paquete listo: $HERE/${PKG}_${VERSION}_all.deb"
