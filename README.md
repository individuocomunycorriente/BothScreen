## Disclaimer
Este proyecto está hecho completamente con Claude. Tuve una necesidad y la cubrí lo más rápido posible. 
Hasta ahora funciona bien, pero cualquier error que noten, por favor háganlo saber por medio de las issues.

# BothScreen

Convierte una **Samsung Galaxy Tab S7** en un monitor externo de tu **Ubuntu
26.04 (GNOME/Wayland)** a través del **cable USB-C**. Sin Wi-Fi, sin servidores
intermedios y sin módulos de kernel.

Dos piezas:

| Pieza | Qué es | Dónde va |
|---|---|---|
| `bothscreen_1.2.0_all.deb` | Daemon + ventana de control (Python/GTK4) | PC Linux |
| `bothscreen.apk` | Visor a pantalla completa (Java/MediaCodec) | Tab S7 |

El `.deb` ya lleva el `.apk` dentro y lo instala solo en la tablet la primera
vez que la conectas.

---

## 1. Cómo funciona

```
┌──────────────────── Ubuntu 26.04 ────────────────────┐      ┌── Tab S7 ──┐
│                                                      │      │            │
│  Mutter ──RecordVirtual──> monitor virtual 1920x1200 │      │            │
│    │                                                 │      │            │
│    └── PipeWire ──> GStreamer ──> VA-API (Radeon)    │      │            │
│                                    H.264 / HEVC      │      │            │
│                                        │             │      │            │
│                                    TCP :27183 ───────┼─USB──┼─> MediaCodec
│                                                      │      │      │     │
│                                    <──── ACKs ───────┼──────┼──────┘     │
└──────────────────────────────────────────────────────┘      └────────────┘
```

Las cuatro decisiones que hacen que esto vaya fino y gaste poco:

**Monitor virtual de verdad, no un espejo.** Se usa `RecordVirtual` de la API
`org.gnome.Mutter.ScreenCast`. GNOME crea una región del escritorio que no está
respaldada por hardware pero que aparece en *Configuración → Pantallas* como un
monitor más: puedes colocarla a la izquierda, a la derecha o arriba, arrastrar
ventanas y darle su propio espacio de trabajo. Desde GNOME 50 (el de Ubuntu
26.04) se le puede fijar el modo exacto, así que pedimos 1920×1200 @60 y no
dejamos que PipeWire negocie cualquier cosa. No hace falta `evdi`, DisplayLink
ni parchear Xorg.

**El compositor solo entrega frames cuando algo cambia.** El stream de Mutter va
guiado por daño: si la pantalla virtual está quieta, no llegan buffers y no se
transmite absolutamente nada. En reposo el consumo baja prácticamente a cero sin
que haya que inventar ninguna heurística de "detección de pantalla estática".

**Codificación en la GPU.** Tu Ryzen 5 7535HS lleva una Radeon 660M con VCN 3.x,
que codifica H.264 y HEVC por hardware. Se usa el plugin `va` de GStreamer, y la
conversión y el escalado también van en la GPU con `vapostproc`. El daemon tiene
varios pipelines candidatos y los prueba en orden hasta que uno arranca, así que
si VA-API fallara caería solo a `x264enc` por software.

El fotograma llega del compositor por memoria de sistema y no por DMABuf, que
sería lo ideal, porque es la única forma de que GNOME dibuje el puntero del
ratón en un monitor virtual (el porqué está en *El puntero del ratón*). Con
`--no-cursor` se usa el camino cero-copia, en el que el fotograma no pasa por la
CPU en ningún momento.

**Control adaptativo con ACKs.** La tablet confirma cada frame que ya pintó. El
daemon mira cuántos lleva sin confirmar: si se acumulan, baja el bitrate un 30 %
y, si la cosa sigue mal, recorta los fps; si se mantiene al día durante dos
segundos, sube un 15 %. Es un AIMD clásico, el mismo principio que TCP, y evita
tanto los cortes como el gastar 20 Mbps para mostrar un editor de texto. Lo que
nunca hace es bajar el tope de fps por debajo del suelo pedido (ver *Los fps
mínimos*).

