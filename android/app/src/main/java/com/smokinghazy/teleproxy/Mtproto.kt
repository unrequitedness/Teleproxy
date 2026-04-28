package com.smokinghazy.teleproxy

import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.spec.IvParameterSpec
import javax.crypto.spec.SecretKeySpec

/**
 * Obfuscated2 / MTProto handshake helpers — Kotlin port of
 * proxy/tg_ws_proxy.py:_try_handshake, _generate_relay_init,
 * _build_crypto_ctx and proxy/bridge.py:CryptoCtx (TCP-fallback flavour).
 */
object Mtproto {

    const val HANDSHAKE_LEN = 64
    const val SKIP_LEN = 8
    const val PREKEY_LEN = 32
    const val KEY_LEN = 32
    const val IV_LEN = 16
    const val PROTO_TAG_POS = 56
    const val DC_IDX_POS = 60

    // Valid protocol tags (raw little-endian on wire)
    val TAG_ABRIDGED = byteArrayOf(0xef.toByte(), 0xef.toByte(), 0xef.toByte(), 0xef.toByte())
    val TAG_INTERMEDIATE = byteArrayOf(0xee.toByte(), 0xee.toByte(), 0xee.toByte(), 0xee.toByte())
    val TAG_SECURE = byteArrayOf(0xdd.toByte(), 0xdd.toByte(), 0xdd.toByte(), 0xdd.toByte())

    private val RESERVED_FIRST = setOf(0xEF.toByte())
    private val RESERVED_STARTS = listOf(
        byteArrayOf(0x48, 0x45, 0x41, 0x44),                // HEAD
        byteArrayOf(0x50, 0x4F, 0x53, 0x54),                // POST
        byteArrayOf(0x47, 0x45, 0x54, 0x20),                // GET<sp>
        byteArrayOf(0xee.toByte(), 0xee.toByte(), 0xee.toByte(), 0xee.toByte()),
        byteArrayOf(0xdd.toByte(), 0xdd.toByte(), 0xdd.toByte(), 0xdd.toByte()),
        byteArrayOf(0x16, 0x03, 0x01, 0x02),                // TLS 1.0 ClientHello
    )
    private val RESERVED_CONTINUE = byteArrayOf(0, 0, 0, 0)

    private val rng = SecureRandom()

    /**
     * MTProto target DC IPs (mirrors bridge.py:DC_DEFAULT_IPS / dc_redirects).
     * Telegram uses these for the public proxy fallback.
     */
    val DEFAULT_DC_IPS: Map<Int, String> = mapOf(
        1 to "149.154.175.50",
        2 to "149.154.167.220", // dc_redirect from windows.py default
        3 to "149.154.175.100",
        4 to "149.154.167.220", // dc_redirect from windows.py default
        5 to "149.154.171.5",
        203 to "91.105.192.100",
    )
    const val DC_PORT = 443

    // ------------------------------------------------------------------
    // SHA-256 / hex helpers
    // ------------------------------------------------------------------
    fun sha256(vararg parts: ByteArray): ByteArray {
        val md = MessageDigest.getInstance("SHA-256")
        for (p in parts) md.update(p)
        return md.digest()
    }

    fun hexDecode(s: String): ByteArray {
        val cleaned = s.trim().lowercase()
        require(cleaned.length % 2 == 0) { "Secret must be hex (even length)" }
        val out = ByteArray(cleaned.length / 2)
        for (i in out.indices) {
            val hi = Character.digit(cleaned[2 * i], 16)
            val lo = Character.digit(cleaned[2 * i + 1], 16)
            require(hi >= 0 && lo >= 0) { "Bad hex character at index ${2 * i}" }
            out[i] = ((hi shl 4) or lo).toByte()
        }
        return out
    }

    fun hexEncode(b: ByteArray): String {
        val sb = StringBuilder(b.size * 2)
        for (x in b) sb.append("%02x".format(x.toInt() and 0xff))
        return sb.toString()
    }

