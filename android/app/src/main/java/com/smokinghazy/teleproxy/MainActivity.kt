package com.smokinghazy.teleproxy

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.ContextCompat

class MainActivity : ComponentActivity() {

    private val notifPerm = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { /* ignore */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        State.cfg.value = ConfigStore.load(this)

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }

        setContent {
            TeleproxyTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = TPColors.bg) {
                    AppScreen()
                }
            }
        }
    }
}

object TPColors {
    val bg = Color(0xFF1A1630)
    val card = Color(0xFF24203D)
    val cardEdge = Color(0xFF332C57)
    val accent = Color(0xFFBD93F9)
    val accent2 = Color(0xFF8E7DD0)
    val text = Color(0xFFE6E2F5)
    val mute = Color(0xFF9C95C0)
    val warning = Color(0xFFFFB86C)
    val success = Color(0xFF50FA7B)
    val danger = Color(0xFFFF5577)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppScreen() {
    val ctx = LocalContext.current
    val cfg = State.cfg.value
    val running = State.running.value

    var host by remember(cfg.host) { mutableStateOf(cfg.host) }
    var port by remember(cfg.port) { mutableStateOf(cfg.port.toString()) }
    var secret by remember(cfg.secret) { mutableStateOf(cfg.secret) }
    var dc2 by remember(cfg) { mutableStateOf(cfg.dcIps[2] ?: "149.154.167.220") }
    var dc4 by remember(cfg) { mutableStateOf(cfg.dcIps[4] ?: "149.154.167.220") }
    var useWs by remember(cfg) { mutableStateOf(cfg.useWs) }

    val dirty by remember {
        derivedStateOf {
            host != State.cfg.value.host ||
                port.toIntOrNull() != State.cfg.value.port ||
                secret != State.cfg.value.secret ||
                dc2 != (State.cfg.value.dcIps[2] ?: "") ||
                dc4 != (State.cfg.value.dcIps[4] ?: "") ||
                useWs != State.cfg.value.useWs
        }
    }

    val link = remember(host, port, secret) {
        val p = port.toIntOrNull() ?: 1080
        "tg://proxy?server=$host&port=$p&secret=dd$secret"
    }

    fun snapshot(): ProxyConfig {
        val p = port.toIntOrNull() ?: 1080
        val ips = mapOf(2 to dc2, 4 to dc4)
        return ProxyConfig(host = host, port = p, secret = secret, dcIps = ips, useWs = useWs)
    }

    fun applyAndRestart() {
        val newCfg = snapshot()
        if (!newCfg.isValid()) {
            State.appendLog("Invalid config — secret must be 32 hex chars")
            return
        }
        ConfigStore.save(ctx, newCfg)
        State.cfg.value = newCfg
        ContextCompat.startForegroundService(ctx, ProxyService.intentRestart(ctx))
    }

    fun startStop() {
        if (running) {
            ContextCompat.startForegroundService(ctx, ProxyService.intentStop(ctx))
        } else {
            val newCfg = snapshot()
            if (!newCfg.isValid()) {
                State.appendLog("Invalid config — secret must be 32 hex chars")
                return
            }
            ConfigStore.save(ctx, newCfg)
            State.cfg.value = newCfg
            ContextCompat.startForegroundService(ctx, ProxyService.intentStart(ctx))
        }
    }

    fun openInTelegram() {
        val newCfg = snapshot()
        if (newCfg.isValid()) {
            ConfigStore.save(ctx, newCfg)
            State.cfg.value = newCfg
            // restart so the running secret matches the link
            ContextCompat.startForegroundService(ctx, ProxyService.intentRestart(ctx))
        }
        try {
            ctx.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(link)).apply {
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            })
        } catch (_: Throwable) {
            State.appendLog("Telegram client not installed?")
        }
    }

    fun copyLink() {
        val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText("Teleproxy", link))
        State.appendLog("Link copied to clipboard")
    }

    Column(Modifier.fillMaxSize()) {
        TopAppBar(
            title = {
                Column {
                    Text("Teleproxy", color = TPColors.accent,
                        fontSize = 22.sp, fontWeight = FontWeight.Bold)
                    Text("by smokinghazy", color = TPColors.mute, fontSize = 12.sp)
                }
            },
            colors = TopAppBarDefaults.topAppBarColors(
                containerColor = TPColors.bg,
                titleContentColor = TPColors.text,
            )
        )

        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 8.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            // --- status row ---
            Card(
                colors = CardDefaults.cardColors(containerColor = TPColors.card),
                shape = RoundedCornerShape(14.dp),
            ) {
                Row(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(16.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Box(
                        Modifier
                            .background(
                                if (running) TPColors.success else TPColors.danger,
                                RoundedCornerShape(8.dp))
                            .padding(horizontal = 10.dp, vertical = 4.dp)
                    ) {
                        Text(
                            if (running) "RUNNING" else "STOPPED",
                            color = Color(0xFF1A1630),
                            fontWeight = FontWeight.Bold,
                            fontSize = 12.sp,
                        )
                    }
                    Spacer(Modifier.width(12.dp))
                    Text(
                        "${State.cfg.value.host}:${State.cfg.value.port}",
                        color = TPColors.text, fontSize = 16.sp,
                        fontFamily = FontFamily.Monospace,
                    )
                }
            }

            if (dirty && running) {
                Card(
                    colors = CardDefaults.cardColors(containerColor = Color(0xFF3F3520)),
                    shape = RoundedCornerShape(12.dp),
                ) {
                    Text(
                        "⚠ Настройки изменены. Нажмите «Применить и добавить в Telegram», " +
                            "чтобы клиент Telegram использовал новый секрет.",
                        modifier = Modifier.padding(14.dp),
                        color = TPColors.warning,
                        fontSize = 13.sp,
                    )
                }
            }

            // --- settings card ---
            Card(
                colors = CardDefaults.cardColors(containerColor = TPColors.card),
                shape = RoundedCornerShape(14.dp),
            ) {
                Column(Modifier.padding(16.dp)) {
                    Text("Настройки", color = TPColors.accent,
                        fontWeight = FontWeight.SemiBold, fontSize = 14.sp)
                    Spacer(Modifier.height(8.dp))
                    Row {
                        OutlinedTextField(
                            value = host, onValueChange = { host = it.trim() },
                            label = { Text("Host") },
                            singleLine = true,
                            modifier = Modifier.weight(2f),
                            colors = tfColors(),
                        )
                        Spacer(Modifier.width(8.dp))
                        OutlinedTextField(
                            value = port,
                            onValueChange = { port = it.filter { c -> c.isDigit() }.take(5) },
                            label = { Text("Port") },
                            singleLine = true,
                            modifier = Modifier.weight(1f),
                            colors = tfColors(),
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    OutlinedTextField(
                        value = secret,
                        onValueChange = { secret = it.lowercase()
                            .filter { c -> c.isDigit() || c in 'a'..'f' }.take(32) },
                        label = { Text("Secret (32 hex)") },
                        singleLine = true,
                        trailingIcon = {
                            IconButton(onClick = { secret = Mtproto.randomSecret() }) {
                                Icon(Icons.Default.Refresh, "regen", tint = TPColors.accent)
                            }
                        },
                        modifier = Modifier.fillMaxWidth(),
                        colors = tfColors(),
                    )
                    Spacer(Modifier.height(8.dp))
                    Row {
                        OutlinedTextField(
                            value = dc2, onValueChange = { dc2 = it.trim() },
                            label = { Text("DC2 IP") },
                            singleLine = true, modifier = Modifier.weight(1f),
                            colors = tfColors(),
                        )
                        Spacer(Modifier.width(8.dp))
                        OutlinedTextField(
                            value = dc4, onValueChange = { dc4 = it.trim() },
                            label = { Text("DC4 IP") },
                            singleLine = true, modifier = Modifier.weight(1f),
                            colors = tfColors(),
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        androidx.compose.material3.Switch(
                            checked = useWs,
                            onCheckedChange = { useWs = it },
                            colors = androidx.compose.material3.SwitchDefaults.colors(
                                checkedThumbColor = TPColors.accent,
                                checkedTrackColor = TPColors.accent2,
                            ),
                        )
                        Spacer(Modifier.width(10.dp))
                        Column {
                            Text("WebSocket через Cloudflare",
                                color = TPColors.text, fontSize = 14.sp,
                                fontWeight = FontWeight.SemiBold)
                            Text("обход блокировок DC IP (рекомендуется)",
                                color = TPColors.mute, fontSize = 11.sp)
                        }
                    }
                }
            }

            // --- link card ---
            Card(
                colors = CardDefaults.cardColors(containerColor = TPColors.card),
                shape = RoundedCornerShape(14.dp),
            ) {
                Row(
                    Modifier
                        .fillMaxWidth()
                        .padding(14.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        link,
                        color = TPColors.text,
                        fontSize = 12.sp,
                        fontFamily = FontFamily.Monospace,
                        modifier = Modifier.weight(1f),
                    )
                    IconButton(onClick = { copyLink() }) {
                        Icon(Icons.Default.ContentCopy, "copy", tint = TPColors.accent)
                    }
                }
            }

            // --- buttons ---
            Button(
                onClick = { openInTelegram() },
                modifier = Modifier.fillMaxWidth().height(52.dp),
                shape = RoundedCornerShape(12.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = TPColors.accent,
                    contentColor = Color(0xFF1A1630),
                ),
            ) { Text("Применить и добавить в Telegram",
                fontWeight = FontWeight.Bold, fontSize = 15.sp) }

            Row {
                Button(
                    onClick = { startStop() },
                    modifier = Modifier.weight(1f).height(48.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (running) TPColors.danger else TPColors.success,
                        contentColor = Color(0xFF1A1630),
                    ),
                ) { Text(if (running) "Остановить" else "Запустить",
                    fontWeight = FontWeight.SemiBold) }
                Spacer(Modifier.width(8.dp))
                Button(
                    onClick = { applyAndRestart() },
                    modifier = Modifier.weight(1f).height(48.dp),
                    shape = RoundedCornerShape(12.dp),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = TPColors.accent2,
                        contentColor = Color(0xFF1A1630),
                    ),
                ) { Text("Применить", fontWeight = FontWeight.SemiBold) }
            }

            // --- log card ---
            Card(
                colors = CardDefaults.cardColors(containerColor = TPColors.card),
                shape = RoundedCornerShape(14.dp),
            ) {
                Column(Modifier.padding(12.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Лог", color = TPColors.accent, fontSize = 14.sp,
                            fontWeight = FontWeight.SemiBold,
                            modifier = Modifier.weight(1f))
                        TextButton(onClick = { State.clearLog() }) {
                            Text("очистить", color = TPColors.mute, fontSize = 12.sp)
                        }
                    }
                    Spacer(Modifier.height(6.dp))
                    LogList()
                }
            }

            Spacer(Modifier.height(20.dp))
        }
    }
}

