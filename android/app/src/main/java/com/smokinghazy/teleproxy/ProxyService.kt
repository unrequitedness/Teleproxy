package com.smokinghazy.teleproxy

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.NotificationCompat

/**
 * Foreground service hosting the local MTProto bridge.
 * Activity drives it via start/stop intents; UI subscribes to [State].
 */
class ProxyService : Service() {

    companion object {
        const val CH_ID = "teleproxy.proxy"
        const val NOTI_ID = 1001
        const val ACTION_START = "teleproxy.start"
        const val ACTION_STOP = "teleproxy.stop"
        const val ACTION_RESTART = "teleproxy.restart"

        fun intentStart(ctx: Context) = Intent(ctx, ProxyService::class.java).apply {
            action = ACTION_START
        }
        fun intentStop(ctx: Context) = Intent(ctx, ProxyService::class.java).apply {
            action = ACTION_STOP
        }
        fun intentRestart(ctx: Context) = Intent(ctx, ProxyService::class.java).apply {
            action = ACTION_RESTART
        }
    }

    private var server: ProxyServer? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> { stopProxy(); stopSelf(); return START_NOT_STICKY }
            ACTION_RESTART -> { stopProxy(); startProxy() }
            else -> startProxy()
        }
        return START_STICKY
    }

    private fun startProxy() {
        // re-read config every start so settings changes apply
        val cfg = ConfigStore.load(this)
        State.cfg.value = cfg
        startForeground(NOTI_ID, buildNotification("Proxy listening on ${cfg.host}:${cfg.port}"))

        if (server?.isRunning() == true) {
            server?.stop()
        }

        val s = ProxyServer(
            cfg = cfg,
            onLog = { line -> State.appendLog(line) },
            onError = { err ->
                State.appendLog("ERROR: $err")
                State.running.value = false
            },
        )
        server = s
        s.start()
        State.running.value = s.isRunning()

        if (wakeLock?.isHeld != true) {
            val pm = getSystemService(POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(
                PowerManager.PARTIAL_WAKE_LOCK,
                "Teleproxy::ProxyService"
            ).apply { setReferenceCounted(false); acquire() }
        }
    }

    private fun stopProxy() {
        server?.shutdown()
        server = null
        State.running.value = false
        try { wakeLock?.release() } catch (_: Throwable) {}
        wakeLock = null
    }

    override fun onDestroy() {
        stopProxy()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(NOTIFICATION_SERVICE) as NotificationManager
            if (mgr.getNotificationChannel(CH_ID) == null) {
                val ch = NotificationChannel(
                    CH_ID, "Teleproxy",
                    NotificationManager.IMPORTANCE_LOW
                ).apply {
                    description = "Local MTProto bridge running"
                    setShowBadge(false)
                }
                mgr.createNotificationChannel(ch)
            }
        }
    }

    private fun buildNotification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1, intentStop(this),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CH_ID)
            .setSmallIcon(R.drawable.ic_status)
            .setContentTitle("Teleproxy")
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }
}