    fun randomSecret(): String {
        val b = ByteArray(16)
        rng.nextBytes(b)
        return hexEncode(b)
    }

    // ------------------------------------------------------------------
    // AES-256-CTR streaming cipher wrapper.
    //
    // javax.crypto's "AES/CTR/NoPadding" supports streaming update() with
    // 1-byte granularity, matching cryptography.hazmat's behaviour.
    // ------------------------------------------------------------------
    class CtrStream(key: ByteArray, iv: ByteArray) {
        private val cipher: Cipher = Cipher.getInstance("AES/CTR/NoPadding").apply {
            init(Cipher.ENCRYPT_MODE,
                SecretKeySpec(key, "AES"),
                IvParameterSpec(iv))
        }

        /** Streaming update — XORs `data` with the keystream and returns same-length bytes. */
        fun update(data: ByteArray): ByteArray = cipher.update(data) ?: ByteArray(0)

        fun update(data: ByteArray, off: Int, len: Int): ByteArray =
            cipher.update(data, off, len) ?: ByteArray(0)
    }

    // ------------------------------------------------------------------
    // Handshake validation: returns null when the secret/proto don't match.
    // ------------------------------------------------------------------
    data class HandshakeResult(
        val dc: Int,
        val isMedia: Boolean,
        val protoTag: ByteArray,
        val decPrekeyAndIv: ByteArray,   // bytes [8:56] of the encrypted handshake
    )

    fun tryHandshake(handshake: ByteArray, secret: ByteArray): HandshakeResult? {
        require(handshake.size == HANDSHAKE_LEN) { "handshake must be 64 bytes" }

        val decPrekey = handshake.copyOfRange(SKIP_LEN, SKIP_LEN + PREKEY_LEN)
        val decIv = handshake.copyOfRange(SKIP_LEN + PREKEY_LEN, SKIP_LEN + PREKEY_LEN + IV_LEN)
        val decKey = sha256(decPrekey, secret)

        val cipher = CtrStream(decKey, decIv)
        val decrypted = cipher.update(handshake)

        val protoTag = decrypted.copyOfRange(PROTO_TAG_POS, PROTO_TAG_POS + 4)
        val ok = protoTag.contentEquals(TAG_ABRIDGED) ||
                 protoTag.contentEquals(TAG_INTERMEDIATE) ||
                 protoTag.contentEquals(TAG_SECURE)
        if (!ok) return null

        val dcLo = decrypted[DC_IDX_POS].toInt() and 0xff
        val dcHi = decrypted[DC_IDX_POS + 1].toInt() // signed
        val dcIdx = (dcHi shl 8) or dcLo // signed because dcHi is signed
        // Note: Python uses signed little-endian — we already kept the sign of dcHi.

        val dc = kotlin.math.abs(dcIdx)
        val isMedia = dcIdx < 0

        return HandshakeResult(
            dc = dc,
            isMedia = isMedia,
            protoTag = protoTag,
            decPrekeyAndIv = handshake.copyOfRange(SKIP_LEN, SKIP_LEN + PREKEY_LEN + IV_LEN),
        )
    }

