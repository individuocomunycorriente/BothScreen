package cl.danko.bothscreen;

import android.app.Activity;
import android.content.pm.ActivityInfo;
import android.graphics.Point;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Display;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.WindowManager;
import android.widget.TextView;

import java.util.Locale;

/**
 * Pantalla completa, sin nada más: un SurfaceView que recibe el vídeo y un HUD
 * discreto con el estado. Un toque en la pantalla muestra u oculta el HUD.
 *
 * El hilo de red solo existe mientras la actividad está visible Y hay un
 * Surface donde pintar. En cuanto la app pasa a segundo plano se cierra el
 * socket y se libera el decodificador: nada de seguir decodificando vídeo con
 * la app escondida.
 */
public class MainActivity extends Activity implements SurfaceHolder.Callback,
        StreamClient.Listener {

    private static final String TAG = "BothScreen";

    /** Tras este rato sin transmitir se deja que la tablet se apague sola. */
    private static final long PANTALLA_ENCENDIDA_TIMEOUT_MS = 90_000L;

    private SurfaceView surfaceView;
    private TextView hud;
    private StreamClient client;
    private final Handler ui = new Handler(Looper.getMainLooper());

    private int port = Protocol.DEFAULT_PORT;
    private String streamInfo = "";
    private boolean hudVisible = true;

    private boolean surfaceReady;
    private boolean inForeground;

    private final Runnable hideHud = new Runnable() {
        @Override
        public void run() {
            if (hud != null && streamInfo.length() > 0) {
                hud.setVisibility(View.GONE);
                hudVisible = false;
            }
        }
    };

    private final Runnable releaseScreenOn = new Runnable() {
        @Override
        public void run() {
            getWindow().clearFlags(
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            Log.i(TAG, "sin transmisión: la tablet ya puede apagar la pantalla");
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setRequestedOrientation(ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE);
        setContentView(R.layout.activity_main);

        surfaceView = (SurfaceView) findViewById(R.id.surface);
        hud = (TextView) findViewById(R.id.hud);
        surfaceView.getHolder().addCallback(this);

        if (getIntent() != null && getIntent().hasExtra("port")) {
            port = getIntent().getIntExtra("port", Protocol.DEFAULT_PORT);
        }

        findViewById(R.id.root).setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                toggleHud();
            }
        });

        setStatus("Esperando al PC…");
        goImmersive();
    }

    // ------------------------------------------------------------ ciclo de vida
    @Override
    protected void onStart() {
        super.onStart();
        inForeground = true;
        maybeStartClient();
    }

    @Override
    protected void onStop() {
        super.onStop();
        inForeground = false;
        stopClient();
    }

    @Override
    protected void onDestroy() {
        super.onDestroy();
        ui.removeCallbacksAndMessages(null);
        stopClient();
    }

    /** Arranca el hilo de red solo si hay dónde pintar y la app está visible. */
    private synchronized void maybeStartClient() {
        if (client != null || !surfaceReady || !inForeground) {
            return;
        }
        SurfaceHolder holder = surfaceView.getHolder();
        if (holder.getSurface() == null || !holder.getSurface().isValid()) {
            return;
        }
        Point size = realDisplaySize();
        int w = Math.max(size.x, size.y);
        int h = Math.min(size.x, size.y);
        int fps = Math.round(refreshRate());

        client = new StreamClient(holder.getSurface(), port, w, h, fps, this);
        client.start();
        Log.i(TAG, "cliente arrancado (" + w + "x" + h + " @" + fps + ")");
    }

    private synchronized void stopClient() {
        if (client == null) {
            return;
        }
        StreamClient dying = client;
        client = null;
        dying.shutdown();
        try {
            // Espera corta: lo justo para que suelte socket y decodificador
            // antes de que el sistema nos congele.
            dying.join(1500);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        Log.i(TAG, "cliente detenido");
        ui.removeCallbacks(releaseScreenOn);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
    }

    private void toggleHud() {
        hudVisible = !hudVisible;
        hud.setVisibility(hudVisible ? View.VISIBLE : View.GONE);
        if (hudVisible) {
            ui.removeCallbacks(hideHud);
            ui.postDelayed(hideHud, 4000);
        }
    }

    private void goImmersive() {
        View decor = getWindow().getDecorView();
        decor.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY);
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            goImmersive();
        }
    }

    // ------------------------------------------------------------- surface
    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        surfaceReady = true;
        maybeStartClient();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
        // El servidor negocia la resolución con la relación de aspecto del
        // panel, así que el Surface siempre encaja sin recortes.
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        surfaceReady = false;
        stopClient();
    }

    private Point realDisplaySize() {
        Display display = getWindowManager().getDefaultDisplay();
        Point point = new Point();
        if (Build.VERSION.SDK_INT >= 17) {
            display.getRealSize(point);
        } else {
            display.getSize(point);
        }
        return point;
    }

    private float refreshRate() {
        float rate = getWindowManager().getDefaultDisplay().getRefreshRate();
        if (rate < 24f || rate > 240f) {
            return 60f;
        }
        return rate;
    }

    // -------------------------------------------------------- StreamClient
    @Override
    public void onStatus(final String status) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                setStatus(status);
            }
        });
    }

    private void setStatus(String status) {
        if (status == null || status.length() == 0) {
            hud.setText(streamInfo);
        } else {
            hud.setText(status);
            hud.setVisibility(View.VISIBLE);
            hudVisible = true;
            ui.removeCallbacks(hideHud);
        }
    }

    @Override
    public void onStreamStarted(final int codec, final int width, final int height,
                                final int fps) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                // Mantener la pantalla encendida solo mientras hay vídeo.
                ui.removeCallbacks(releaseScreenOn);
                getWindow().addFlags(
                        WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
                streamInfo = String.format(Locale.US, "%s  %dx%d @%d",
                        Protocol.nameFor(codec), width, height, fps);
                hud.setText(streamInfo);
                hud.setVisibility(View.VISIBLE);
                hudVisible = true;
                ui.removeCallbacks(hideHud);
                ui.postDelayed(hideHud, 4000);
            }
        });
    }

    @Override
    public void onStats(final int bitrateKbps, final float fps, final long frames) {
        ui.post(new Runnable() {
            @Override
            public void run() {
                if (!hudVisible) {
                    return;
                }
                hud.setText(String.format(Locale.US, "%s   %.1f Mbps   %.0f fps",
                        streamInfo, bitrateKbps / 1000f, fps));
            }
        });
    }

    @Override
    public void onStreamStopped() {
        ui.post(new Runnable() {
            @Override
            public void run() {
                streamInfo = "";
                setStatus("Esperando al PC…");
                ui.removeCallbacks(releaseScreenOn);
                ui.postDelayed(releaseScreenOn, PANTALLA_ENCENDIDA_TIMEOUT_MS);
            }
        });
    }
}
