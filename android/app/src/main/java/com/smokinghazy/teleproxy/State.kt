package com.smokinghazy.teleproxy

import androidx.compose.runtime.mutableStateOf
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Lightweight singleton observable state, shared between UI and service. */
object State {
    val running = mutableStateOf(false)
    val cfg = mutableStateOf(ProxyConfig())
    val log = mutableStateOf<List<String>>(emptyList())

    private val tsFmt = SimpleDateFormat("HH:mm:ss", Locale.US)
    private const val MAX_LINES = 600

    @Synchronized
    fun appendLog(line: String) {
        val stamped = "[${tsFmt.format(Date())}] $line"
        val cur = log.value
        val next = if (cur.size >= MAX_LINES)
            (cur.drop(cur.size - MAX_LINES + 1) + stamped)
        else cur + stamped
        log.value = next
    }

    @Synchronized
    fun clearLog() { log.value = emptyList() }
}