**Un suelo de fps.** Que el compositor no repinte no puede significar que la
tablet se quede a medias. Por defecto se garantizan 10 fps: si en una décima de
segundo no llega nada nuevo, se reenvía el último fotograma. Repetir una imagen
idéntica se codifica en unos cientos de bytes, así que en reposo se sigue sin
gastar ancho de banda apreciable, y a cambio lo que se ve en la tablet nunca va
un fotograma por detrás de lo que hay en el PC.

Además, `h264parse`/`h265parse` pegan los SPS/PPS delante de cada keyframe, pero
`MediaCodec` los quiere en un buffer aparte marcado como `BUFFER_FLAG_CODEC_CONFIG`.
El daemon los separa antes de enviarlos; si no, el decodificador se traga el
primer keyframe y la tablet se queda en negro unos segundos.

---

## 2. Requisitos

- Ubuntu 26.04 con sesión **GNOME sobre Wayland** (la predeterminada).
  Compruébalo con `echo $XDG_SESSION_TYPE` → debe decir `wayland`.
- Un cable USB-C **de datos** (los de solo carga no sirven).
- La Tab S7 con **Depuración USB** activada.

---

## 3. Instalación

### 3.1 En el PC

```bash
sudo apt install ./bothscreen_1.2.0_all.deb
```

Si `apt` se queja de dependencias:

```bash
sudo apt --fix-broken install
```

Comprueba que la aceleración por hardware está disponible:

```bash
vainfo | grep -E 'H264|HEVC'
```

Deberías ver entradas `VAProfileH264...` y `VAProfileHEVCMain` con
`VAEntrypointEncSlice`. Si no aparecen, instala `mesa-va-drivers` y reinicia la
sesión; sin ellas todo funciona igual pero codificando por CPU.

### 3.2 Activar la depuración USB en la Tab S7

1. **Ajustes → Información del software** → toca 7 veces *Número de compilación*.
2. Vuelve a **Ajustes → Opciones de desarrollador** → activa **Depuración USB**.
3. Conecta el cable y acepta el diálogo *"¿Permitir depuración USB?"*
   (marca *Permitir siempre desde este equipo*).

Verifica desde el PC:

```bash
adb devices     # debe listar tu tablet como "device", no "unauthorized"
```

### 3.3 La app de la tablet

No hace falta hacer nada: el daemon detecta que no está instalada y la instala.
Si prefieres hacerlo a mano:

```bash
adb install -r /usr/share/bothscreen/bothscreen.apk
```

Android avisará de que viene de un origen desconocido porque está firmada con
una clave propia, no con una de Google Play. Es esperable.

---

## 4. Uso

### Con ventana

Busca **BothScreen** en el lanzador de aplicaciones, o:

```bash
bothscreen --gui
```

Elige resolución y códec, pulsa **Iniciar** y la app se abre sola en la tablet.
La ventana muestra en vivo el caudal real, los fps y cuántos frames van en
vuelo. Al pulsar **Detener** el monitor virtual desaparece y el escritorio
vuelve a su disposición anterior.

### Desde la terminal

```bash
bothscreen                       # 1920x1200 @60, HEVC, adaptativo
bothscreen --size 1600x1000      # menos ancho de banda
bothscreen --codec h264          # si HEVC diera problemas
bothscreen --no-adaptive --bitrate 10000   # bitrate fijo
bothscreen --fps 30              # la mitad de datos, sigue fluido para trabajar
bothscreen --fps-minimo 30       # refresco garantizado más alto (cursor más fino)
bothscreen --fps-minimo 0        # solo transmitir cuando algo cambia
bothscreen -v                    # log detallado, útil para diagnosticar
```

### Colocar la pantalla

Una vez conectada, ve a **Configuración → Pantallas** y arrastra el monitor
nuevo a donde lo tengas físicamente. GNOME recuerda la posición para la próxima
vez.

### El puntero del ratón

