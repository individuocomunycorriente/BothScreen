#!/usr/bin/env bash
# Compila el APK sin Android Studio ni Gradle, usando solo las herramientas
# empaquetadas en Debian/Ubuntu:
#
#   sudo apt install aapt dalvik-exchange zipalign apksigner \
#                    android-sdk-platform-23 default-jdk
#
# El código fuente no usa ninguna API posterior a Android 6, así que compila
# contra el android.jar de la API 23 que trae Ubuntu; las opciones modernas
# (modo de baja latencia de MediaCodec) se activan por nombre de clave, que es
# válido en cualquier nivel. En el manifiesto se declara targetSdk 34 igual.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/app/src/main"
BUILD="$HERE/build"
OUT="${1:-$HERE/bothscreen.apk}"

ANDROID_JAR="${ANDROID_JAR:-/usr/lib/android-sdk/platforms/android-23/android.jar}"
AAPT="${AAPT:-aapt}"
DX="${DX:-/usr/lib/android-sdk/build-tools/debian/dx}"
ZIPALIGN="${ZIPALIGN:-zipalign}"
APKSIGNER="${APKSIGNER:-apksigner}"

MIN_SDK=21
TARGET_SDK=34

for tool in "$AAPT" "$DX" "$ZIPALIGN" "$APKSIGNER" javac keytool; do
    command -v "$tool" >/dev/null 2>&1 || [ -x "$tool" ] || {
        echo "Falta la herramienta: $tool" >&2; exit 1; }
done
[ -f "$ANDROID_JAR" ] || { echo "No encuentro $ANDROID_JAR" >&2; exit 1; }

rm -rf "$BUILD"
mkdir -p "$BUILD/gen" "$BUILD/classes" "$BUILD/dex"

echo "==> recursos (R.java)"
"$AAPT" package -f -m \
    -J "$BUILD/gen" \
    -M "$SRC/AndroidManifest.xml" \
    -S "$SRC/res" \
    -I "$ANDROID_JAR" \
    --min-sdk-version "$MIN_SDK" \
    --target-sdk-version "$TARGET_SDK"

echo "==> javac"
find "$SRC/java" "$BUILD/gen" -name '*.java' > "$BUILD/sources.txt"
javac -nowarn -encoding UTF-8 \
    -source 8 -target 8 \
    -bootclasspath "$ANDROID_JAR" \
    -classpath "$ANDROID_JAR" \
    -d "$BUILD/classes" \
    @"$BUILD/sources.txt" 2>&1 | grep -v 'bootstrap class path' || true

echo "==> dex"
"$DX" --dex --min-sdk-version="$MIN_SDK" \
    --output="$BUILD/dex/classes.dex" "$BUILD/classes"

echo "==> empaquetado"
"$AAPT" package -f \
    -M "$SRC/AndroidManifest.xml" \
    -S "$SRC/res" \
    -I "$ANDROID_JAR" \
    -F "$BUILD/unsigned.apk" \
    --min-sdk-version "$MIN_SDK" \
    --target-sdk-version "$TARGET_SDK"

( cd "$BUILD/dex" && "$AAPT" add -f "$BUILD/unsigned.apk" classes.dex >/dev/null )

echo "==> zipalign"
"$ZIPALIGN" -f -p 4 "$BUILD/unsigned.apk" "$BUILD/aligned.apk"

# La clave de firma NO está en el repositorio a propósito (ver .gitignore):
# quien la tenga puede compilar un APK que Android acepte como actualización de
# esta app. Si no aparece se genera una nueva, pero ojo: los APK firmados con
# una clave distinta no se instalan encima de los anteriores, hay que
# desinstalar primero. Guarda el .keystore que uses en un sitio seguro.
KEYSTORE="${KEYSTORE:-$HERE/bothscreen.keystore}"
STOREPASS="${STOREPASS:-bothscreen}"
if [ ! -f "$KEYSTORE" ]; then
    echo "==> no hay keystore en $KEYSTORE; generando una nueva"
    echo "    (guárdala: sin ella las actualizaciones no se instalarán encima)"
    keytool -genkeypair -v \
        -keystore "$KEYSTORE" \
        -alias bothscreen \
        -keyalg RSA -keysize 2048 -validity 10950 \
        -storepass "$STOREPASS" -keypass "$STOREPASS" \
        -dname "CN=BothScreen, OU=Personal, O=Danko, L=Santiago, C=CL" \
        >/dev/null 2>&1
fi

echo "==> firma"
"$APKSIGNER" sign \
    --ks "$KEYSTORE" \
    --ks-key-alias bothscreen \
    --ks-pass "pass:$STOREPASS" \
    --key-pass "pass:$STOREPASS" \
    --v1-signing-enabled true \
    --v2-signing-enabled true \
    --out "$OUT" \
    "$BUILD/aligned.apk"

"$APKSIGNER" verify --print-certs "$OUT" >/dev/null
echo "APK listo: $OUT"
