package com.smokinghazy.teleproxy

import android.content.Context
import androidx.core.content.edit
import org.json.JSONObject

/** User-editable proxy settings. */
data class ProxyConfig(
    val host: String = "127.0.0.1",
    val port: Int = 1080,
    val secret: String = "1c03f9098a1158b93d8eae24d2d52e12",
    val dcIps: Map<Int, String> = mapOf(
        2 to "149.154.167.220",
        4 to "149.154.167.220",
    ),
    val useWs: Boolean = true,
) {
    fun toLink(): String =
        "tg://proxy?server=$host&port=$port&secret=dd$secret"

    fun isValid(): Boolean = try {
        val s = Mtproto.hexDecode(secret)
        s.size == 16 && port in 1..65535 && host.isNotBlank()
    } catch (_: Throwable) { false }
}

object ConfigStore {
    private const val PREFS = "teleproxy"
    private const val KEY = "config"

    fun load(ctx: Context): ProxyConfig {
        val sp = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val raw = sp.getString(KEY, null) ?: return ProxyConfig()
        return try {
            val o = JSONObject(raw)
            val dc = mutableMapOf<Int, String>()
            val arr = o.optJSONObject("dc_ips")
            if (arr != null) {
                arr.keys().forEach { k ->
                    val v = arr.optString(k, "")
                    if (v.isNotBlank()) dc[k.toInt()] = v
                }
            }
            ProxyConfig(
                host = o.optString("host", "127.0.0.1"),
                port = o.optInt("port", 1080),
                secret = o.optString("secret", "1c03f9098a1158b93d8eae24d2d52e12"),
                dcIps = if (dc.isEmpty()) ProxyConfig().dcIps else dc.toMap(),
                useWs = o.optBoolean("use_ws", true),
            )
        } catch (_: Throwable) {
            ProxyConfig()
        }
    }

    fun save(ctx: Context, c: ProxyConfig) {
        val o = JSONObject().apply {
            put("host", c.host)
            put("port", c.port)
            put("secret", c.secret)
            val ips = JSONObject()
            for ((dc, ip) in c.dcIps) ips.put(dc.toString(), ip)
            put("dc_ips", ips)
            put("use_ws", c.useWs)
        }
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit {
            putString(KEY, o.toString())
        }
    }
}