Que el cursor se vea no depende de una opción de configuración sino de **cómo se
le pide el vídeo a GNOME**, y tiene truco. Mutter entrega el fotograma de dos
maneras y elige según lo que negocie el cliente:

| Caps que pide GStreamer | Mutter sirve | Función que usa | ¿Cursor? |
|---|---|---|---|
| `video/x-raw(memory:DMABuf)` | `SPA_DATA_DmaBuf` | `record_to_framebuffer` | **no** |
| `video/x-raw` a secas | `SPA_DATA_MemFd` | `record_to_buffer` | sí |

En el camino DMABuf, el stream **virtual** de Mutter hace un
`cogl_blit_framebuffer` pelado, sin ningún tratamiento del cursor — a diferencia
de los streams de monitor y de área, que repintan con
`CLUTTER_PAINT_FLAG_FORCE_CURSORS` en los dos caminos. En el camino por memoria
sí repinta con esa bandera. Es una asimetría del propio Mutter, no algo que se
pueda configurar.

Por eso, cuando el puntero está activado, la aplicación deja de pedir DMABuf y
captura por memoria. Cuesta una lectura del fotograma por la CPU; a cambio se ve
el cursor. Con `--no-cursor` se recupera el camino cero-copia.

Si quieres verlo con tus propios ojos:

```bash
bothscreen --diagnostico
```

Captura un fotograma con el cursor oculto y otro con el cursor incrustado por
cada camino, y los compara: si la única diferencia es una manchita de unos
cientos de píxeles, ese es el puntero. Deja los PNG en
`~/.cache/bothscreen/`. Durante la prueba hay que dejar el ratón quieto
sobre la pantalla virtual.

### Los fps mínimos

El stream de Mutter va guiado por daño: si en la pantalla virtual no cambia
nada, no llega ni un fotograma. Eso ahorra muchísimo ancho de banda, pero tiene
un efecto secundario feo: el decodificador de la tablet no saca a pantalla el
último fotograma que ha recibido hasta que le llega el siguiente. Con el
escritorio quieto, ese último fotograma es justo el que lleva el puntero en su
posición nueva, así que el cursor parecía no existir hasta que se movía otra
cosa.

El suelo de fps lo arregla obligando a que salga un fotograma cada tanto pase lo
que pase. Se implementa con `keepalive-time` de `pipewiresrc`: cuando el hilo de
captura lleva ese tiempo esperando un buffer que no llega, sale por *timeout* y
empuja otra referencia del último, poniéndole PTS y DTS del reloj del pipeline
(`gst_pipewire_src_create`). Es decir, el fotograma repetido entra más adelante
en la línea de tiempo, no como un duplicado, así que ni el limitador lo descarta
ni el codificador ve el reloj ir hacia atrás.

```bash
bothscreen --fps-minimo 10       # por defecto
bothscreen --fps-minimo 30       # el puntero se mueve más fino
bothscreen --fps-minimo 0        # desactivado: solo cuando algo cambia
```

En la ventana es **Calidad → FPS mínimos garantizados**. El suelo nunca puede
superar al tope de fps: si pides más, manda el tope.

### Ajustes que se recuerdan

Lo que elijas en la ventana se guarda en
`~/.config/bothscreen/config.json`. Desde la terminal, cualquier opción
que escribas manda sobre lo guardado, y `--sin-ajustes-guardados` ignora el
archivo por completo.

---

## 5. Qué queda corriendo (y qué no)

Es una preocupación razonable en algo que graba la pantalla y habla con la
tablet, así que aquí está el inventario completo:

**Mientras no transmites.** El monitor virtual no existe: se crea cuando la
tablet conecta y se destruye cuando se va, de modo que el escritorio queda
exactamente como estaba. No hay pipeline de GStreamer, no hay codificador, no
hay hilos de trabajo. Al pulsar **Detener** se cierra el socket de escucha, se
espera a que el hilo de aceptación muera de verdad y se libera el puerto; el
`adb reverse` se retira y la app de la tablet se cierra con `am force-stop`.

