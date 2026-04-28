package com.smokinghazy.teleproxy

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelChildren
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketException
import java.util.concurrent.atomic.AtomicInteger

/**
 * Local MTProto bridge. Listens on host:port, validates obfuscated2
 * handshakes against the configured secret, and forwards re-encrypted
 * traffic upstream. Tries WebSocket-over-TLS to kws<dc>.web.telegram.org
 * first (this is what bypasses ISP DPI / DC blocking — it lands on
 * Cloudflare). Falls back to direct TCP to the DC IP when WS fails.
 */
class ProxyServer(
    private val cfg: ProxyConfig,
    private val onLog: (String) -> Unit,
    private val onError: (String) -> Unit,
) {
    private var server: ServerSocket? = null
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val activeConns = AtomicInteger(0)

    @Volatile private var running: Boolean = false

    fun isRunning() = running

    fun activeConnections() = activeConns.get()

    fun start() {
        if (running) return
        val secret = try {
            Mtproto.hexDecode(cfg.secret)
        } catch (t: Throwable) {
            onError("Invalid secret: ${t.message}")
            return
        }
        if (secret.size != 16) {
            onError("Secret must be exactly 16 bytes (32 hex chars)")
            return
        }

        val s = try {
            ServerSocket().apply {
                reuseAddress = true
                bind(InetSocketAddress(InetAddress.getByName(cfg.host), cfg.port))
            }
        } catch (t: Throwable) {
            onError("Bind failed (${cfg.host}:${cfg.port}): ${t.message}")
            return
        }
        server = s
        running = true
        onLog("Proxy listening on ${cfg.host}:${cfg.port} (secret=${cfg.secret})")
        onLog("Telegram link: ${cfg.toLink()}")

        scope.launch { acceptLoop(s, secret) }
    }

    fun stop() {
        if (!running) return
        running = false
        try { server?.close() } catch (_: Throwable) {}
        server = null
        scope.coroutineContext.cancelChildren()
        onLog("Proxy stopped")
    }

    private suspend fun acceptLoop(s: ServerSocket, secret: ByteArray) =
        withContext(Dispatchers.IO) {
            while (isActive && running) {
                val client = try {
                    s.accept()
                } catch (_: SocketException) {
                    return@withContext
                } catch (t: Throwable) {
                    onLog("accept() error: ${t.message}")
                    delay(50)
                    continue
                }
                scope.launch { handleClient(client, secret) }
            }
        }

    private suspend fun handleClient(client: Socket, secret: ByteArray) =
        withContext(Dispatchers.IO) {
            val label = "${client.inetAddress.hostAddress}:${client.port}"
            activeConns.incrementAndGet()
            client.tcpNoDelay = true
            try {
                val cIn = client.getInputStream()
                val cOut = client.getOutputStream()

                val handshake = ByteArray(Mtproto.HANDSHAKE_LEN)
                if (!readFully(cIn, handshake)) {
                    onLog("[$label] disconnected before handshake")
                    return@withContext
                }
                val hs = Mtproto.tryHandshake(handshake, secret) ?: run {
                    onLog("[$label] bad handshake (wrong secret or proto)")
                    drainSilently(cIn)
                    return@withContext
                }
                val mediaTag = if (hs.isMedia) "m" else ""
                val dcIdx = if (hs.isMedia) -hs.dc else hs.dc
                val relayInit = Mtproto.generateRelayInit(hs.protoTag, dcIdx)
                val ctx = Mtproto.buildCtx(hs.decPrekeyAndIv, secret, relayInit)
                val protoInt = (hs.protoTag[0].toLong() and 0xff) or
                    ((hs.protoTag[1].toLong() and 0xff) shl 8) or
                    ((hs.protoTag[2].toLong() and 0xff) shl 16) or
                    ((hs.protoTag[3].toLong() and 0xff) shl 24)

                onLog("[$label] handshake ok DC${hs.dc}$mediaTag")

                if (cfg.useWs) {
                    val ws = tryWs(label, hs, relayInit)
                    if (ws != null) {
                        bridgeWs(label, cIn, cOut, ws, ctx, relayInit, protoInt)
                        return@withContext
                    }
                    onLog("[$label] WS path failed; falling back to direct TCP")
                }

                val dcIp = cfg.dcIps[hs.dc] ?: Mtproto.DEFAULT_DC_IPS[hs.dc]
                if (dcIp == null) { onLog("[$label] unknown DC ${hs.dc}"); return@withContext }
                bridgeTcp(label, cIn, cOut, dcIp, relayInit, ctx)
            } catch (t: Throwable) {
                onLog("[$label] error: ${t.message}")
            } finally {
                activeConns.decrementAndGet()
                try { client.close() } catch (_: Throwable) {}
            }
        }

    // ---------- WS attempt ----------
    private fun tryWs(label: String, hs: Mtproto.HandshakeResult, relayInit: ByteArray): RawWs? {
        // SNI=domain points Cloudflare to the right DC; connect target can be
        // either a forced redirect IP (cfg.dcIps[dc]) or the DNS resolution of
        // the kws domain.
        val baseDc = if (hs.dc == 203) 2 else hs.dc
        val mediaTag = if (hs.isMedia) "m" else ""
        val domains = if (hs.isMedia) {
            listOf("kws$baseDc-1.web.telegram.org", "kws$baseDc.web.telegram.org")
        } else {
            listOf("kws$baseDc.web.telegram.org", "kws$baseDc-1.web.telegram.org")
        }

        // Targets to attempt: configured redirect IP, then DNS resolution of domain.
        val redirectIp = cfg.dcIps[hs.dc]
        val targets = ArrayList<String>().apply {
            if (!redirectIp.isNullOrBlank()) add(redirectIp)
        }
        for (d in domains) {
            try {
                for (a in InetAddress.getAllByName(d)) targets.add(a.hostAddress)
            } catch (_: Throwable) { /* DNS fail */ }
        }
        if (targets.isEmpty()) {
            onLog("[$label] DC${hs.dc}$mediaTag no WS targets resolvable")
            return null
        }

        for (target in targets.distinct()) {
            for (domain in domains) {
                try {
                    onLog("[$label] WS try wss://$domain/apiws via $target")
                    val ws = RawWs.connect(target, domain, timeoutMs = 8000)
                    onLog("[$label] WS connected via $target ($domain)")
                    // first frame: relay_init unmodified
                    ws.sendBinary(relayInit)
                    return ws
                } catch (e: WsHandshakeError) {
                    onLog("[$label] WS $target/$domain → HTTP ${e.code}")
                } catch (t: Throwable) {
                    onLog("[$label] WS $target/$domain → ${t.message}")
                }
            }
        }
        return null
    }

    // ---------- Direct TCP bridge ----------
    private suspend fun bridgeTcp(
        label: String, cIn: java.io.InputStream, cOut: java.io.OutputStream,
        dcIp: String, relayInit: ByteArray, ctx: Mtproto.Ctx,
    ) = withContext(Dispatchers.IO) {
        val remote = Socket()
        try {
            remote.connect(InetSocketAddress(dcIp, Mtproto.DC_PORT), 10_000)
        } catch (t: Throwable) {
            onLog("[$label] connect $dcIp failed: ${t.message}")
            return@withContext
        }
        remote.tcpNoDelay = true
        val rIn = remote.getInputStream()
        val rOut = remote.getOutputStream()
        rOut.write(relayInit); rOut.flush()
        onLog("[$label] TCP bridge -> $dcIp:${Mtproto.DC_PORT}")

        val up = scope.launch {
            pump(cIn, rOut) { chunk -> ctx.tgEnc.update(ctx.cltDec.update(chunk)) }
            try { remote.shutdownOutput() } catch (_: Throwable) {}
        }
        val down = scope.launch {
            pump(rIn, cOut) { chunk -> ctx.cltEnc.update(ctx.tgDec.update(chunk)) }
        }
        try { up.join(); down.join() }
        catch (_: CancellationException) {}
        finally {
            try { remote.close() } catch (_: Throwable) {}
            onLog("[$label] TCP session closed")
        }
    }

    // ---------- WS bridge (re-encrypt + framing) ----------
    private suspend fun bridgeWs(
        label: String,
        cIn: java.io.InputStream, cOut: java.io.OutputStream,
        ws: RawWs, ctx: Mtproto.Ctx, relayInit: ByteArray, protoInt: Long,
    ) = withContext(Dispatchers.IO) {
        val splitter: MsgSplitter? = try { MsgSplitter(relayInit, protoInt) }
            catch (_: Throwable) { null }

        val up = scope.launch {
            val buf = ByteArray(64 * 1024)
            try {
                while (isActive) {
                    val n = try { cIn.read(buf) } catch (_: IOException) { -1 }
                    if (n <= 0) {
                        if (splitter != null) {
                            val tail = splitter.flush()
                            if (tail.isNotEmpty()) {
                                try { ws.sendBatch(tail) } catch (_: Throwable) {}
                            }
                        }
                        break
                    }
                    val chunk = if (n == buf.size) buf.copyOf() else buf.copyOf(n)
                    val plain = ctx.cltDec.update(chunk)
                    val enc = ctx.tgEnc.update(plain)
                    if (splitter != null) {
                        val parts = splitter.split(enc)
                        if (parts.isEmpty()) continue
                        try { ws.sendBatch(parts) } catch (_: Throwable) { break }
                    } else {
                        try { ws.sendBinary(enc) } catch (_: Throwable) { break }
                    }
                }
            } catch (_: Throwable) {}
            try { ws.close() } catch (_: Throwable) {}
        }
        val down = scope.launch {
            try {
                while (isActive) {
                    val frame = ws.recv() ?: break
                    val plain = ctx.tgDec.update(frame)
                    val toClient = ctx.cltEnc.update(plain)
                    try { cOut.write(toClient); cOut.flush() }
                    catch (_: IOException) { break }
                }
            } catch (_: Throwable) {}
        }
        try { up.join(); down.join() }
        catch (_: CancellationException) {}
        finally {
            try { ws.close() } catch (_: Throwable) {}
            onLog("[$label] WS session closed")
        }
    }

    private suspend fun pump(
        input: java.io.InputStream, output: java.io.OutputStream,
        transform: (ByteArray) -> ByteArray,
    ) = withContext(Dispatchers.IO) {
        val buf = ByteArray(64 * 1024)
        while (isActive) {
            val n = try { input.read(buf) } catch (_: IOException) { -1 }
            if (n <= 0) break
            val chunk = if (n == buf.size) buf.copyOf() else buf.copyOf(n)
            val out = try { transform(chunk) } catch (t: Throwable) {
                onLog("transform error: ${t.message}"); break
            }
            if (out.isNotEmpty()) {
                try { output.write(out); output.flush() } catch (_: IOException) { break }
            }
        }
    }

    private fun readFully(input: java.io.InputStream, dst: ByteArray): Boolean {
        var off = 0
        while (off < dst.size) {
            val n = try { input.read(dst, off, dst.size - off) } catch (_: IOException) { -1 }
            if (n <= 0) return false
            off += n
        }
        return true
    }

    private fun drainSilently(input: java.io.InputStream) {
        val buf = ByteArray(4096)
        try { while (input.read(buf) > 0) {} } catch (_: Throwable) {}
    }

    fun shutdown() {
        stop()
        scope.cancel()
    }
}
