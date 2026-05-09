package com.smokinghazy.teleproxy

/**
 * Direct port of proxy/bridge.py:MsgSplitter — splits a TCP byte stream
 * into individual MTProto transport packets so each is sent as one WS
 * binary frame. Decryption inside the splitter is performed with a fresh
 * AES-CTR cipher derived from relay_init (mirrors Python).
 *
 * Performance: keeps cipher and plaintext bytes in raw [ByteArray] backing
 * stores with head/tail indices; never boxes individual bytes (the original
 * `ArrayDeque<Byte>` implementation pinned the GC under chatty traffic and
 * was the main reason for ping spikes up to several seconds).
 */
class MsgSplitter(relayInit: ByteArray, private val protoInt: Long) {
    private val dec: Mtproto.CtrStream

    // Two parallel ring-ish buffers. cipherBuf and plainBuf grow only on
    // dec.update() calls and shrink on packet emission, so a head index +
    // periodic compaction is enough — no need for a full ring.
    private var cipherBuf: ByteArray = ByteArray(INIT_CAP)
    private var cipherHead = 0
    private var cipherTail = 0   // exclusive

    private var plainBuf: ByteArray = ByteArray(INIT_CAP)
    private var plainHead = 0
    private var plainTail = 0

    private var disabled = false

    init {
        val key = relayInit.copyOfRange(8, 40)
        val iv = relayInit.copyOfRange(40, 56)
        dec = Mtproto.CtrStream(key, iv)
        // skip past 64-byte init (mirrors Python dec.update(ZERO_64))
        dec.update(ByteArray(64))
    }

    private fun cipherSize() = cipherTail - cipherHead
    private fun plainSize() = plainTail - plainHead

    private fun ensureCipherCap(extra: Int) {
        if (cipherTail + extra <= cipherBuf.size) return
        val used = cipherSize()
        if (used + extra <= cipherBuf.size) {
            // Compact in place.
            System.arraycopy(cipherBuf, cipherHead, cipherBuf, 0, used)
            cipherHead = 0
            cipherTail = used
            return
        }
        var cap = cipherBuf.size
        while (cap < used + extra) cap *= 2
        val nb = ByteArray(cap)
        System.arraycopy(cipherBuf, cipherHead, nb, 0, used)
        cipherBuf = nb
        cipherHead = 0
        cipherTail = used
    }

    private fun ensurePlainCap(extra: Int) {
        if (plainTail + extra <= plainBuf.size) return
        val used = plainSize()
        if (used + extra <= plainBuf.size) {
            System.arraycopy(plainBuf, plainHead, plainBuf, 0, used)
            plainHead = 0
            plainTail = used
            return
        }
        var cap = plainBuf.size
        while (cap < used + extra) cap *= 2
        val nb = ByteArray(cap)
        System.arraycopy(plainBuf, plainHead, nb, 0, used)
        plainBuf = nb
        plainHead = 0
        plainTail = used
    }

    fun split(chunk: ByteArray): List<ByteArray> {
        if (chunk.isEmpty()) return emptyList()
        if (disabled) return listOf(chunk)

        ensureCipherCap(chunk.size)
        System.arraycopy(chunk, 0, cipherBuf, cipherTail, chunk.size)
        cipherTail += chunk.size

        val plainPart = dec.update(chunk)
        ensurePlainCap(plainPart.size)
        System.arraycopy(plainPart, 0, plainBuf, plainTail, plainPart.size)
        plainTail += plainPart.size

        val out = ArrayList<ByteArray>()
        while (cipherSize() > 0) {
            val plen = nextPacketLen() ?: break
            if (plen <= 0) {
                out.add(drainAllCipher())
                plainHead = plainTail   // clear plain buf
                disabled = true
                break
            }
            out.add(takeCipher(plen))
            plainHead += plen
            // Periodic compaction when head crawls far.
            if (plainHead > 8 * 1024 && plainHead * 2 > plainBuf.size) {
                System.arraycopy(plainBuf, plainHead, plainBuf, 0, plainSize())
                plainTail -= plainHead
                plainHead = 0
            }
            if (cipherHead > 8 * 1024 && cipherHead * 2 > cipherBuf.size) {
                System.arraycopy(cipherBuf, cipherHead, cipherBuf, 0, cipherSize())
                cipherTail -= cipherHead
                cipherHead = 0
            }
        }
        return out
    }

    fun flush(): List<ByteArray> {
        if (cipherSize() == 0) return emptyList()
        val tail = drainAllCipher()
        plainHead = plainTail
        return listOf(tail)
    }

    private fun nextPacketLen(): Int? {
        if (plainSize() == 0) return null
        return when (protoInt) {
            PROTO_ABRIDGED -> nextAbridgedLen()
            PROTO_INTERMEDIATE, PROTO_PADDED_INTERMEDIATE -> nextIntermediateLen()
            else -> 0
        }
    }

    private fun nextAbridgedLen(): Int? {
        val first = plainBuf[plainHead].toInt() and 0xff
        val payloadLen: Int
        val headerLen: Int
        if (first == 0x7f || first == 0xff) {
            if (plainSize() < 4) return null
            val b1 = plainBuf[plainHead + 1].toInt() and 0xff
            val b2 = plainBuf[plainHead + 2].toInt() and 0xff
            val b3 = plainBuf[plainHead + 3].toInt() and 0xff
            payloadLen = (b1 or (b2 shl 8) or (b3 shl 16)) * 4
            headerLen = 4
        } else {
            payloadLen = (first and 0x7f) * 4
            headerLen = 1
        }
        if (payloadLen <= 0) return 0
        val packetLen = headerLen + payloadLen
        if (plainSize() < packetLen) return null
        return packetLen
    }

    private fun nextIntermediateLen(): Int? {
        if (plainSize() < 4) return null
        val b0 = plainBuf[plainHead].toInt() and 0xff
        val b1 = plainBuf[plainHead + 1].toInt() and 0xff
        val b2 = plainBuf[plainHead + 2].toInt() and 0xff
        val b3 = plainBuf[plainHead + 3].toInt() and 0xff
        val payloadLen = (b0 or (b1 shl 8) or (b2 shl 16) or (b3 shl 24)) and 0x7fff_ffff
        if (payloadLen <= 0) return 0
        val packetLen = 4 + payloadLen
        if (plainSize() < packetLen) return null
        return packetLen
    }

    private fun takeCipher(n: Int): ByteArray {
        val out = ByteArray(n)
        System.arraycopy(cipherBuf, cipherHead, out, 0, n)
        cipherHead += n
        return out
    }

    private fun drainAllCipher(): ByteArray {
        val n = cipherSize()
        val out = ByteArray(n)
        System.arraycopy(cipherBuf, cipherHead, out, 0, n)
        cipherHead = cipherTail
        return out
    }

    companion object {
        const val PROTO_ABRIDGED = 0xefefefefL
        const val PROTO_INTERMEDIATE = 0xeeeeeeeeL
        const val PROTO_PADDED_INTERMEDIATE = 0xddddddddL

        private const val INIT_CAP = 64 * 1024
    }
}