**Al cerrar la ventana.** Se hace lo mismo que en Detener y además se apaga el
servidor `adb` — pero solo si lo habíamos levantado nosotros. Si ya tenías adb
corriendo para otra cosa (Android Studio, scrcpy), se deja en paz. El proceso
del daemon termina con la ventana: no queda ningún servicio de fondo, ni unidad
de systemd, ni nada que arranque con la sesión.

**En la tablet.** La app solo mantiene el socket y el decodificador mientras
está en primer plano; al pasar a segundo plano los suelta. Cuando no hay
transmisión reintenta con espera creciente (de 0,8 s a 5 s como mucho) en vez de
martillear el socket, y a los 90 segundos sin vídeo deja de forzar la pantalla
encendida para que la tablet se pueda dormir sola.

Puedes comprobarlo tú mismo tras cerrar la aplicación:

```bash
pgrep -a -f bothscreen     # no debería devolver nada
pgrep -a adb                     # tampoco, salvo que ya lo usaras antes
```

---

## 6. Qué esperar de calidad y consumo

Medido a 1920×1200, que es la relación 16:10 exacta de la Tab S7 (su panel es
2560×1600, así que la GPU de la tablet escala sin deformar ni dejar bandas):

| Uso | Códec | Caudal típico | Notas |
|---|---|---|---|
| Escritorio quieto | cualquiera | **~0** | el compositor no envía frames |
| Editor de texto, terminal | HEVC | 0,3 – 1,5 Mbps | picos al hacer scroll |
| Navegar, leer PDF | HEVC | 2 – 5 Mbps | |
| Vídeo a pantalla completa | HEVC | 8 – 12 Mbps | el techo por defecto es 14 |
| Lo mismo en H.264 | H.264 | ~35 % más | a cambio, decodifica más rápido |

El USB 3 del túnel `adb` da del orden de 300 Mbps, así que el cuello de botella
nunca es el cable: es el codificador y el decodificador. La latencia extremo a
extremo se queda en unos 40–70 ms, que se nota al mover el ratón por esa
pantalla pero es perfectamente cómodo para tener ahí documentación, Slack,
logs o Spotify.

**Recomendación:** deja HEVC y adaptativo activados. Si vas a usar la tablet
para vídeo y prefieres nitidez sobre consumo, sube el bitrate máximo a 20000.

---

## 7. Solución de problemas

**«No se pudo hablar con org.gnome.Mutter.ScreenCast»**
Estás en X11 o en un escritorio que no es GNOME. Cierra sesión y en la pantalla
de login elige **GNOME** (no *GNOME en Xorg*).

**La tablet aparece como `unauthorized`**
Desbloquea la tablet: el diálogo de autorización de depuración USB solo se
muestra con la pantalla desbloqueada. Si no aparece,
`adb kill-server && adb start-server` y reconecta el cable.

**Pantalla negra en la tablet, pero el daemon dice que transmite**
Casi siempre es el códec. Prueba `bothscreen --codec h264`. Si con H.264
va bien, tu Tab S7 está rechazando el perfil HEVC que genera la Radeon.

**La pantalla parece congelada hasta que muevo mucho las cosas**
Desde la 1.2.0 hay un suelo de fps precisamente para esto: mira que en la ventana
*FPS mínimos garantizados* no esté en 0 y súbelo a 20-30. El campo *Caudal*
muestra los fotogramas por segundo reales; en reposo tienen que rondar el suelo
que hayas puesto, nunca 0.

**Va a tirones o el HUD muestra muchos «frames en vuelo»**
Baja la resolución (`--size 1600x1000`) o los fps (`--fps 30`). Si persiste con
todo bajo, prueba `--software` para descartar que el problema esté en VA-API.

**El puntero del ratón no se ve**
Primero, sube los fps mínimos: `bothscreen --fps-minimo 30`. Si el cursor se ve
al mover ventanas pero no sobre el escritorio quieto, el problema era ese y no
la captura. Si sigue sin verse, comprueba en el log (`bothscreen -v`) qué
pipeline se activó: tiene que poner `va-memoria`, `va-memoria-cpu` o `software`,
nunca `va-dmabuf`. Si sale `va-dmabuf` es que el puntero está desactivado en los
ajustes. Ver *El puntero del ratón* más arriba para el porqué, y `--diagnostico`
para comprobarlo.

