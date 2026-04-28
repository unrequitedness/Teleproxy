package com.smokinghazy.teleproxy

/**
 * Direct port of proxy/bridge.py:MsgSplitter — splits a TCP byte stream
 * into individual MTProto transport packets so each is sent as one WS
 * binary frame. Decryption inside the splitter is performed with a fresh
 * AES-CTR cipher derived from relay_init (mirrors Python).
 */
class MsgSplitter(relayInit: ByteArray, private val protoInt: Long) {
    private val dec: Mtproto.CtrStream
    private val cipherBuf = ArrayDeque<Byte>(0)
    private val plainBuf = ArrayDeque<Byte>(0)
    private var disabled = false

    init {
        val key = relayInit.copyOfRange(8, 40)
        val iv = relayInit.copyOfRange(40, 56)
        dec = Mtproto.CtrStream(key, iv)
        // skip past 64-byte init like Python (dec.update(ZERO_64))
        dec.update(ByteArray(64))
    }

    fun split(chunk: ByteArray): List<ByteArray> {
        if (chunk.isEmpty()) return emptyList()
        if (disabled) return listOf(chunk)

        for (b in chunk) cipherBuf.addLast(b)
        val plainPart = dec.update(chunk)
        for (b in plainPart) plainBuf.addLast(b)

        val out = ArrayList<ByteArray>()
        while (cipherBuf.isNotEmpty()) {
            val plen = nextPacketLen() ?: break
            if (plen <= 0) {
                out.add(drainAllCipher())
                plainBuf.clear()
                disabled = true
                break
            }
            out.add(takeCipher(plen))
            takePlain(plen)
        }
        return out
    }

    fun flush(): List<ByteArray> {
        if (cipherBuf.isEmpty()) return emptyList()
        val tail = drainAllCipher()
        plainBuf.clear()
        return listOf(tail)
    }

    private fun nextPacketLen(): Int? {
        if (plainBuf.isEmpty()) return null
        return when (protoInt) {
            PROTO_ABRIDGED -> nextAbridgedLen()
            PROTO_INTERMEDIATE, PROTO_PADDED_INTERMEDIATE -> nextIntermediateLen()
            else -> 0
        }
    }

    private fun nextAbridgedLen(): Int? {
        val first = plainBuf.first().toInt() and 0xff
        val payloadLen: Int
        val headerLen: Int
        if (first == 0x7f || first == 0xff) {
            if (plainBuf.size < 4) return null
            val it = plainBuf.iterator()
            it.next() // skip first
            val b1 = it.next().toInt() and 0xff
            val b2 = it.next().toInt() and 0xff
            val b3 = it.next().toInt() and 0xff
            payloadLen = (b1 or (b2 shl 8) or (b3 shl 16)) * 4
            headerLen = 4
        } else {
            payloadLen = (first and 0x7f) * 4
            headerLen = 1
        }
        if (payloadLen <= 0) return 0
        val packetLen = headerLen + payloadLen
        if (plainBuf.size < packetLen) return null
        return packetLen
    }

    private fun nextIntermediateLen(): Int? {
        if (plainBuf.size < 4) return null
        val it = plainBuf.iterator()
        val b0 = it.next().toInt() and 0xff
        val b1 = it.next().toInt() and 0xff
        val b2 = it.next().toInt() and 0xff
        val b3 = it.next().toInt() and 0xff
        val payloadLen = (b0 or (b1 shl 8) or (b2 shl 16) or (b3 shl 24)) and 0x7fff_ffff
        if (payloadLen <= 0) return 0
        val packetLen = 4 + payloadLen
        if (plainBuf.size < packetLen) return null
        return packetLen
    }

    private fun takeCipher(n: Int): ByteArray {
        val out = ByteArray(n)
        for (i in 0 until n) out[i] = cipherBuf.removeFirst()
        return out
    }
    private fun takePlain(n: Int) {
        for (i in 0 until n) plainBuf.removeFirst()
    }
    private fun drainAllCipher(): ByteArray {
        val out = ByteArray(cipherBuf.size)
        var i = 0
        while (cipherBuf.isNotEmpty()) { out[i++] = cipherBuf.removeFirst() }
        return out
    }

    companion object {
        const val PROTO_ABRIDGED = 0xefefefefL
        const val PROTO_INTERMEDIATE = 0xeeeeeeeeL
        const val PROTO_PADDED_INTERMEDIATE = 0xddddddddL
    }
}