    // ------------------------------------------------------------------
    // relay_init generation — analog of _generate_relay_init.
    // ------------------------------------------------------------------
    fun generateRelayInit(protoTag: ByteArray, dcIdx: Int): ByteArray {
        // 1) draw 64 bytes that don't collide with reserved patterns
        val rnd = ByteArray(HANDSHAKE_LEN)
        while (true) {
            rng.nextBytes(rnd)
            if (RESERVED_FIRST.contains(rnd[0])) continue
            var clash = false
            val first4 = rnd.copyOfRange(0, 4)
            for (s in RESERVED_STARTS) {
                if (first4.contentEquals(s)) { clash = true; break }
            }
            if (clash) continue
            val cont = rnd.copyOfRange(4, 8)
            if (cont.contentEquals(RESERVED_CONTINUE)) continue
            break
        }

        // 2) encrypt with key/iv extracted from itself
        val encKey = rnd.copyOfRange(SKIP_LEN, SKIP_LEN + PREKEY_LEN)
        val encIv = rnd.copyOfRange(SKIP_LEN + PREKEY_LEN, SKIP_LEN + PREKEY_LEN + IV_LEN)
        val cipher = CtrStream(encKey, encIv)
        val encryptedFull = cipher.update(rnd)

        // 3) inject proto tag + signed-le dc + 2 random bytes into bytes [56:64]
        val dcLeBytes = byteArrayOf((dcIdx and 0xff).toByte(), ((dcIdx shr 8) and 0xff).toByte())
        val tail2 = ByteArray(2).also { rng.nextBytes(it) }
        val tailPlain = protoTag + dcLeBytes + tail2

        // keystream at [56:64] = encryptedFull[i] xor rnd[i]
        val keystreamTail = ByteArray(8)
        for (i in 0 until 8) {
            keystreamTail[i] = (encryptedFull[56 + i].toInt() xor rnd[56 + i].toInt()).toByte()
        }
        val encTail = ByteArray(8)
        for (i in 0 until 8) {
            encTail[i] = (tailPlain[i].toInt() xor keystreamTail[i].toInt()).toByte()
        }

        val out = rnd.copyOf()
        for (i in 0 until 8) out[PROTO_TAG_POS + i] = encTail[i]
        return out
    }

    // ------------------------------------------------------------------
    // Crypto context — 4 streams, exactly mirroring _build_crypto_ctx.
    // ------------------------------------------------------------------
    class Ctx(
        val cltDec: CtrStream,   // decrypt data coming FROM the client
        val cltEnc: CtrStream,   // encrypt data going TO the client
        val tgEnc: CtrStream,    // encrypt data going TO telegram
        val tgDec: CtrStream,    // decrypt data coming FROM telegram
    )

    private val ZERO_64 = ByteArray(64)

    fun buildCtx(clientDecPrekeyIv: ByteArray, secret: ByteArray, relayInit: ByteArray): Ctx {
        require(clientDecPrekeyIv.size == PREKEY_LEN + IV_LEN)

        // --- client side ---
        val cltDecPrekey = clientDecPrekeyIv.copyOfRange(0, PREKEY_LEN)
        val cltDecIv = clientDecPrekeyIv.copyOfRange(PREKEY_LEN, PREKEY_LEN + IV_LEN)
        val cltDecKey = sha256(cltDecPrekey, secret)

        val reversed = clientDecPrekeyIv.copyOf().also { it.reverse() }
        val cltEncPrekey = reversed.copyOfRange(0, PREKEY_LEN)
        val cltEncIv = reversed.copyOfRange(PREKEY_LEN, PREKEY_LEN + IV_LEN)
        val cltEncKey = sha256(cltEncPrekey, secret)

        val cltDec = CtrStream(cltDecKey, cltDecIv)
        val cltEnc = CtrStream(cltEncKey, cltEncIv)
        // skip past the 64-byte handshake we already consumed in tryHandshake
        cltDec.update(ZERO_64)

        // --- telegram side (raw key, no secret hash) ---
        val relayEncKey = relayInit.copyOfRange(SKIP_LEN, SKIP_LEN + PREKEY_LEN)
        val relayEncIv = relayInit.copyOfRange(SKIP_LEN + PREKEY_LEN, SKIP_LEN + PREKEY_LEN + IV_LEN)

        val relayPair = relayInit.copyOfRange(SKIP_LEN, SKIP_LEN + PREKEY_LEN + IV_LEN)
            .also { it.reverse() }
        val relayDecKey = relayPair.copyOfRange(0, KEY_LEN)
        val relayDecIv = relayPair.copyOfRange(KEY_LEN, KEY_LEN + IV_LEN)

        val tgEnc = CtrStream(relayEncKey, relayEncIv)
        val tgDec = CtrStream(relayDecKey, relayDecIv)
        tgEnc.update(ZERO_64)

        return Ctx(cltDec, cltEnc, tgEnc, tgDec)
    }
}