**Pulso Detener y luego Iniciar y no arranca**
Eso era un fallo de la 1.0.0: el hilo que esperaba conexiones no moría y dejaba
el puerto ocupado, con el efecto añadido de que la tablet seguía recibiendo
vídeo aunque la ventana dijera lo contrario. Corregido en la 1.0.1; si lo ves
otra vez, `bothscreen -v` lo dirá en el log.

**Se desconecta al bloquear la tablet**
La app pide mantener la pantalla encendida, pero el ahorro de energía agresivo de
One UI puede matarla igual. En **Ajustes → Aplicaciones → BothScreen →
Batería**, ponla en *Sin restricciones*.

**Quiero cambiar el puerto**
`bothscreen --port 30000`. El daemon monta el `adb reverse` con ese puerto
y se lo pasa a la app por el intent de arranque.

---

## 8. Compilar desde el código

El árbol de fuentes tiene esta forma:

```
bothscreen/
├── linux/
│   ├── bothscreen/       # el daemon
│   │   ├── virtualmonitor.py  # D-Bus con Mutter
│   │   ├── encoder.py         # pipelines GStreamer + separación de CSD
│   │   ├── server.py          # protocolo, cola de envío, control adaptativo
│   │   ├── adb.py             # túnel USB
│   │   ├── gui.py             # ventana GTK4/libadwaita
│   │   └── app.py             # CLI
│   ├── tests/                 # pruebas que corren sin GNOME ni GPU
│   └── build-deb.sh
└── android/
    ├── app/src/main/java/...  # MainActivity, StreamClient, VideoDecoder
    ├── build-apk.sh           # compila sin Android Studio
    └── build.gradle           # …o ábrelo en Android Studio
```

### El `.deb`

```bash
cd linux && ./build-deb.sh
```

### El `.apk` sin Android Studio

El código no usa ninguna API posterior a Android 6, así que compila contra el
`android.jar` que empaqueta Ubuntu. Las opciones modernas (modo de baja latencia
de `MediaCodec`) se activan por nombre de clave, que es válido en cualquier nivel
de API, y el manifiesto declara `targetSdk 34` igual.

```bash
sudo apt install aapt dalvik-exchange zipalign apksigner \
                 android-sdk-platform-23 default-jdk
cd android && ./build-apk.sh
```

### El `.apk` con Android Studio

Abre la carpeta `android/` y compila normal. Usa un manifiesto propio
(`app/src/gradle/AndroidManifest.xml`) porque AGP 8 ya no acepta el atributo
`package`, pero comparte exactamente las mismas fuentes Java.

### Publicar el repositorio

Lo que **nunca** debe entrar en git está en `.gitignore`: la clave de firma
(`*.keystore`), los binarios (`*.deb`, `*.apk`) y los paquetes de fuentes. Antes
de subir nada, dos comprobaciones que cuestan cinco segundos:

```bash
git ls-files | grep -iE 'keystore|\.jks|\.deb$|\.apk$'   # no debe imprimir nada
git log --all --numstat --format= | sort -u | grep -i keystore   # tampoco
```

La segunda importa tanto como la primera: borrar un archivo en un commit
posterior no lo saca del historial, y en un repositorio público eso equivale a
haberlo publicado. Si alguna vez aparece, lo correcto es generar una clave nueva
y no confiar en la antigua.

El `.deb` y el `.apk` se publican como *Releases* de GitHub, no como archivos
versionados.

### La clave de firma

`android/bothscreen.keystore` **no está en el repositorio** y no debe
estarlo: quien la tenga puede compilar un APK que Android acepte como
actualización de esta app. Guárdala aparte. Si `build-apk.sh` no la encuentra
genera una nueva, pero un APK firmado con otra clave no se instala encima del
anterior: hay que desinstalar la app primero.

### Pruebas

