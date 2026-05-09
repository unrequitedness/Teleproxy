# Teleproxy

Локальный MTProto-прокси для Telegram, который маскирует трафик под HTTPS к
`*.web.telegram.org` через Cloudflare и обходит блокировку прямых DC IP.

Поддерживаются две платформы:

| Платформа     | Стек                              | Артефакт                                    |
|---------------|-----------------------------------|---------------------------------------------|
| Windows x64   | Python 3.11 + customtkinter + PyInstaller | `release/Teleproxy-windows-x64.exe`       |
| Android 7.0+  | Kotlin + Jetpack Compose          | `release/Teleproxy-android.apk`             |

> Author: **Nysiusa**

---

## Что это и зачем

Telegram периодически режут на уровне DPI/IP. Этот прокси разворачивает у тебя
локально (`127.0.0.1:1080` или `:1443`) MTProto-сервер: клиент Telegram ходит
на этот локальный адрес, а сам прокси за тебя достает Telegram через WebSocket
поверх TLS к `kws<dc>.web.telegram.org`. Поскольку этот хост обслуживается
Cloudflare (десятки тысяч IP, общий пул с миллионом других сайтов), его не
заблокировать без массового сопутствующего ущерба.

Когда WS не открывается — прокси возвращается к прямому TCP к официальным
DC IP (`149.154.x.x`).

---

## Скриншоты

| Windows | Android |
|---------|---------|
| _будут добавлены_ | _будут добавлены_ |

---

## Быстрый старт

### Windows

1. Скачать `Teleproxy-windows-x64.exe` из релизов.
2. Запустить (Defender может ругнуться — это unsigned PyInstaller-бандл; нажать «подробнее → выполнить в любом случае»).
3. В окне приложения нажать **«Применить и добавить в Telegram»** — ссылка
   `tg://proxy?...` сама откроется в десктопном Telegram, и тот спросит «Включить?»

### Android

1. Скачать `Teleproxy-android.apk` из релизов.
2. Установить (включить «Установка из неизвестных источников» в настройках).
3. Открыть Teleproxy → нажать **«Запустить»**.
4. Нажать **«Применить и добавить в Telegram»** — Telegram-клиент сам подхватит ссылку.

В обоих случаях прокси работает только пока приложение открыто (на Android
это foreground-сервис с уведомлением — Android не убьёт его в фоне).

---

## Конфигурация

В UI можно менять:

- **Host / Port** — адрес локального сокета.
- **Secret** — 32 hex-символа. Кнопка «↻» генерирует случайный.
  ⚠ После любой смены секрета нажимай **«Применить и добавить в Telegram»**, а не
  просто «Запустить» — иначе клиент Telegram продолжит слать старый секрет, и
  прокси будет писать `bad handshake (wrong secret or proto)`.
- **DC IP overrides** — кому из DC принудительно дать конкретный IP (по умолчанию
  DC2 / DC4 → `149.154.167.220`, остальные через DNS).
- **WebSocket через Cloudflare** — переключатель основного режима. Если выключить,
  прокси будет ходить только по прямому TCP (быстрее на «чистой» сети, но не
  обходит блок).

Конфиг хранится:

- Windows: `%APPDATA%\TgWsProxy\config.json`
- Android: `SharedPreferences` приложения

---

## Сборка из исходников

### Windows

```bash
cd desktop
python -m venv .venv
. .venv/bin/activate            # или .venv\Scripts\activate на Windows
pip install -r requirements.txt
# Запуск без сборки:
python teleproxy.py
# Сборка onefile-exe:
pip install pyinstaller==6.10.0
pyinstaller packaging/teleproxy.spec
# артефакт: dist/Teleproxy.exe
```

Требования: Python ≥ 3.10, на Windows нужны системные DLL для tkinter
(идут с официальным python.org-инсталлятором).

### Android

```bash
cd android
./gradlew :app:assembleDebug          # debug-APK в app/build/outputs/apk/debug/
./gradlew :app:assembleRelease        # release-APK (подписан debug-keystore'ом)
```

Требования: JDK 17, Android SDK с `platforms;android-34` и `build-tools;34.0.0`.
Минимальный `compileSdk` = 34, `minSdk` = 24 (Android 7.0+).

---

## Архитектура

### Прокси-движок (общий принцип)

1. Принять TCP-подключение от клиента Telegram.
2. Прочитать 64-байтный obfuscated2-handshake.
3. Расшифровать его ключом `SHA256(prekey || secret)` в режиме AES-256-CTR,
   проверить proto-tag (`0xefefefef` / `0xeeeeeeee` / `0xdddddddd`) и DC index.
4. Сгенерировать собственный 64-байтный `relay_init` для исходящего обфускейта.
5. Открыть upstream — сначала **WSS к `kws<dc>.web.telegram.org/apiws`** через
   Cloudflare, при провале — прямой TCP к DC IP.
6. Запустить два потока re-encrypt:
   - up: `client_ciphertext → cltDec → tgEnc → upstream`
   - down: `upstream → tgDec → cltEnc → client`

См. `desktop/proxy/bridge.py` (Python оригинал) и
`android/app/src/main/java/com/smokinghazy/teleproxy/{Mtproto,ProxyServer,RawWs,MsgSplitter}.kt`
(Kotlin порт) — это 1-в-1 одна и та же логика, отличается только язык.

### Android-специфика

- `ProxyService` — Foreground service с `WAKE_LOCK` partial, чтобы Doze не
  убивал прокси.
- `MainActivity` + Compose — UI на Material3 в фиолетовой палитре.
- Шифрование через `javax.crypto.Cipher("AES/CTR/NoPadding")` (поддерживается
  с Android 4.x; на Android 7+ доступен AES-256 без unlimited-strength patch).

### Windows-специфика

- `customtkinter` для тёмной темы + собственный `utils/glass.py` для
  Mica/Acrylic/BlurBehind backdrop (Win 10/11).
- `pystray` + Pillow для системного трея и иконки окна.
- Закрытие крестика **сворачивает в трей**, не убивает прокси. Полноценный
  выход — пункт «Выход» в меню трея.
- Чекбокс «Запускать с Windows» добавляет запись в
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` с флагом `--minimized`.
- PyInstaller `onefile` режим. На Wine при сборке падает PE-checksum
  (нет `imagehlp.dll`); патч уже учтён в spec-файле.

---

## Лицензия

[MIT](LICENSE)

## Благодарности

В Python-версии используется ядро MTProto-моста, основанное на работе сообщества
энтузиастов; UI-обвязка, Android-порт, упаковка и сопровождение — Nysiusa.