@Composable
private fun LogList() {
    val list = State.log.value
    val state = rememberLazyListState()
    LaunchedEffect(list.size) {
        if (list.isNotEmpty()) state.animateScrollToItem(list.size - 1)
    }
    LazyColumn(
        state = state,
        modifier = Modifier
            .fillMaxWidth()
            .height(260.dp)
            .background(Color(0xFF15112A), RoundedCornerShape(8.dp))
            .padding(8.dp),
    ) {
        items(list) { line ->
            Text(
                line, color = TPColors.text, fontSize = 11.sp,
                fontFamily = FontFamily.Monospace,
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun tfColors() = androidx.compose.material3.OutlinedTextFieldDefaults.colors(
    focusedTextColor = TPColors.text,
    unfocusedTextColor = TPColors.text,
    focusedBorderColor = TPColors.accent,
    unfocusedBorderColor = TPColors.cardEdge,
    focusedLabelColor = TPColors.accent,
    unfocusedLabelColor = TPColors.mute,
    cursorColor = TPColors.accent,
)

@Composable
fun TeleproxyTheme(content: @Composable () -> Unit) {
    val colors = androidx.compose.material3.darkColorScheme(
        primary = TPColors.accent,
        onPrimary = Color(0xFF1A1630),
        background = TPColors.bg,
        onBackground = TPColors.text,
        surface = TPColors.card,
        onSurface = TPColors.text,
    )
    MaterialTheme(colorScheme = colors, content = content)
}