```bash
cd linux/tests
python3 test_pipeline.py   # codificación, Annex-B, CSD, ajuste en caliente
python3 test_session.py    # handshake, ACKs, adaptación, reconexión
python3 test_restart.py    # el ciclo Iniciar/Detener/Iniciar de la ventana
python3 test_ajustes.py    # ajustes guardados, precedencia, limpieza de adb
python3 test_puntero.py    # que ningún pipeline con puntero negocie DMABuf
python3 test_fluidez.py    # que un fotograma aislado no se quede atascado
javac -cp ../../android/app/src/main/java WireTest.java && java WireTest
```

`test_session.py` levanta el servidor real con una tablet simulada y solo
sustituye las dos piezas que necesitan hardware (Mutter y VA-API), así que
cubre el protocolo completo sin tener nada conectado. `test_restart.py` es el
que faltaba en la 1.0.0 y el que destapó los dos fallos del botón.

---

## 9. Limitaciones conocidas

- **Solo GNOME sobre Wayland.** La API de monitores virtuales es de Mutter. En
  KDE haría falta reescribir `virtualmonitor.py` sobre `kwin` y en X11 sobre
  salidas `VIRTUAL` de xrandr.
- **Sin entrada táctil.** La tablet es una pantalla pasiva, tal como lo pediste.
  Añadirlo es un canal de vuelta con `org.gnome.Mutter.RemoteDesktop` y
  `NotifyPointerMotionAbsolute` contra el mismo stream; el protocolo ya tiene
  espacio para mensajes de control en esa dirección.
- **Sin audio.** El sonido sigue saliendo por el PC.
- **Una tablet a la vez.**
- **La API de Mutter es privada.** GNOME no promete compatibilidad entre
  versiones; una actualización mayor podría requerir tocar `virtualmonitor.py`.
  Por eso el código ya negocia: si `modes` no está soportado, cae a dejar que
  PipeWire negocie el tamaño. Por lo mismo, el comportamiento del puntero
  depende de la versión y por eso existe `--diagnostico`.

---

## 10. Historial

### 1.2.0

- **Suelo de fps configurable**, por defecto 10. La tablet se refresca a ese
  ritmo aunque en la pantalla no cambie nada, y con eso el puntero se ve moverse
  sobre un escritorio quieto: hasta ahora el último fotograma recibido —el que
  llevaba el cursor recién movido— se quedaba sin mostrar en la tablet hasta que
  algo más se movía. Se ajusta en la ventana (*Calidad → FPS mínimos
  garantizados*) o con `--fps-minimo N`; con `0` se recupera el comportamiento
  anterior, puramente guiado por daño.
- Se implementa con el `keepalive-time` de `pipewiresrc`, que reenvía el último
  buffer con marca de tiempo nueva. No toca la negociación de caps, que es la
  parte frágil de todo esto.
- El ajuste automático ya no puede bajar el tope de fps por debajo del suelo
  pedido: si pudiera, el limitador empezaría a descartar los reenvíos justo
  cuando el enlace va peor.
- `test_fluidez.py` cubre el suelo (intervalos, recorte contra el tope, cambio
  en caliente) y `test_ajustes.py` que se guarde y se pueda pisar desde la
  terminal.

### 1.1.0

- **El proyecto pasa a llamarse BothScreen.** Cambian el nombre del paquete
  Debian, el ejecutable, el módulo de Python, el `applicationId` de Android, la
  carpeta de configuración y el identificador del protocolo.
- Instalar el paquete nuevo retira el antiguo `segunda-pantalla`
  (`Conflicts`/`Replaces`), y al conectar la tablet se desinstala sola la app
  anterior, que Android considera distinta por tener otro `applicationId`.
- Los ajustes de la versión anterior se recuperan la primera vez que arranca.
- `WireTest` compila ahora contra la clase `Protocol` real de la app Android y
  comprueba que su constante `MAGIC` coincide con la que escribe Python. Al
  renombrar, esa constante se había quedado atrás (está escrita carácter a
  carácter y el reemplazo no la vio); sin esta comprobación, tablet y PC
  habrían dejado de entenderse sin que ninguna prueba dijera nada.

