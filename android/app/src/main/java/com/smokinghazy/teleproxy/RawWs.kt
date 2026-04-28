package com.smokinghazy.teleproxy

import java.io.BufferedInputStream
import java.io.DataInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.util.Base64
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLParameters
import javax.net.ssl.SSLSocket
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

/**
 * Direct port of proxy/raw_websocket.py — minimal WebSocket client
 * (RFC 6455 binary frames, client-masked) over TLS to kws<dc>.web.telegram.org.
 * Mirrors the original which disables certificate verification because
 * connections target arbitrary IPs and reuse SNI for routing.
 */
class RawWs private constructor(
    private val socket: SSLSocket,
    private val input: DataInputStream,
    private val output: OutputStream,
) {
    @Volatile private var closed = false
    private val rng = SecureRandom()

    fun isClosed() = closed

    @Synchronized
    fun sendBinary(data: ByteArray) {
        if (closed) throw IOException("ws closed")
        output.write(buildFrame(OP_BINARY, data))
        output.flush()
    }

    @Synchronized
    fun sendBatch(parts: List<ByteArray>) {
        if (closed) throw IOException("ws closed")
        for (p in parts) output.write(buildFrame(OP_BINARY, p))
        output.flush()
    }

    /** Reads next non-control frame; returns null on close. */
    fun recv(): ByteArray? {
        while (!closed) {
            val (op, payload) = readFrame() ?: return null
            when (op) {
                OP_CLOSE -> {
                    closed = true
                    try {
                        output.write(buildFrame(OP_CLOSE,
                            if (payload.size >= 2) payload.copyOfRange(0, 2) else ByteArray(0)))
                        output.flush()
                    } catch (_: Throwable) {}
                    return null
                }
                OP_PING -> {
                    try {
                        output.write(buildFrame(OP_PONG, payload))
                        output.flush()
                    } catch (_: Throwable) {}
                }
                OP_PONG -> { /* ignore */ }
                OP_TEXT, OP_BINARY -> return payload
                else -> { /* unknown opcode, ignore */ }
            }
        }
        return null
    }

    fun close() {
        if (closed) return
        closed = true
        try {
            output.write(buildFrame(OP_CLOSE, ByteArray(0)))
            output.flush()
        } catch (_: Throwable) {}
        try { socket.close() } catch (_: Throwable) {}
    }

    private fun buildFrame(opcode: Int, data: ByteArray): ByteArray {
        val n = data.size
        val fb = (0x80 or opcode).toByte()
        val mask = ByteArray(4).also { rng.nextBytes(it) }
        val payload = ByteArray(n)
        for (i in 0 until n) payload[i] = (data[i].toInt() xor mask[i and 3].toInt()).toByte()

        return when {
            n < 126 -> {
                val out = ByteArray(2 + 4 + n)
                out[0] = fb; out[1] = (0x80 or n).toByte()
                System.arraycopy(mask, 0, out, 2, 4)
                System.arraycopy(payload, 0, out, 6, n)
                out
            }
            n < 65536 -> {
                val out = ByteArray(4 + 4 + n)
                out[0] = fb; out[1] = (0x80 or 126).toByte()
                out[2] = ((n shr 8) and 0xff).toByte()
                out[3] = (n and 0xff).toByte()
                System.arraycopy(mask, 0, out, 4, 4)
                System.arraycopy(payload, 0, out, 8, n)
                out
            }
            else -> {
                val out = ByteArray(10 + 4 + n)
                out[0] = fb; out[1] = (0x80 or 127).toByte()
                val ln = n.toLong()
                for (i in 0 until 8) out[2 + i] = ((ln ushr ((7 - i) * 8)) and 0xff).toByte()
                System.arraycopy(mask, 0, out, 10, 4)
                System.arraycopy(payload, 0, out, 14, n)
                out
            }
        }
    }

    private fun readFrame(): Pair<Int, ByteArray>? {
        return try {
            val b0 = input.readUnsignedByte()
            val b1 = input.readUnsignedByte()
            val opcode = b0 and 0x0f
            var len = b1 and 0x7f
            len = when (len) {
                126 -> input.readUnsignedShort()
                127 -> {
                    val v = input.readLong()
                    if (v > Int.MAX_VALUE.toLong()) return null
                    v.toInt()
                }
                else -> len
            }
            val masked = (b1 and 0x80) != 0
            val mask = if (masked) ByteArray(4).also { input.readFully(it) } else null
            val payload = ByteArray(len)
            input.readFully(payload)
            if (mask != null) {
                for (i in 0 until len) {
                    payload[i] = (payload[i].toInt() xor mask[i and 3].toInt()).toByte()
                }
            }
            opcode to payload
        } catch (_: IOException) {
            null
        }
    }

    companion object {
        const val OP_TEXT = 0x1
        const val OP_BINARY = 0x2
        const val OP_CLOSE = 0x8
        const val OP_PING = 0x9
        const val OP_PONG = 0xA

        private val INSECURE_TRUST_ALL: Array<TrustManager> = arrayOf(object : X509TrustManager {
            override fun checkClientTrusted(p0: Array<out X509Certificate>?, p1: String?) {}
            override fun checkServerTrusted(p0: Array<out X509Certificate>?, p1: String?) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        })

        private val sslFactory: SSLSocketFactory by lazy {
            val ctx = SSLContext.getInstance("TLS")
            ctx.init(null, INSECURE_TRUST_ALL, java.security.SecureRandom())
            ctx.socketFactory
        }

        /**
         * Connect to host:443 (host is normally an IP; SNI = domain) and
         * upgrade to a WebSocket on /apiws.
         */
        fun connect(host: String, domain: String, timeoutMs: Int = 10_000): RawWs {
            val raw = Socket()
            raw.tcpNoDelay = true
            raw.connect(InetSocketAddress(host, 443), timeoutMs)
            val ssl = sslFactory.createSocket(raw, host, 443, true) as SSLSocket
            // Enable SNI = domain (separate from connect target IP)
            val params: SSLParameters = ssl.sslParameters
            params.serverNames = listOf(javax.net.ssl.SNIHostName(domain))
            ssl.sslParameters = params
            ssl.soTimeout = timeoutMs
            ssl.startHandshake()

            val wsKey = Base64.getEncoder().encodeToString(ByteArray(16).also {
                java.security.SecureRandom().nextBytes(it)
            })
            val req = ("GET /apiws HTTP/1.1\r\n" +
                "Host: $domain\r\n" +
                "Upgrade: websocket\r\n" +
                "Connection: Upgrade\r\n" +
                "Sec-WebSocket-Key: $wsKey\r\n" +
                "Sec-WebSocket-Version: 13\r\n" +
                "Sec-WebSocket-Protocol: binary\r\n" +
                "\r\n").toByteArray(Charsets.US_ASCII)
            ssl.outputStream.write(req)
            ssl.outputStream.flush()

            val bin = BufferedInputStream(ssl.inputStream)
            val statusLine = readLine(bin) ?: throw IOException("ws: empty response")
            val parts = statusLine.split(' ', limit = 3)
            val code = parts.getOrNull(1)?.toIntOrNull() ?: 0
            val headers = HashMap<String, String>()
            while (true) {
                val line = readLine(bin) ?: throw IOException("ws: truncated headers")
                if (line.isEmpty()) break
                val idx = line.indexOf(':')
                if (idx > 0) headers[line.substring(0, idx).lowercase().trim()] =
                    line.substring(idx + 1).trim()
            }
            if (code != 101) {
                try { ssl.close() } catch (_: Throwable) {}
                throw WsHandshakeError(code, statusLine, headers["location"])
            }
            // Reset socket read timeout for streaming
            ssl.soTimeout = 0
            return RawWs(ssl, DataInputStream(bin), ssl.outputStream)
        }

        private fun readLine(input: InputStream): String? {
            val sb = StringBuilder()
            while (true) {
                val b = input.read()
                if (b < 0) return if (sb.isEmpty()) null else sb.toString()
                if (b == 0x0d) {
                    val nx = input.read()
                    if (nx == 0x0a) return sb.toString()
                    if (nx >= 0) sb.append(b.toChar()).append(nx.toChar())
                } else if (b == 0x0a) {
                    return sb.toString()
                } else {
                    sb.append(b.toChar())
                }
            }
        }
    }
}

class WsHandshakeError(val code: Int, val statusLine: String, val location: String?) :
    IOException("HTTP $code: $statusLine") {
    val isRedirect: Boolean get() = code in setOf(301, 302, 303, 307, 308)
}