### 1.0.4

- **La 1.0.3 no arrancaba.** Al fijar el framerate en los caps de la fuente,
  pipewiresrc se quedaba sin formatos que ofrecer (`no more input formats`,
  `error set output format: -22`) y ningún pipeline llegaba a PLAYING. Mutter
  anuncia framerate variable porque el stream va guiado por daño, así que no se
  le puede exigir un valor concreto: la cadencia de captura se controla donde
  siempre estuvo bien, en el `refresh-rate` del monitor virtual que se pide por
  D-Bus.
- **Un candidato que falla ya no se lleva la sesión por delante.** El error del
  bus del primer pipeline llegaba al manejador que cierra la sesión, y con ella
  desaparecía el monitor virtual, así que los candidatos siguientes fallaban
  también aunque fueran correctos. Durante la búsqueda esos errores son
  información, no avería.

### 1.0.3

Tres defectos que se notaban como «solo se actualiza si hay mucho movimiento» y
«el puntero solo se ve sobre una ventana». Los tres rompían justo las
actualizaciones pequeñas y aisladas, que es exactamente lo que produce un
puntero cruzando un escritorio quieto:

- **`always-copy=false` en pipewiresrc.** Mutter dibuja en un juego pequeño de
  buffers; si al ir a grabar no encuentra uno libre, no graba y **no reprograma
  nada**, así que ese fotograma se pierde para siempre. Con `always-copy=false`
  el buffer recorría toda la cadena antes de devolverse. Ahora se copia de
  entrada y Mutter nunca se queda sin.
- **`videorate` delante de una fuente guiada por daño.** Sustituido por una
  sonda en un pad que decide en el acto: o pasa el fotograma o lo descarta, sin
  guardar nada para después. De paso deja de meter su rango de framerate en la
  negociación de caps.
- (Un tercer cambio de esta versión, fijar el framerate en los caps, resultó
  estar mal y se revirtió en la 1.0.4.)

`test_fluidez.py` mide que un fotograma aislado salga codificado en unos pocos
milisegundos en lugar de esperar al siguiente.

### 1.0.2

- **El puntero, de verdad esta vez.** El problema no era ninguna propiedad de
  `RecordVirtual` sino la negociación de buffers: pedir `memory:DMABuf` hace que
  Mutter sirva el fotograma por el camino que, para monitores virtuales, no
  dibuja el cursor. Con el puntero activado ya no se ofrece ese pipeline. Hay un
  test (`test_puntero.py`) que lo fija para que no vuelva.
- El diagnóstico ahora compara los dos caminos de captura, que es el eje que
  importa de verdad.

### 1.0.1

- **Iniciar volvía a fallar tras Detener.** Cerrar el socket de escucha desde
  otro hilo no despierta a quien está bloqueado en `accept()`, así que el
  puerto seguía ocupado y `bind()` daba *Address already in use*. Ahora hay un
  `socketpair` que hace de timbre, se espera con `select` a los dos a la vez, y
  `stop()` no da por cerrado el servidor hasta que el hilo ha muerto de verdad.
- **La tablet seguía transmitiendo con la ventana parada.** El mismo hilo, al
  volver de `accept()`, no comprobaba si el servidor seguía en marcha y montaba
  una sesión entera igualmente. Ahora se comprueba, y hay un test que lo
  vigila.
- **El puntero no aparecía.** El monitor virtual se declaraba «de plataforma»,
  que es lo que usan las sesiones remotas sin pantalla física. Ahora se declara
  como un monitor normal, y `--diagnostico` resuelve el caso de que tu GNOME se
  comporte distinto.
- **Menos cosas corriendo.** Los hilos se esperan al cerrar en vez de dejarlos
  a su aire, el servidor `adb` se apaga si lo levantamos nosotros, y la app de
  la tablet suelta socket y decodificador al pasar a segundo plano.
- Ajustes recordados entre sesiones, y logo y autoría en la ventana.

### 1.0.0

- Primera versión.
