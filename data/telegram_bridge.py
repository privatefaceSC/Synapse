"""PoC-мост Telegram через Telethon.

Работает как полноценный Telegram-клиент на стороне сервера: принимает
входящие сообщения напрямую из Telegram (без Android-клиента) и пишет их
в обычную модель через record_message.

Опционально: без переменных окружения TELEGRAM_API_ID / TELEGRAM_API_HASH
или без установленного telethon мост молчит, остальное приложение
работает как раньше.

Сессия Telethon = полный доступ к аккаунту. Файл сессии лежит в db/
(в .gitignore). Это PoC для локального запуска с личным аккаунтом.
"""

import asyncio
import base64
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid


logger = logging.getLogger(__name__)

_loop = None
_thread = None
_client = None
_clients = {}
_handler_registered = False
_handler_registered_users = set()
_refresh_task = None  # legacy alias for the owner account
_refresh_tasks = {}
_startup_tasks = {}
_auth_locks = {}
_client_locks = {}
_handler_locks = {}
_sync_locks = {}
_recent_sync_futures = {}
_lifecycle_generation = {}
_auth_retry_after = {}
_file_send_semaphore = None
_file_send_semaphore_loop = None
_pending_file_sends = 0
_pending_file_sends_lock = threading.Lock()
# chat_id диалогов, сообщения из которых мост игнорирует. Сейчас muted-чаты
# не попадают сюда: они синхронизируются с Contact.muted, но не теряются.
_skip_chat_ids = set()
# chat_id диалогов, где в Telegram выключены уведомления. Обновляется
# периодически и точечно через UpdateNotifySettings.
_muted_chat_ids_by_user = {}
_MUTE_CACHE_TTL = 120
# chat_id диалогов, которые в самом Telegram лежат в архиве. Их не
# отбрасываем: сохраняем сообщение и помечаем локальный Contact архивным.
_archived_chat_ids_by_user = {}
_ARCHIVE_CACHE_TTL = 120
# Недавние отправки из веб-панели (chat_id, text, monotonic-время) —
# чтобы не записать их повторно, когда Telegram пришлёт их обратно
# как исходящее событие.
_recent_self_sent = []
# Точные Telegram-id отправленных web-медиа. В отличие от подписи (часто
# пустой) такой ключ не может случайно поглотить нативную отправку пользователя.
_recent_self_sent_ids = []
# chat_id -> typing state. Старый формат float ещё поддерживается ниже:
# {expires: monotonic, authors: {user_id_or_name: display_name}}.
_typing = {}
_recent_sync_at_by_user = {}
_recent_sync_inflight_users = set()
_RECENT_SYNC_INTERVAL = 10
_RECENT_SYNC_DIALOG_LIMIT = 12
_RECENT_SYNC_MESSAGE_LIMIT = 8
_DEFAULT_MEDIA_MAX_MB = 20
_DEFAULT_MEDIA_STORE_MAX_MB = 650
_DEFAULT_CATCHUP_IMAGE_MAX_MB = 8
_CATCHUP_MEDIA_MAX_AGE_SECONDS = 15 * 60
# [последний расчёт monotonic, bytes, поколение записей]
_media_usage_cache = [0.0, 0, 0]
_media_usage_lock = threading.Lock()
_media_reservation_lock = threading.Lock()
_media_reserved_bytes = 0
# Результат проверки «является ли megagroup группой комментариев канала».
# Отрицательный ответ кэшируем ненадолго: связь группы с каналом может
# появиться позже, но делать GetFullChannel на каждое входящее слишком дорого.
_discussion_check_cache = {}
_DISCUSSION_CHECK_TTL = 300
_STATE_TEMPLATE = {
    "phone": None,
    "phone_code_hash": None,
    "code_hint": None,
    "resend_available_at": None,
    "resend_supported": False,
    "resend_method": None,
    "authorized": False,
    "needs_password": False,
    "error": None,
    "last_media_skip": None,
}
_state = dict(_STATE_TEMPLATE)
_states = {}


def _env_api():
    aid = os.environ.get("TELEGRAM_API_ID")
    ah = os.environ.get("TELEGRAM_API_HASH")
    try:
        aid = int(aid) if aid else None
    except ValueError:
        aid = None
    return aid, ah


def _owner_user_id():
    try:
        return int(os.environ.get("TELEGRAM_OWNER_USER_ID", "1"))
    except ValueError:
        return 1


def _env_flag(name, default=True):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "off", "")


def _settings_path():
    """Файл с настройками фильтрации, меняемыми из веб-панели."""
    return os.path.join(os.getcwd(), "db", "tg_settings.json")


def _load_settings() -> dict:
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_settings(data: dict):
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _skip_muted():
    """Muted-чаты всегда синхронизируются с Contact.muted.

    Раньше это было настройкой из UI/env, но теперь это базовое поведение:
    пользователь выключает звук в Telegram или на сайте, а состояние просто
    догоняет вторую сторону.
    """
    return True


def _skip_archived():
    """Складывать ли архивированные Telegram-чаты в локальный архив."""
    s = _load_settings()
    if "skip_archived" in s:
        return bool(s["skip_archived"])
    return _env_flag("TELEGRAM_SKIP_ARCHIVED", True)


def update_filters(skip_muted=None, skip_archived=None, user_id=None):
    """Меняет настройки фильтрации (из веб-панели) и сразу пересобирает
    кэш, чтобы изменение применилось без перезапуска сервера."""
    s = _load_settings()
    if skip_muted is not None:
        s["sync_muted"] = True
        s["skip_muted"] = True
    if skip_archived is not None:
        s["skip_archived"] = bool(skip_archived)
    _save_settings(s)
    if _state_for(user_id)["authorized"]:
        try:
            _call(_refresh_filter_cache(user_id), timeout=30)
        except Exception as exc:  # noqa: BLE001
            _state_for(user_id)["error"] = f"filter: {exc}"


def ghost_mode_enabled() -> bool:
    """True — мы НЕ шлём Telegram'у никаких сигналов о том, что мы читаем
    или печатаем (read receipts, typing, online-пинг). Поведение по
    умолчанию — True: код моста и так не вызывает `send_read_acknowledge`/
    `set_typing`, флаг просто фиксирует «режим призрак» как осознанный
    выбор и страхует от случайных будущих вызовов."""
    s = _load_settings()
    if "ghost_mode" in s:
        return bool(s["ghost_mode"])
    return _env_flag("TELEGRAM_GHOST_MODE", True)


def set_ghost_mode(enabled: bool, user_id=None):
    s = _load_settings()
    s["ghost_mode"] = bool(enabled)
    _save_settings(s)


def _state_for(user_id=None):
    user_id = _normalize_user_id(user_id)
    if user_id not in _states:
        _states[user_id] = dict(_STATE_TEMPLATE)
    return _states[user_id]


def _normalize_user_id(user_id=None):
    try:
        return int(user_id if user_id is not None else _owner_user_id())
    except (TypeError, ValueError):
        return _owner_user_id()


def _session_path(user_id=None):
    legacy = os.environ.get("TELEGRAM_SESSION")
    if legacy and user_id is None:
        return legacy
    user_id = _normalize_user_id(user_id)
    session_dir = os.path.join(os.getcwd(), "db", "tg_sessions")
    os.makedirs(session_dir, exist_ok=True)
    return os.path.join(session_dir, f"user_{user_id}")


def _sent_code_hint(sent) -> str:
    code_type = getattr(getattr(sent, "type", None), "__class__", type(None)).__name__
    next_type = getattr(getattr(sent, "next_type", None), "__class__", type(None)).__name__
    length = getattr(getattr(sent, "type", None), "length", None)
    length_text = f", {length} цифр" if length else ""
    if "App" in code_type:
        place = "приложение Telegram на одном из ваших устройств"
    elif "Sms" in code_type:
        place = "SMS"
    elif "Call" in code_type:
        place = "телефонный звонок"
    elif "FlashCall" in code_type:
        place = "flash-call"
    else:
        place = "Telegram"
    hint = f"Код отправлен: {place}{length_text}."
    if "Sms" in next_type:
        hint += " Если код не пришёл, Telegram позже разрешит запросить SMS."
    elif "Call" in next_type:
        hint += " Если код не пришёл, Telegram позже разрешит звонок."
    return hint


def _media_root():
    """То же хранилище медиа, что у Android-клиента (см. main._media_root)."""
    return os.environ.get("SKILLWOOD_MEDIA_ROOT") or os.path.join(
        os.getcwd(), "media")


def _media_max_bytes():
    """Лимит на размер скачиваемого медиа. Крупнее — не качаем, оставляем
    в ленте текстовый плейсхолдер. Настраивается TELEGRAM_MEDIA_MAX_MB."""
    try:
        mb = int(os.environ.get("TELEGRAM_MEDIA_MAX_MB",
                                str(_DEFAULT_MEDIA_MAX_MB)))
    except ValueError:
        mb = _DEFAULT_MEDIA_MAX_MB
    return max(1, mb) * 1024 * 1024


def _media_store_max_bytes():
    """Общий предел media/, чтобы вложения не заполняли квоту хостинга.

    SKILLWOOD_MEDIA_STORE_MAX_MB — новое имя; Telegram-prefixed вариант
    оставлен как совместимый fallback. Ноль отключает общий предел.
    """
    try:
        raw = os.environ.get(
            "SKILLWOOD_MEDIA_STORE_MAX_MB",
            os.environ.get("TELEGRAM_MEDIA_STORE_MAX_MB",
                           str(_DEFAULT_MEDIA_STORE_MAX_MB)))
        mb = int(raw)
    except ValueError:
        mb = _DEFAULT_MEDIA_STORE_MAX_MB
    return max(0, mb) * 1024 * 1024


def _media_store_usage_bytes(force=False):
    """Размер media/ с коротким кэшем; вызывается только перед загрузкой."""
    now = time.monotonic()
    if not force and now - _media_usage_cache[0] < 30:
        return _media_usage_cache[1]
    with _media_usage_lock:
        now = time.monotonic()
        if not force and now - _media_usage_cache[0] < 30:
            return _media_usage_cache[1]
        # Если во время обхода кто-то сохранил файл, поколение изменится.
        # Повторяем один раз, не блокируя event-loop на threading.Lock.
        total = 0
        for _attempt in range(2):
            generation = _media_usage_cache[2]
            total = 0
            root = _media_root()
            try:
                for base, _dirs, files in os.walk(root):
                    for name in files:
                        try:
                            total += os.path.getsize(os.path.join(base, name))
                        except OSError:
                            continue
            except OSError:
                total = 0
            if generation == _media_usage_cache[2]:
                _media_usage_cache[0] = time.monotonic()
                _media_usage_cache[1] = total
                return total
        _media_usage_cache[0] = 0.0
        return max(total, _media_usage_cache[1])


def _invalidate_media_usage_cache():
    _media_usage_cache[0] = 0.0
    _media_usage_cache[2] += 1


def _note_media_usage_delta(size):
    """Обновляет уже посчитанный кэш без повторного обхода всего media/."""
    _media_usage_cache[2] += 1
    if _media_usage_cache[0]:
        _media_usage_cache[1] += max(0, int(size or 0))
        _media_usage_cache[0] = time.monotonic()


def _encrypted_size_estimate(raw_size):
    """Fernet хранит payload в base64 и добавляет служебные поля."""
    raw_size = max(0, int(raw_size or 0))
    return ((raw_size + 96) * 4 // 3) + 256


def _media_store_has_capacity(incoming_size=0):
    limit = _media_store_max_bytes()
    if limit <= 0:
        return True
    return _media_store_usage_bytes() + max(0, int(incoming_size or 0)) <= limit


class MediaStoreFullError(OSError):
    """Безопасный отказ записи до того, как квота уронит SQLite/WSGI."""


@dataclass
class _MediaReservation:
    size: int
    replacing_size: int = 0
    active: bool = True


def reserve_media_write(size, replacing_size=0):
    """Атомарно резервирует место для любого writer в этом процессе."""
    global _media_reserved_bytes
    size = max(0, int(size or 0))
    replacing_size = max(0, int(replacing_size or 0))
    limit = _media_store_max_bytes()
    with _media_reservation_lock:
        growth = max(0, size - replacing_size)
        if (limit > 0 and _media_store_usage_bytes()
                + _media_reserved_bytes + growth > limit):
            limit_mb = round(limit / 1024 / 1024)
            raise MediaStoreFullError(
                f"Хранилище достигло безопасного лимита {limit_mb} МБ")
        _media_reserved_bytes += growth
    return _MediaReservation(size=size, replacing_size=replacing_size)


def finish_media_write(reservation, committed):
    """Освобождает reservation и обновляет общий кэш после записи."""
    global _media_reserved_bytes
    if reservation is None or not reservation.active:
        return
    growth = max(0, reservation.size - reservation.replacing_size)
    with _media_reservation_lock:
        if committed:
            # Между reserve и finish другой writer мог пересчитать os.walk
            # уже вместе с новым файлом. Прибавление delta тогда посчитает
            # его дважды и ложно объявит хранилище заполненным. Инвалидация
            # дешевле и безопаснее; следующий reserve получит точный размер.
            _invalidate_media_usage_cache()
        _media_reserved_bytes = max(0, _media_reserved_bytes - growth)
        reservation.active = False


async def _reserve_media_write_async(size, replacing_size=0):
    """Резервирует место вне event-loop и не теряет reservation при cancel."""
    task = asyncio.create_task(asyncio.to_thread(
        reserve_media_write, size, replacing_size))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # shield оставляет worker жить. Когда он закончит, обязательно снимем
        # возможную reservation; иначе один logout способен ложно заполнить
        # хранилище до следующего перезапуска WSGI.
        def _release(done):
            try:
                reservation = done.result()
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                return
            finish_media_write(reservation, False)

        task.add_done_callback(_release)
        raise


def _catchup_media_allowed(msg, kind):
    """В догонке автоматически берём только свежие небольшие фото.

    Видео и документы остаются ленивыми: иначе один reconnect снова заполнит
    ограниченную квоту AlwaysData. Live-события используют обычный лимит.
    """
    if kind != "image":
        return False
    msg_date = getattr(msg, "date", None)
    if msg_date is None:
        return False
    try:
        age = time.time() - msg_date.timestamp()
    except (AttributeError, OSError, OverflowError, ValueError):
        return False
    if age < -300 or age > _CATCHUP_MEDIA_MAX_AGE_SECONDS:
        return False
    size = getattr(getattr(msg, "file", None), "size", None) or 0
    try:
        catchup_mb = int(os.environ.get(
            "TELEGRAM_CATCHUP_IMAGE_MAX_MB",
            str(_DEFAULT_CATCHUP_IMAGE_MAX_MB)))
    except ValueError:
        catchup_mb = _DEFAULT_CATCHUP_IMAGE_MAX_MB
    catchup_limit = max(1, catchup_mb) * 1024 * 1024
    return (not size or size <= min(catchup_limit, _media_max_bytes()))


def is_configured() -> bool:
    aid, ah = _env_api()
    return bool(aid and ah)


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
        return True
    except Exception:
        return False


def _ensure_loop():
    global _loop, _thread
    if _loop is not None:
        return
    _loop = asyncio.new_event_loop()

    def _runner():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _thread = threading.Thread(target=_runner, name="tg-bridge", daemon=True)
    _thread.start()


def _call(coro, timeout=60):
    _ensure_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return fut.result(timeout=timeout)
    except FutureTimeoutError:
        # Иначе HTTP-запрос уже вернул ошибку, а coroutine позже всё равно
        # выполнит отправку/сброс входа и неожиданно изменит состояние.
        fut.cancel()
        raise


def _async_lock_for(store, owner):
    owner = _normalize_user_id(owner)
    lock = store.get(owner)
    if lock is None:
        lock = asyncio.Lock()
        store[owner] = lock
    return lock


def _client_lock_for(owner):
    return _async_lock_for(_client_locks, owner)


def _handler_lock_for(owner):
    return _async_lock_for(_handler_locks, owner)


def _sync_lock_for(owner):
    return _async_lock_for(_sync_locks, owner)


def _lifecycle_token(owner):
    return int(_lifecycle_generation.get(_normalize_user_id(owner), 0))


def _bump_lifecycle(owner):
    owner = _normalize_user_id(owner)
    _lifecycle_generation[owner] = _lifecycle_token(owner) + 1
    return _lifecycle_generation[owner]


async def _get_client(user_id=None):
    global _client
    user_id = _normalize_user_id(user_id)
    async with _client_lock_for(user_id):
        if user_id in _clients:
            client = _clients[user_id]
            try:
                is_connected = client.is_connected()
            except Exception:  # noqa: BLE001
                is_connected = True
            if not is_connected:
                await client.connect()
            return client
        from telethon import TelegramClient
        aid, ah = _env_api()
        # connection_retries поменьше — без VPN серверы Telegram недоступны,
        # нет смысла долго долбиться (по умолчанию 5 попыток).
        client = TelegramClient(_session_path(user_id), aid, ah,
                                connection_retries=3)
        await client.connect()
        _clients[user_id] = client
        if user_id == _owner_user_id():
            _client = client
        return client


def _sender_name(sender) -> str:
    if sender is None:
        return "Telegram"
    fn = getattr(sender, "first_name", None)
    ln = getattr(sender, "last_name", None)
    un = getattr(sender, "username", None)
    name = " ".join(p for p in (fn, ln) if p).strip()
    if not name:
        name = ("@" + un) if un else (getattr(sender, "title", None) or "Telegram")
    return name


def _chat_title(chat) -> str:
    """Название чата (группы/канала) — становится именем контакта."""
    if chat is None:
        return "Telegram"
    return getattr(chat, "title", None) or _sender_name(chat)


async def _resolve_fwd_from(fwd, client=None):
    """Из `MessageFwdHeader` достаёт (имя_автора, его_telegram_chat_id).
    Если автор «спрятал» себя в форвардах (private settings), telethon
    отдаёт только `from_name` без `from_id` — chat_id будет None,
    в UI ник станет некликабельным."""
    from_id = getattr(fwd, "from_id", None)
    from_name = getattr(fwd, "from_name", None)
    chat_id = None
    name = None
    if from_id is not None:
        try:
            from telethon import utils
            chat_id = utils.get_peer_id(from_id)
        except Exception:  # noqa: BLE001
            chat_id = None
        if chat_id is not None:
            try:
                if client is None:
                    client = await _get_client()
                entity = await client.get_entity(from_id)
                name = _sender_name(entity)
            except Exception:  # noqa: BLE001
                name = None
    if not name:
        name = from_name or "Скрытый отправитель"
    return name, chat_id


def _media_kind(msg):
    """Тип вложения в терминах модели Attachment, либо None.

    Порядок важен: видео/голос/аудио/стикер в Telegram — это тоже
    document, поэтому общий `document`-файл проверяем последним."""
    if getattr(msg, "photo", None):
        return "image"
    # Кружочек (video note) — отдельный вид: в UI рисуем круглым плеером,
    # поэтому отличаем его от обычного видео ещё на приёме.
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "video", None) or getattr(msg, "gif", None):
        return "video"
    if getattr(msg, "document", None):
        return "file"
    return None


def _media_placeholder(kind, msg):
    """Текст сообщения, когда подписи нет (или медиа слишком крупное)."""
    if kind == "file":
        name = getattr(getattr(msg, "file", None), "name", None)
        return f"📎 {name}" if name else "📎 Файл"
    return {
        "image": "📷 Фото",
        "video": "🎬 Видео",
        # Кружок показываем «голым» — без текстовой подписи в бабле.
        "video_note": "",
        "voice": "🎤 Голосовое сообщение",
        "audio": "🎵 Аудио",
        "sticker": "🩷 Стикер",
    }.get(kind, "📎 Вложение")


def _sticker_pack_key_from_set(stickerset, pack_set=None):
    if stickerset is None and pack_set is None:
        return None
    set_id = getattr(pack_set, "id", None) or getattr(stickerset, "id", None)
    if set_id:
        return f"telegram:{set_id}"
    short_name = (getattr(pack_set, "short_name", None)
                  or getattr(stickerset, "short_name", None))
    if short_name:
        return f"telegram:{short_name}"
    return None


def _sticker_pack_meta_from_message(msg, data=None):
    try:
        stickerset = _sticker_set_from_message(msg)
    except Exception:  # noqa: BLE001
        stickerset = None
    document = getattr(msg, "document", None)
    pack_key = _sticker_pack_key_from_set(stickerset)
    pack_title = getattr(stickerset, "short_name", None) or None
    item_key = getattr(document, "id", None)
    if item_key is None and data:
        item_key = hashlib.sha256(data).hexdigest()
    return pack_key, pack_title, str(item_key) if item_key is not None else None


def _save_attachment(db, user_id, message_id, kind, data, msg):
    """Шифрует и кладёт медиа в media/<user_id>/, создаёт Attachment.
    Хранилище и шифрование — те же, что у Android-клиента."""
    from data.attachments import Attachment
    from data.crypto import encrypt_bytes

    existing = (db.query(Attachment)
                .filter(Attachment.user_id == user_id,
                        Attachment.message_id == message_id).first())
    if existing is not None:
        existing_full = os.path.join(_media_root(), existing.stored_path)
        if os.path.exists(existing_full):
            return existing
        # Запись могла остаться после аварии диска/ручной очистки. Этот метод
        # вызывается только когда Telegram снова реально отдал байты, поэтому
        # заменяем битую ссылку, не создавая второе сообщение.
        db.delete(existing)
        db.flush()

    root = _media_root()
    rel_dir = str(user_id)
    os.makedirs(os.path.join(root, rel_dir), exist_ok=True)
    stored_path = f"{rel_dir}/{uuid.uuid4().hex}.enc"
    encrypted = encrypt_bytes(data)
    try:
        reservation = reserve_media_write(len(encrypted))
    except MediaStoreFullError as exc:
        _state_for(user_id)["last_media_skip"] = str(exc)
        return None
    committed = False
    full_path = os.path.join(root, stored_path)
    temp_path = full_path + ".tmp-" + uuid.uuid4().hex
    try:
        with open(temp_path, "wb") as f:
            f.write(encrypted)
        os.replace(temp_path, full_path)
        committed = True
    finally:
        if not committed:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        finish_media_write(reservation, committed)

    file_obj = getattr(msg, "file", None)
    pack_key = pack_title = item_key = None
    if kind == "sticker":
        pack_key, pack_title, item_key = _sticker_pack_meta_from_message(
            msg, data)
    att = Attachment(
        user_id=user_id,
        message_id=message_id,
        kind=kind,
        mime=getattr(file_obj, "mime_type", None),
        original_name=getattr(file_obj, "name", None),
        stored_path=stored_path,
        size=len(data),
        dedup_key=None,
        sticker_pack_key=pack_key,
        sticker_pack_title=pack_title,
        sticker_item_key=item_key,
    )
    db.add(att)
    db.commit()
    return att


async def _download_media_payload(msg, kind, user_id=None):
    state = _state_for(user_id)
    if kind is None:
        return None, None
    size = getattr(getattr(msg, "file", None), "size", None) or 0
    if size and size > _media_max_bytes():
        size_mb = round(size / 1024 / 1024, 1)
        limit_mb = round(_media_max_bytes() / 1024 / 1024, 1)
        state["last_media_skip"] = (
            f"{kind}: {size_mb} МБ больше лимита {limit_mb} МБ")
        return None, None
    if not await asyncio.to_thread(
            _media_store_has_capacity, _encrypted_size_estimate(size)):
        limit_mb = round(_media_store_max_bytes() / 1024 / 1024)
        state["last_media_skip"] = (
            f"{kind}: хранилище достигло безопасного лимита {limit_mb} МБ")
        return None, None
    try:
        data = await msg.download_media(file=bytes)
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"download: {exc}"
        logger.exception("Telegram media download failed for user_id=%s",
                         _normalize_user_id(user_id))
        return None, None
    if data is None:
        state["last_media_skip"] = f"{kind}: Telethon не вернул данные"
        return None, None
    if not await asyncio.to_thread(
            _media_store_has_capacity, _encrypted_size_estimate(len(data))):
        limit_mb = round(_media_store_max_bytes() / 1024 / 1024)
        state["last_media_skip"] = (
            f"{kind}: хранилище достигло безопасного лимита {limit_mb} МБ")
        return None, None
    state["last_media_skip"] = None
    return kind, data


async def _maybe_fetch_avatar(chat, chat_id, user_id=None, client=None,
                              expected_lifecycle=None):
    """Лениво скачивает фото профиля чата и сохраняет его контакту.
    Качает только если у контакта аватара ещё нет или файл был удалён."""
    from data import db_sessions
    from data.contacts import Contact, MessengerHandle
    from data.crypto import encrypt_bytes

    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    if _lifecycle_token(owner) != expected_lifecycle:
        return
    db = db_sessions.create_session()
    try:
        handle = (db.query(MessengerHandle)
                  .filter(MessengerHandle.user_id == owner,
                          MessengerHandle.messenger_name == "Telegram",
                          MessengerHandle.tg_chat_id == chat_id).first())
        if handle is None:
            return
        contact = db.query(Contact).filter(
            Contact.id == handle.contact_id).first()
        if contact is None:
            return
        had_stale_path = False
        if contact.avatar_path:
            full = os.path.join(_media_root(), contact.avatar_path)
            if os.path.exists(full):
                return
            contact.avatar_path = None
            had_stale_path = True
        contact_id = contact.id

        if client is None:
            client = await _get_client(owner)
        photo = await client.download_profile_photo(chat, file=bytes)
        if _lifecycle_token(owner) != expected_lifecycle:
            return
        if not photo:
            if had_stale_path:
                db.commit()
            return  # у чата нет фото профиля

        rel_dir = f"{owner}/tg_avatars"
        os.makedirs(os.path.join(_media_root(), rel_dir), exist_ok=True)
        rel_path = f"{rel_dir}/{contact_id}.enc"
        encrypted = encrypt_bytes(photo)
        full_path = os.path.join(_media_root(), rel_path)
        try:
            replacing_size = (os.path.getsize(full_path)
                              if os.path.exists(full_path) else 0)
        except OSError:
            replacing_size = 0
        try:
            # Первый reserve после рестарта считает весь media/. Не держим
            # из-за этого Telegram event-loop и приём новых сообщений.
            reservation = await _reserve_media_write_async(
                len(encrypted), replacing_size)
        except MediaStoreFullError:
            return
        if _lifecycle_token(owner) != expected_lifecycle:
            finish_media_write(reservation, False)
            return
        committed = False
        temp_path = full_path + ".tmp-" + uuid.uuid4().hex
        try:
            with open(temp_path, "wb") as f:
                f.write(encrypted)
            os.replace(temp_path, full_path)
            committed = True
        finally:
            if not committed:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            finish_media_write(reservation, committed)
        if _lifecycle_token(owner) == expected_lifecycle:
            contact.avatar_path = rel_path
            db.commit()
    finally:
        db.close()


async def _handle_message(event, user_id=None, client=None,
                          expected_lifecycle=None):
    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    if _lifecycle_token(owner) != expected_lifecycle:
        return
    # Пропускаем чаты с выключенными уведомлениями. Архив Telegram не
    # отбрасываем: такие сообщения попадут в локальный раздел «Архив».
    if event.chat_id in _skip_chat_ids:
        return
    # Discussion-группы каналов (комментарии) — не создаём из них
    # отдельный Contact в БД. См. _known_discussion_groups.
    _ensure_discussion_groups_loaded()
    if _chat_id_variants(event.chat_id) & _known_discussion_groups:
        return
    msg = event.message
    is_out = bool(getattr(msg, "out", False))
    kind = _media_kind(msg)
    preloaded_media = None

    # Одноразовое фото важно забрать до сетевых запросов метаданных чата:
    # после открытия в Telegram оно может исчезнуть. Обычные фото/файлы
    # остаются на прежнем ленивом пути ниже.
    media = getattr(msg, "media", None)
    if kind is not None and getattr(media, "ttl_seconds", None):
        loaded_kind, loaded_data = await _download_media_payload(
            msg, kind, user_id=user_id)
        if loaded_kind is not None and loaded_data is not None:
            preloaded_media = (loaded_kind, loaded_data)

    # Telegram присылает комментарии как обычные сообщения связанной
    # megagroup. Пользователь мог впервые открыть комментарии в оригинальном
    # Telegram, поэтому группа ещё не обязательно есть в нашем реестре.
    # Определяем её до любых запросов к SQLite и до Web Push: это не даёт
    # шквалу комментариев подвесить сайт.
    chat = await event.get_chat()
    if _lifecycle_token(owner) != expected_lifecycle:
        return
    if await _is_linked_discussion_group(
            chat, event.chat_id, user_id=user_id, client=client):
        return

    # Anti-dupe: при пересылке (и в принципе любых исходящих, записанных
    # синхронно из веб-панели) мы уже создали запись в БД с этим
    # tg_message_id. Если эхо прилетело — не дублируем.
    tg_message_id_for_dupe = getattr(msg, "id", None)
    if tg_message_id_for_dupe is not None:
        from data import db_sessions
        from data.attachments import Attachment
        from data.contacts import MessengerHandle
        from data.users import Messages as _Messages
        db = db_sessions.create_session()
        try:
            handle_ids = [hid for (hid,) in db.query(MessengerHandle.id).filter(
                MessengerHandle.user_id == _normalize_user_id(user_id),
                MessengerHandle.messenger_name == "Telegram",
                MessengerHandle.tg_chat_id == event.chat_id).all()]
            if handle_ids:
                exists = db.query(
                    _Messages.id, _Messages.delivery_status).filter(
                    _Messages.user_id == _normalize_user_id(user_id),
                    _Messages.tg_message_id == int(tg_message_id_for_dupe),
                    _Messages.handle_id.in_(handle_ids)).first()
                # `scheduled` — скрытая idempotency-запись. Реальное эхо
                # должно пройти в record_message, который превратит её в
                # видимое отправленное сообщение и сохранит стабильный id.
                if (exists is not None
                        and exists.delivery_status != 'scheduled'):
                    if kind is None:
                        return
                    attachments = db.query(Attachment).filter(
                        Attachment.user_id == _normalize_user_id(user_id),
                        Attachment.message_id == exists.id).all()
                    if any(
                            att.stored_path and os.path.exists(os.path.join(
                                _media_root(), att.stored_path))
                            for att in attachments):
                        return
                    # Разрешаем repair только свежему фото. Старые файлы могли
                    # быть удалены владельцем специально при очистке квоты;
                    # catch_up не должен молча скачивать их заново.
                    if not _catchup_media_allowed(msg, kind):
                        return
        finally:
            db.close()

    # Контакт = сам чат: для лички это собеседник, для группы/канала —
    # название чата. event.get_chat() возвращает собеседника и для
    # входящих, и для исходящих, поэтому исходящие тоже попадают куда надо.
    chat_key = _chat_title(chat)

    if event.is_private:
        chat_type = "private"
    elif event.is_group:
        chat_type = "group"
    else:
        chat_type = "channel"

    # Автор подписи над сообщением.
    author_tg_chat_id = None
    if is_out:
        author = "Вы"
    elif chat_type == "group":
        author = _sender_name(await event.get_sender())
        author_tg_chat_id = getattr(event, "sender_id", None)
    else:  # личка или канал
        author = chat_key
        author_tg_chat_id = getattr(event, "sender_id", None)

    text = msg.message or ""
    if not text and kind is None:
        # Ни текста, ни понятного вложения (например системное событие).
        return

    if is_out and _pop_self_sent_id(
            event.chat_id, getattr(msg, "id", None), user_id):
        return

    # Своё сообщение, отправленное через веб-панель, уже записано
    # маршрутом /send — не дублируем его эхом из Telegram.
    if is_out and _pop_self_sent(event.chat_id, text):
        return

    await _persist_telegram_message(msg, event.chat_id, chat, chat_key,
                                    chat_type, is_out, author, kind, text,
                                    author_tg_chat_id=author_tg_chat_id,
                                    user_id=user_id, client=client,
                                    preloaded_media=preloaded_media,
                                    expected_lifecycle=expected_lifecycle)


async def _persist_telegram_message(msg, chat_id, chat, chat_key, chat_type,
                                    is_out, author, kind, text,
                                    author_tg_chat_id=None, user_id=None,
                                    client=None, archived=None, muted=None,
                                    download_media=True, notify=True,
                                    preloaded_media=None,
                                    expected_lifecycle=None):
    """Скачивает медиа (если есть), пишет запись в БД и тянет аватар чата.
    Вынесено из `_handle_message`, чтобы тем же кодом сохранять и сообщения,
    созданные синхронно прямо из веб-панели (forward / send) — иначе UI ждёт
    NewMessage-эха из Telethon, которое может задержаться или потеряться."""
    # Скачиваем медиа, если оно есть и не слишком большое. Для catch-up
    # синхронизации истории медиа не тянем автоматически, чтобы после
    # временного обрыва мост не забивал квоту пачкой старых видео/файлов.
    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    if _lifecycle_token(owner) != expected_lifecycle:
        return
    was_known = _telegram_message_exists(
        owner, chat_id, getattr(msg, "id", None))
    data = None
    if preloaded_media is not None:
        kind, data = preloaded_media
    elif download_media:
        kind, data = await _download_media_payload(msg, kind, user_id=user_id)
    if _lifecycle_token(owner) != expected_lifecycle:
        return

    # send_file мог завершиться, пока handler ждал download_media. Проверяем
    # точный id повторно прямо перед записью в БД.
    if is_out and _pop_self_sent_id(
            chat_id, getattr(msg, "id", None), user_id):
        return

    if not text:
        text = _media_placeholder(_media_kind(msg), msg)

    # На какое telegram-сообщение это — ответ (reply).
    reply_to_tg_id = getattr(msg, "reply_to_msg_id", None)

    # Одноразовое медиа: ttl_seconds лежит на MessageMediaPhoto / Document.
    # Мы файл уже скачали выше — он останется у нас даже после ttl.
    ttl = None
    media = getattr(msg, "media", None)
    if media is not None:
        ttl = getattr(media, "ttl_seconds", None)

    # Forward-заголовок: имя оригинального автора + его chat_id (если виден).
    # У Telegram это `Message.fwd_from` (MessageFwdHeader). `from_id` — Peer
    # автора (User/Channel/Chat), `from_name` — fallback для пересылок со
    # «спрятанным» автором (запрет показа имени в форвардах).
    fwd_name = None
    fwd_chat_id = None
    fwd = getattr(msg, "fwd_from", None)
    if fwd is not None:
        fwd_name, fwd_chat_id = await _resolve_fwd_from(fwd, client=client)

    # Forum-чат: Telegram кладёт top-id темы в reply_to. Сам head-message
    # темы имеет `action = MessageActionTopicCreate(title=...)` — оттуда
    # берём название. Для head-сообщения tg_topic_id == tg_message_id.
    is_forum_chat = bool(getattr(chat, "forum", False))
    topic_id = None
    topic_title = None
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is not None and getattr(reply_to, "forum_topic", False):
        topic_id = (getattr(reply_to, "reply_to_top_id", None)
                    or getattr(reply_to, "reply_to_msg_id", None))
    action = getattr(msg, "action", None)
    if action is not None:
        # MessageActionTopicCreate / TopicEdit. Импортим лениво —
        # типы могут отсутствовать в старом Telethon.
        try:
            from telethon.tl.types import (MessageActionTopicCreate,
                                            MessageActionTopicEdit)
            if isinstance(action, (MessageActionTopicCreate,
                                    MessageActionTopicEdit)):
                topic_title = getattr(action, "title", None)
                # Голова темы — это сам этот msg.
                if topic_id is None:
                    topic_id = getattr(msg, "id", None)
                # Сбрасываем кэш — клиент сразу увидит свежий список.
                _forum_topics_cache.pop(int(chat_id), None)
        except ImportError:
            pass

    # HTML-версия текста с форматированием (bold/italic/spoiler/code/
    # ссылки). У Telegram это `Message.entities` — список позиционных
    # тегов. Telethon умеет собирать их обратно в HTML через
    # extensions.html.unparse. Если разметки нет — оставляем NULL,
    # UI сам отрисует plain через escapeHtml+linkify.
    text_html = None
    entities = getattr(msg, "entities", None)
    if entities:
        try:
            from telethon.extensions.html import unparse as _tg_html_unparse
            text_html = _tg_html_unparse(text, entities)
        except Exception:  # noqa: BLE001
            text_html = None

    if _lifecycle_token(owner) != expected_lifecycle:
        return

    from data import db_sessions
    from data.contacts import record_message
    db = db_sessions.create_session()
    notify_message_id = None
    try:
        message = record_message(db, owner, "Telegram", chat_key, text,
                                 tg_chat_id=chat_id, author=author,
                                 outgoing=is_out, tg_chat_type=chat_type,
                                 tg_message_id=getattr(msg, "id", None),
                                 reply_to_tg_id=reply_to_tg_id,
                                 tg_ttl_seconds=ttl,
                                 fwd_from_name=fwd_name,
                                 fwd_from_tg_chat_id=fwd_chat_id,
                                 author_tg_chat_id=author_tg_chat_id,
                                 tg_topic_id=int(topic_id) if topic_id else None,
                                 tg_topic_title=topic_title,
                                 tg_is_forum=is_forum_chat,
                                 text_html=text_html,
                                 archived=archived,
                                 muted=muted)
        if message is not None and not was_known:
            notify_message_id = message.id
        # message is None — контакт в блок-листе, медиа тоже пропускаем.
        if message is not None and data is not None and kind is not None:
            _save_attachment(db, owner, message.id, kind, data, msg)
    finally:
        db.close()

    if (_lifecycle_token(owner) == expected_lifecycle
            and notify and notify_message_id is not None):
        try:
            from data import webpush
            await asyncio.to_thread(webpush.notify_message, notify_message_id)
        except Exception as exc:  # noqa: BLE001
            _state_for(user_id)["error"] = f"webpush: {exc}"

    # Фото профиля собеседника/группы — лениво, один раз.
    if _lifecycle_token(owner) != expected_lifecycle:
        return
    try:
        await _maybe_fetch_avatar(
            chat, chat_id, user_id=user_id, client=client,
            expected_lifecycle=expected_lifecycle)
    except Exception as exc:  # noqa: BLE001
        _state_for(user_id)["error"] = f"avatar: {exc}"


def _pop_self_sent(chat_id, text) -> bool:
    """True, если (chat_id, text) — недавняя отправка из веб-панели.
    Совпадение удаляется, протухшие записи (старше 2 минут) чистятся."""
    now = time.monotonic()
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return False
    found = False
    kept = []
    for rec_cid, rec_text, ts in _recent_self_sent:
        if now - ts >= 120:
            continue
        if not found and rec_cid == cid and rec_text == text:
            found = True
            continue
        kept.append((rec_cid, rec_text, ts))
    _recent_self_sent[:] = kept
    return found


def _pop_self_sent_id(chat_id, message_id, user_id=None) -> bool:
    """Снимает маркер конкретного Telegram-сообщения, отправленного web."""
    if message_id is None:
        return False
    now = time.monotonic()
    try:
        owner = _normalize_user_id(user_id)
        cid = int(chat_id)
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    found = False
    kept = []
    for rec_owner, rec_cid, rec_mid, ts in _recent_self_sent_ids:
        if now - ts >= 120:
            continue
        if (not found and rec_owner == owner
                and rec_cid == cid and rec_mid == mid):
            found = True
            continue
        kept.append((rec_owner, rec_cid, rec_mid, ts))
    _recent_self_sent_ids[:] = kept
    return found


def _chat_type_from_entity(chat) -> str:
    """Грубый тип Telegram-сущности для catch-up синхронизации."""
    if chat is None:
        return "private"
    if getattr(chat, "broadcast", False):
        return "channel"
    if (getattr(chat, "megagroup", False)
            or getattr(chat, "gigagroup", False)
            or getattr(chat, "title", None)):
        return "group"
    return "private"


def _archive_handle_types_for_entity(chat):
    """Типы handle, совместимые с Telegram-сущностью диалога."""
    return (_chat_type_from_entity(chat),)


def _archive_handle_types_for_peer(peer):
    """Ограничить legacy-поиск ID тем же пространством Telegram peer."""
    name = type(peer).__name__.lower()
    if 'user' in name:
        return ('private',)
    if 'channel' in name:
        # PeerChannel покрывает и каналы, и megagroup/supergroup.
        return ('group', 'channel')
    if 'chat' in name:
        return ('group',)
    return None


def _telegram_message_state(user_id, chat_id, tg_message_id):
    """Возвращает (message_id, has_live_attachment) для Telegram-id."""
    if tg_message_id is None:
        return None, False
    from data import db_sessions
    from data.attachments import Attachment
    from data.contacts import MessengerHandle
    from data.users import Messages as _Messages

    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    try:
        handle_ids = [hid for (hid,) in db.query(MessengerHandle.id).filter(
            MessengerHandle.user_id == owner,
            MessengerHandle.messenger_name == "Telegram",
            MessengerHandle.tg_chat_id == chat_id).all()]
        if not handle_ids:
            return None, False
        message = db.query(_Messages).filter(
            _Messages.user_id == owner,
            _Messages.tg_message_id == int(tg_message_id),
            _Messages.handle_id.in_(handle_ids)).first()
        if message is None:
            return None, False
        attachments = db.query(Attachment).filter(
            Attachment.user_id == owner,
            Attachment.message_id == message.id).all()
        has_live = any(
            att.stored_path
            and os.path.exists(os.path.join(_media_root(), att.stored_path))
            for att in attachments
        )
        return message.id, has_live
    finally:
        db.close()


def _telegram_message_exists(user_id, chat_id, tg_message_id) -> bool:
    message_id, _has_live_attachment = _telegram_message_state(
        user_id, chat_id, tg_message_id)
    return message_id is not None


async def _repair_telegram_photo(message_id, msg, user_id=None,
                                 expected_lifecycle=None):
    """Докачивает файл к существующей текстовой заглушке без нового push."""
    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    if _lifecycle_token(owner) != expected_lifecycle:
        return False
    kind, data = await _download_media_payload(msg, "image", user_id=user_id)
    if (kind is None or data is None
            or _lifecycle_token(owner) != expected_lifecycle):
        return False
    from data import db_sessions
    from data.users import Messages as _Messages

    db = db_sessions.create_session()
    try:
        message = db.query(_Messages).filter(
            _Messages.id == int(message_id),
            _Messages.user_id == owner).first()
        if message is None:
            return False
        return _save_attachment(
            db, owner, message.id, kind, data, msg) is not None
    finally:
        db.close()


async def _sync_recent_dialogs(user_id=None, client=None, dialog_limit=None,
                               message_limit=None):
    """Сериализует ограниченную догонку одного Telegram-аккаунта."""
    owner = _normalize_user_id(user_id)
    lock = _sync_lock_for(owner)
    # Startup, reconnect и UI могут попросить догонку одновременно. Второй
    # полный проход не нужен: уже запущенный увидит тот же свежий хвост.
    if lock.locked():
        return 0
    expected_lifecycle = _lifecycle_token(owner)
    async with lock:
        if _lifecycle_token(owner) != expected_lifecycle:
            return 0
        return await _sync_recent_dialogs_once(
            owner, client=client, dialog_limit=dialog_limit,
            message_limit=message_limit,
            expected_lifecycle=expected_lifecycle)


async def _sync_recent_dialogs_once(user_id=None, client=None,
                                    dialog_limit=None, message_limit=None,
                                    expected_lifecycle=None):
    """Best-effort catch-up последних Telegram-сообщений.

    Live NewMessage обычно ловит входящие/исходящие сразу. Но после рестарта,
    сетевого обрыва или сбоя фонового event-stream часть апдейтов можно
    пропустить. Этот проход дешево догоняет последние сообщения из недавних
    диалогов и сохраняет только те tg_message_id, которых ещё нет в БД.
    """
    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    if client is None:
        client = await _get_client(owner)
    if not await client.is_user_authorized():
        return 0
    if _lifecycle_token(owner) != expected_lifecycle:
        return 0
    # После рестарта WSGI пользователь мог попасть сюда раньше startup-задачи.
    # Идемпотентно возвращаем live handler, чтобы следующие сообщения шли
    # сразу, а не продолжали жить только на десятисекундной догонке.
    await _activate(owner, client=client)

    dialog_limit = int(dialog_limit or _RECENT_SYNC_DIALOG_LIMIT)
    message_limit = int(message_limit or _RECENT_SYNC_MESSAGE_LIMIT)
    saved = 0
    seen_dialogs = 0
    archive_states = []
    async for dialog in client.iter_dialogs(limit=dialog_limit):
        if _lifecycle_token(owner) != expected_lifecycle:
            return saved
        if seen_dialogs >= dialog_limit:
            break
        seen_dialogs += 1
        chat = getattr(dialog, "entity", None)
        chat_id = getattr(dialog, "id", None)
        if chat_id is None and chat is not None:
            try:
                from telethon import utils
                chat_id = utils.get_peer_id(chat)
            except Exception:  # noqa: BLE001
                chat_id = None
        if chat_id is None:
            continue
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError):
            continue
        if chat_id in _skip_chat_ids:
            continue
        _ensure_discussion_groups_loaded()
        if _chat_id_variants(chat_id) & _known_discussion_groups:
            continue
        if await _is_linked_discussion_group(
                chat, chat_id, user_id=owner, client=client):
            continue

        chat_key = _chat_title(chat)
        chat_type = _chat_type_from_entity(chat)
        archived = int(getattr(dialog, "folder_id", 0) or 0) == 1

        async for msg in client.iter_messages(chat or chat_id,
                                              limit=message_limit):
            if _lifecycle_token(owner) != expected_lifecycle:
                return saved
            tg_id = getattr(msg, "id", None)
            if tg_id is None:
                continue
            kind = _media_kind(msg)
            existing_id, has_live_attachment = _telegram_message_state(
                owner, chat_id, tg_id)
            if existing_id is not None:
                if (kind == "image" and not has_live_attachment
                        and _catchup_media_allowed(msg, kind)):
                    await _repair_telegram_photo(
                        existing_id, msg, user_id=owner,
                        expected_lifecycle=expected_lifecycle)
                continue
            text = getattr(msg, "message", None) or ""
            if not text and kind is None:
                continue
            is_out = bool(getattr(msg, "out", False))
            author_tg_chat_id = None
            if is_out:
                author = "Вы"
            elif chat_type == "group":
                try:
                    sender = await msg.get_sender()
                except Exception:  # noqa: BLE001
                    sender = None
                author = _sender_name(sender)
                author_tg_chat_id = getattr(sender, "id", None)
            else:
                author = chat_key
                author_tg_chat_id = getattr(msg, "sender_id", None)

            await _persist_telegram_message(
                msg, chat_id, chat, chat_key, chat_type,
                is_out, author, kind, text,
                author_tg_chat_id=author_tg_chat_id,
                user_id=owner, client=client,
                download_media=_catchup_media_allowed(msg, kind),
                notify=False,
                expected_lifecycle=expected_lifecycle)
            saved += 1
        # Статус архива приходит от самого Dialog, а не вычисляется по
        # числовым вариантам ID сообщения. Так входящее сообщение само по
        # себе не меняет архив, но catch-up подхватывает действие из Telegram.
        archive_handle_types = _archive_handle_types_for_entity(chat)
        archive_states.append((chat_id, archived, archive_handle_types))
        # Entity уже пришла вместе со списком диалогов, поэтому определение
        # форума не требует отдельного блокирующего MTProto-запроса из UI.
        if (chat_type in ('group', 'channel')
                and hasattr(chat, 'forum')):
            _apply_telegram_forum_state(
                owner, chat_id, bool(getattr(chat, 'forum', False)),
                allowed_types=archive_handle_types)
    if _lifecycle_token(owner) == expected_lifecycle:
        _apply_telegram_archive_snapshot(owner, archive_states)
    return saved


async def _safe_sync_recent_dialogs(user_id=None):
    owner = _normalize_user_id(user_id)
    try:
        return await _sync_recent_dialogs(owner)
    except Exception as exc:  # noqa: BLE001
        _state_for(owner)["error"] = f"sync_recent: {exc}"
        return 0
    finally:
        _recent_sync_at_by_user[owner] = time.monotonic()
        _recent_sync_inflight_users.discard(owner)


def sync_recent(user_id=None, wait=False):
    """Запускает throttled catch-up недавних Telegram-диалогов.

    В обычном UI режиме не блокируем запрос: догонка завершится в фоне, а
    следующий polling контактов/ленты покажет найденные сообщения.
    """
    if not is_configured() or not telethon_available():
        return None
    owner = _normalize_user_id(user_id)
    if wait:
        return _call(_sync_recent_dialogs(owner), timeout=60)
    now = time.monotonic()
    last = _recent_sync_at_by_user.get(owner, 0)
    if owner in _recent_sync_inflight_users:
        return None
    if now - last < _RECENT_SYNC_INTERVAL:
        return None
    _recent_sync_inflight_users.add(owner)
    _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(
        _safe_sync_recent_dialogs(owner), _loop)
    _recent_sync_futures[owner] = future

    def _forget(done_future):
        if _recent_sync_futures.get(owner) is done_future:
            _recent_sync_futures.pop(owner, None)

    future.add_done_callback(_forget)
    return None


async def _register_handler(user_id=None, client=None):
    """Регистрирует ровно один набор callbacks на пользователя/процесс."""
    owner = _normalize_user_id(user_id)
    async with _handler_lock_for(owner):
        if owner in _handler_registered_users:
            return
        token = _lifecycle_token(owner)
        return await _register_handler_unlocked(
            owner, client=client, registration_token=token)


async def _register_handler_unlocked(user_id=None, client=None,
                                     registration_token=None):
    global _handler_registered
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    if registration_token is None:
        registration_token = _lifecycle_token(owner)
    from telethon import events
    if client is None:
        client = await _get_client(owner)
    if _lifecycle_token(owner) != registration_token:
        return

    # Без incoming=True — ловим и входящие, и исходящие (мои ответы
    # с любого устройства Telegram тоже попадают в ленту).
    @client.on(events.NewMessage())
    async def _on_new(event):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            await _handle_message(
                event, user_id=owner, client=client,
                expected_lifecycle=registration_token)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"incoming: {exc}"
            logger.exception("Telegram live handler failed for user_id=%s",
                             owner)

    # Событие «печатает…» + смена онлайн-статуса. UserUpdate приходит и на
    # то, и на другое — какие именно поля выставлены, зависит от Telegram.
    @client.on(events.UserUpdate())
    async def _on_user_update(event):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            if getattr(event, "typing", False):
                chat_id = int(event.chat_id)
                exp = time.monotonic() + 6
                name = None
                user_key = getattr(event, "user_id", None)
                try:
                    user = await event.get_user()
                    if user is not None:
                        from telethon.utils import get_display_name
                        name = get_display_name(user) or None
                        user_key = getattr(user, "id", user_key)
                except Exception:  # noqa: BLE001
                    name = None
                if not name:
                    name = "Собеседник"
                state = _typing.get(chat_id)
                if not isinstance(state, dict):
                    state = {"expires": exp, "authors": {}}
                state["expires"] = exp
                state.setdefault("authors", {})[user_key or name] = {
                    "name": name,
                    "expires": exp,
                }
                _typing[chat_id] = state
        except Exception:  # noqa: BLE001
            pass

    # Прочтение моих исходящих собеседником: апдейт ставит max_id —
    # все мои сообщения с tg_message_id <= max_id в этом чате прочитаны.
    # Для приватных диалогов и базовых групп — UpdateReadHistoryOutbox,
    # для супергрупп/каналов — UpdateReadChannelOutbox.
    from telethon.tl.types import (UpdateReadHistoryOutbox,
                                    UpdateReadChannelOutbox)

    @client.on(events.Raw([UpdateReadHistoryOutbox, UpdateReadChannelOutbox]))
    async def _on_read_outbox(update):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            await _handle_read_outbox(update, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"read_outbox: {exc}"

    # Удаление сообщений собеседником или мной с другого устройства.
    # У нас в БД сообщение не сносится, а помечается deleted_at — в ленте
    # вместо текста показывается «🗑 Сообщение удалено».
    @client.on(events.MessageDeleted())
    async def _on_deleted(event):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            await _handle_deleted(event, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"deleted: {exc}"

    # Реакции на сообщения (мои и собеседника). У user-API Telegram это один
    # тип апдейта — UpdateMessageReactions — который покрывает и личку, и
    # группы, и каналы. Мы перезаписываем строки message_reactions целиком.
    from telethon.tl.types import UpdateMessageReactions

    @client.on(events.Raw([UpdateMessageReactions]))
    async def _on_reactions(update):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            await _handle_reactions(update, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"reactions: {exc}"

    # Смена mute/unmute в Telegram должна отражаться на сайте без ожидания
    # периодического refresh.
    from telethon.tl.types import UpdateNotifySettings

    @client.on(events.Raw([UpdateNotifySettings]))
    async def _on_notify_settings(update):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            await _handle_notify_settings_update(update, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"notify_settings: {exc}"

    # Смена архива в Telegram должна отражаться на сайте без ожидания
    # следующего периодического refresh.
    from telethon.tl.types import UpdateFolderPeers

    @client.on(events.Raw([UpdateFolderPeers]))
    async def _on_folder_peers(update):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            await _handle_folder_peers_update(update, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"folder_peers: {exc}"

    # Редактирование сообщений (мои с другого устройства и собеседника).
    # Telegram в UI показывает только финальный текст с пометкой «ред.»;
    # мы храним ВСЕ прошлые версии в `message_edits`, чтобы видеть, что
    # человек хотел сказать изначально.
    @client.on(events.MessageEdited())
    async def _on_edited(event):
        if _lifecycle_token(owner) != registration_token:
            return
        try:
            await _handle_edited(event, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"edited: {exc}"

    if _lifecycle_token(owner) == registration_token:
        _handler_registered_users.add(owner)
        _handler_registered = True


async def _handle_reactions(update, user_id=None):
    """Принять UpdateMessageReactions/UpdateChannelMessageReactions и записать
    набор реакций в БД. Telegram присылает агрегат — мы храним снимок."""
    from data import db_sessions
    from data.contacts import MessengerHandle
    from data.reactions import replace_reactions
    from data.users import Messages

    msg_id = getattr(update, "msg_id", None)
    if msg_id is None:
        return
    chat_id = None
    peer = getattr(update, "peer", None)
    if peer is not None:
        from telethon import utils
        try:
            chat_id = utils.get_peer_id(peer)
        except Exception:  # noqa: BLE001
            chat_id = None
    if chat_id is None:
        ch = getattr(update, "channel_id", None)
        if ch is not None:
            chat_id = -1000000000000 - int(ch)

    items = _parse_reactions(getattr(update, "reactions", None))
    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    try:
        q = db.query(Messages).filter(
            Messages.user_id == owner,
            Messages.messenger_name == "Telegram",
            Messages.tg_message_id == int(msg_id))
        if chat_id is not None:
            handle_ids = [hid for (hid,) in db.query(MessengerHandle.id).filter(
                MessengerHandle.user_id == owner,
                MessengerHandle.messenger_name == "Telegram",
                MessengerHandle.tg_chat_id == chat_id).all()]
            if not handle_ids:
                return
            q = q.filter(Messages.handle_id.in_(handle_ids))
        target = q.first()
        if target is None:
            return
        replace_reactions(db, target.id, items)
        db.commit()
    finally:
        db.close()


def _parse_reactions(reactions_obj):
    """MessageReactions.results -> [{emoji, count, mine}]. Кастомные эмодзи
    (TGS-стикеры) пропускаем — отрисовывать нечем; считаем только обычные."""
    items = []
    if reactions_obj is None:
        return items
    results = getattr(reactions_obj, "results", None) or []
    for rc in results:
        r = getattr(rc, "reaction", None)
        emoticon = getattr(r, "emoticon", None)
        if not emoticon:
            continue
        items.append({
            "emoji": emoticon,
            "count": int(getattr(rc, "count", 0) or 0),
            "mine": getattr(rc, "chosen_order", None) is not None,
        })
    return items


async def _handle_deleted(event, user_id=None):
    """Пометить наши Messages с указанными tg_message_id как удалённые.

    Для каналов/супергрупп `event.chat_id` есть → фильтр по чату.
    Для лички/малых групп `event.chat_id` обычно None — id'шники
    глобально уникальны в рамках моего диалога, фильтруем только по id."""
    import datetime as _dt
    from data import db_sessions
    from data.contacts import MessengerHandle
    from data.users import Messages

    ids = list(getattr(event, "deleted_ids", None) or [])
    if not ids:
        return
    owner = _normalize_user_id(user_id)
    chat_id = getattr(event, "chat_id", None)
    db = db_sessions.create_session()
    try:
        q = db.query(Messages).filter(
            Messages.user_id == owner,
            Messages.messenger_name == "Telegram",
            Messages.tg_message_id.in_(ids),
            Messages.deleted_at.is_(None),
        )
        if chat_id is not None:
            handle_ids = [hid for (hid,) in db.query(MessengerHandle.id).filter(
                MessengerHandle.user_id == owner,
                MessengerHandle.messenger_name == "Telegram",
                MessengerHandle.tg_chat_id == chat_id).all()]
            if not handle_ids:
                return
            q = q.filter(Messages.handle_id.in_(handle_ids))
        q.update({Messages.deleted_at: _dt.datetime.now()},
                 synchronize_session=False)
        db.commit()
    finally:
        db.close()


async def _handle_edited(event, user_id=None):
    """Зафиксировать прошлую версию текста сообщения в `message_edits`.

    Telethon шлёт MessageEdited не только на изменение текста: иногда
    приходит при добавлении pin'а, при правке inline-кнопок и т.п.
    Поэтому сравниваем старый и новый текст; если совпадают — выходим."""
    import datetime as _dt
    from data import db_sessions
    from data.attachments import Attachment
    from data.contacts import MessengerHandle
    from data.edits import push_old_version
    from data.matching import is_media_placeholder
    from data.users import Messages

    msg = event.message
    tg_id = getattr(msg, "id", None)
    chat_id = getattr(event, "chat_id", None)
    if tg_id is None or chat_id is None:
        return
    new_text = msg.message or ""
    # edit_date — момент редактирования по серверному времени Telegram.
    # Если его нет — fallback на «сейчас».
    edit_dt = getattr(msg, "edit_date", None) or _dt.datetime.now()

    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    try:
        handle_ids = [hid for (hid,) in db.query(MessengerHandle.id).filter(
            MessengerHandle.user_id == owner,
            MessengerHandle.messenger_name == "Telegram",
            MessengerHandle.tg_chat_id == chat_id).all()]
        if not handle_ids:
            return
        target = (db.query(Messages)
                  .filter(Messages.user_id == owner,
                          Messages.tg_message_id == int(tg_id),
                          Messages.handle_id.in_(handle_ids)).first())
        if target is None:
            return
        old_text = target.text or ""
        target_has_media = (db.query(Attachment.id)
                            .filter(Attachment.message_id == target.id)
                            .first() is not None)
        new_kind = _media_kind(msg)
        if not target_has_media and new_kind is not None:
            saved_kind, data = await _download_media_payload(
                msg, new_kind, user_id=user_id)
            if saved_kind is not None and data is not None:
                target_has_media = _save_attachment(
                    db, owner, target.id, saved_kind, data, msg) is not None
        if is_media_placeholder(old_text) and target_has_media:
            target.text = new_text
            db.commit()
            return
        if is_media_placeholder(old_text) and new_kind is not None:
            if new_text:
                target.text = new_text
                db.commit()
            return
        if old_text == new_text:
            return
        push_old_version(db, target.id, old_text, edit_dt)
        target.text = new_text
        db.commit()
    finally:
        db.close()


async def _handle_read_outbox(update, user_id=None):
    """Отметить как прочитанные все исходящие в чате с tg_message_id <= max_id."""
    import datetime as _dt
    from data import db_sessions
    from data.contacts import MessengerHandle
    from data.users import Messages

    # peer/channel_id есть в обоих апдейтах, нормализуем к нашему tg_chat_id.
    chat_id = None
    peer = getattr(update, "peer", None)
    if peer is not None:
        from telethon import utils
        try:
            chat_id = utils.get_peer_id(peer)
        except Exception:  # noqa: BLE001
            chat_id = None
    if chat_id is None:
        ch = getattr(update, "channel_id", None)
        if ch is not None:
            chat_id = -1000000000000 - int(ch)
    max_id = getattr(update, "max_id", None)
    if chat_id is None or max_id is None:
        return

    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    try:
        handle_ids = [hid for (hid,) in db.query(MessengerHandle.id).filter(
            MessengerHandle.user_id == owner,
            MessengerHandle.messenger_name == "Telegram",
            MessengerHandle.tg_chat_id == chat_id).all()]
        if not handle_ids:
            return
        db.query(Messages).filter(
            Messages.handle_id.in_(handle_ids),
            Messages.outgoing.is_(True),
            Messages.tg_read_at.is_(None),
            Messages.tg_message_id.isnot(None),
            Messages.tg_message_id <= int(max_id),
        ).update({Messages.tg_read_at: _dt.datetime.now()},
                 synchronize_session=False)
        db.commit()
    finally:
        db.close()


def is_typing(chat_id) -> bool:
    """True, если собеседник в этом чате печатает прямо сейчас."""
    return typing_status(chat_id)["typing"]


def typing_status(chat_id) -> dict:
    """Статус печати с именами авторов, если Telegram их прислал."""
    try:
        state = _typing.get(int(chat_id))
    except (TypeError, ValueError):
        return {"typing": False, "authors": []}
    now = time.monotonic()
    if isinstance(state, dict):
        authors = state.get("authors") or {}
        alive = []
        for key, info in list(authors.items()):
            if not isinstance(info, dict):
                continue
            if info.get("expires", 0) > now:
                name = (info.get("name") or "").strip()
                if name and name not in alive:
                    alive.append(name)
            else:
                authors.pop(key, None)
        if alive:
            state["expires"] = max(
                info.get("expires", 0)
                for info in authors.values()
                if isinstance(info, dict)
            )
            return {"typing": True, "authors": alive}
        return {"typing": bool(state.get("expires", 0) > now), "authors": []}
    return {"typing": bool(state is not None and state > now), "authors": []}


def _is_muted(dialog) -> bool:
    """True, если у диалога выключены уведомления (mute_until в будущем)."""
    ns = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    return _notify_settings_muted(ns)


def _notify_settings_muted(settings) -> bool:
    """True, если notify_settings Telegram задаёт mute_until в будущем."""
    import datetime as _dt
    mute_until = getattr(settings, "mute_until", None)
    if mute_until is None:
        return False
    if isinstance(mute_until, (int, float)):
        return mute_until > time.time()
    now = _dt.datetime.now(_dt.timezone.utc)
    if mute_until.tzinfo is None:
        mute_until = mute_until.replace(tzinfo=_dt.timezone.utc)
    return mute_until > now


def _chat_id_variants(chat_id):
    try:
        from data.telegram_ids import chat_id_variants
        return {int(v) for v in chat_id_variants(chat_id)}
    except Exception:  # noqa: BLE001
        try:
            return {int(chat_id)}
        except (TypeError, ValueError):
            return set()


def _apply_telegram_mute_state(user_id, chat_id, muted):
    """Записать mute/unmute конкретного Telegram-диалога в Contact.muted."""
    ids = _chat_id_variants(chat_id)
    if not ids:
        return 0
    from data import db_sessions
    from data.contacts import Contact, MessengerHandle
    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    changed = 0
    try:
        handles = (db.query(MessengerHandle)
                   .filter(MessengerHandle.user_id == owner,
                           MessengerHandle.messenger_name == "Telegram",
                           MessengerHandle.tg_chat_id.in_(list(ids)))
                   .all())
        for handle in handles:
            contact = db.query(Contact).filter(
                Contact.id == handle.contact_id,
                Contact.user_id == owner).first()
            if contact is not None and bool(contact.muted) != bool(muted):
                contact.muted = bool(muted)
                changed += 1
        if changed:
            db.commit()
        return changed
    finally:
        db.close()


def _apply_telegram_mute_cache(user_id, known_ids, muted_ids):
    """Синхронизировать все уже известные Telegram-контакты с кэшем mute."""
    from data import db_sessions
    from data.contacts import Contact, MessengerHandle
    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    changed = 0
    try:
        handles = (db.query(MessengerHandle)
                   .filter(MessengerHandle.user_id == owner,
                           MessengerHandle.messenger_name == "Telegram",
                           MessengerHandle.tg_chat_id.isnot(None))
                   .all())
        for handle in handles:
            handle_ids = _chat_id_variants(handle.tg_chat_id)
            if not handle_ids or not (handle_ids & known_ids):
                continue
            contact = db.query(Contact).filter(
                Contact.id == handle.contact_id,
                Contact.user_id == owner).first()
            should_mute = bool(handle_ids & muted_ids)
            if contact is not None and bool(contact.muted) != should_mute:
                contact.muted = should_mute
                changed += 1
        if changed:
            db.commit()
        return changed
    finally:
        db.close()


def _telegram_handles_for_peer(db, owner, chat_id, allowed_types=None):
    """Найти handle peer без смешивания Telegram user/chat/channel ID."""
    try:
        exact_id = int(chat_id)
    except (TypeError, ValueError):
        return []
    from data.contacts import MessengerHandle
    # Канонический peer id уникален между user/chat/channel. Сначала ищем
    # только его: расширенные варианты (+123 ↔ -1000000000123) могут
    # совпасть у совершенно разных Telegram-сущностей.
    exact_query = (db.query(MessengerHandle)
                   .filter(MessengerHandle.user_id == owner,
                           MessengerHandle.messenger_name == "Telegram",
                           MessengerHandle.tg_chat_id == exact_id))
    handles = exact_query.all()
    if allowed_types:
        allowed_types = tuple(allowed_types)
        handles = [
            handle for handle in handles
            if (handle.tg_chat_type in allowed_types
                # Отрицательные canonical ID не пересекаются с PeerUser,
                # поэтому безопасны и для старых строк без сохранённого типа.
                or (handle.tg_chat_type is None and exact_id < 0))
        ]
    # Fallback нужен для старых строк, где channel_id мог сохраниться без
    # префикса -100. Он безопасен только внутри известного peer-типа.
    safe_legacy_types = tuple(
        chat_type for chat_type in (allowed_types or ())
        if chat_type != 'group')
    if not handles and safe_legacy_types:
        legacy_ids = _chat_id_variants(exact_id) - {exact_id}
        if legacy_ids:
            handles = (db.query(MessengerHandle)
                       .filter(MessengerHandle.user_id == owner,
                               MessengerHandle.messenger_name == "Telegram",
                               MessengerHandle.tg_chat_type.in_(
                                   safe_legacy_types),
                               MessengerHandle.tg_chat_id.in_(
                                   list(legacy_ids)))
                       .all())
    return handles


def _telegram_handle_is_archived(handle, archived_ids):
    """Сопоставить сохранённый handle с каноническими ID из Telegram."""
    try:
        chat_id = int(handle.tg_chat_id)
    except (TypeError, ValueError):
        return False
    chat_type = handle.tg_chat_type
    if chat_type == 'private':
        candidates = {abs(chat_id)}
    elif chat_type == 'channel':
        short_id = abs(chat_id)
        if short_id > 1_000_000_000_000:
            short_id -= 1_000_000_000_000
        candidates = {-(1_000_000_000_000 + short_id)}
    elif chat_type == 'group':
        if chat_id < 0:
            candidates = {chat_id}
        else:
            # Старые строки могли хранить без знака и basic group, и
            # megagroup. Пространство private сюда намеренно не попадает.
            candidates = {
                -chat_id,
                -(1_000_000_000_000 + chat_id),
            }
    else:
        candidates = {chat_id}
    return bool(candidates & archived_ids)


def _apply_telegram_archive_state(user_id, chat_id, archived,
                                  allowed_types=None):
    """Записать archive/unarchive конкретного Telegram-диалога в Contact."""
    from data import db_sessions
    from data.contacts import Contact, MessengerHandle
    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    changed = 0
    try:
        handles = _telegram_handles_for_peer(
            db, owner, chat_id, allowed_types=allowed_types)
        contact_ids = {handle.contact_id for handle in handles}
        if not contact_ids:
            return 0
        all_handles = (db.query(MessengerHandle)
                       .filter(MessengerHandle.user_id == owner,
                               MessengerHandle.messenger_name == 'Telegram',
                               MessengerHandle.contact_id.in_(contact_ids),
                               MessengerHandle.tg_chat_id.isnot(None))
                       .all())
        updated_handle_ids = {handle.id for handle in handles}
        cached = _archived_chat_ids_by_user.get(owner)
        archived_ids = set(cached[1]) if cached is not None else set()
        states_by_contact = {}
        for handle in all_handles:
            handle_archived = (bool(archived)
                               if handle.id in updated_handle_ids
                               else _telegram_handle_is_archived(
                                   handle, archived_ids))
            states_by_contact.setdefault(handle.contact_id, []).append(
                handle_archived)
        contacts = (db.query(Contact)
                    .filter(Contact.user_id == owner,
                            Contact.id.in_(contact_ids))
                    .all())
        for contact in contacts:
            # Объединённый контакт скрывается только тогда, когда в архиве
            # находятся все привязанные к нему Telegram-диалоги.
            should_archive = all(states_by_contact.get(contact.id, (False,)))
            if bool(contact.archived) != should_archive:
                contact.archived = should_archive
                changed += 1
        if changed:
            db.commit()
        return changed
    finally:
        db.close()


def _apply_telegram_archive_snapshot(user_id, states):
    """Применить полный Telegram-снимок одним запросом и транзакцией."""
    if not states:
        return 0
    from data import db_sessions
    from data.contacts import Contact, MessengerHandle
    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    changed = 0
    try:
        handles = (db.query(MessengerHandle)
                   .filter(MessengerHandle.user_id == owner,
                           MessengerHandle.messenger_name == 'Telegram',
                           MessengerHandle.tg_chat_id.isnot(None))
                   .all())
        by_exact = {}
        for handle in handles:
            by_exact.setdefault(int(handle.tg_chat_id), []).append(handle)

        handle_states = {}
        affected_contact_ids = set()
        for chat_id, archived, allowed_types in states:
            try:
                exact_id = int(chat_id)
            except (TypeError, ValueError):
                continue
            allowed_types = tuple(allowed_types or ())
            matched = [
                handle for handle in by_exact.get(exact_id, ())
                if (not allowed_types
                    or handle.tg_chat_type in allowed_types
                    or (handle.tg_chat_type is None and exact_id < 0))
            ]
            safe_legacy_types = tuple(
                chat_type for chat_type in allowed_types
                if chat_type != 'group')
            if not matched and safe_legacy_types:
                legacy_ids = _chat_id_variants(exact_id) - {exact_id}
                matched = [
                    handle for handle in handles
                    if handle.tg_chat_type in safe_legacy_types
                    and int(handle.tg_chat_id) in legacy_ids
                ]
            for handle in matched:
                handle_states[handle.id] = bool(archived)
                affected_contact_ids.add(handle.contact_id)

        if affected_contact_ids:
            contacts = (db.query(Contact)
                        .filter(Contact.user_id == owner,
                                Contact.id.in_(list(affected_contact_ids)))
                        .all())
        else:
            contacts = []
        handles_by_contact = {}
        for handle in handles:
            if handle.contact_id in affected_contact_ids:
                handles_by_contact.setdefault(handle.contact_id, []).append(
                    handle)
        for contact in contacts:
            # Объединённый контакт остаётся в основном списке, пока хотя бы
            # один из его Telegram-диалогов не архивирован. Отсутствующий в
            # частичном recent-снимке handle считается активным: так catch-up
            # не может самопроизвольно скрыть контакт.
            should_archive = all(
                handle_states.get(handle.id, False)
                for handle in handles_by_contact.get(contact.id, ()))
            if bool(contact.archived) != should_archive:
                contact.archived = should_archive
                changed += 1
        if changed:
            db.commit()
        return changed
    finally:
        db.close()


def _apply_telegram_forum_state(user_id, chat_id, is_forum,
                                allowed_types=None):
    """Сохранить тип форума из уже загруженной Telegram-сущности."""
    import datetime as _dt
    from data import db_sessions
    owner = _normalize_user_id(user_id)
    db = db_sessions.create_session()
    changed = 0
    try:
        handles = _telegram_handles_for_peer(
            db, owner, chat_id, allowed_types=allowed_types)
        checked_at = _dt.datetime.now()
        for handle in handles:
            if bool(handle.tg_is_forum) != bool(is_forum):
                handle.tg_is_forum = bool(is_forum)
                changed += 1
            if handle.tg_forum_checked_at is None:
                handle.tg_forum_checked_at = checked_at
                changed += 1
        if changed:
            db.commit()
        return changed
    finally:
        db.close()


def _set_cached_mute_state(user_id, chat_id, muted):
    owner = _normalize_user_id(user_id)
    ids = _chat_id_variants(chat_id)
    if not ids:
        return
    cached = _muted_chat_ids_by_user.get(owner)
    muted_ids = set(cached[1]) if cached is not None else set()
    if muted:
        muted_ids.update(ids)
    else:
        muted_ids.difference_update(ids)
    _muted_chat_ids_by_user[owner] = (time.monotonic(), muted_ids)


def _set_cached_archive_state(user_id, chat_id, archived):
    owner = _normalize_user_id(user_id)
    try:
        exact_id = int(chat_id)
    except (TypeError, ValueError):
        return
    cached = _archived_chat_ids_by_user.get(owner)
    archived_ids = set(cached[1]) if cached is not None else set()
    if archived:
        archived_ids.add(exact_id)
    else:
        archived_ids.discard(exact_id)
    _archived_chat_ids_by_user[owner] = (time.monotonic(), archived_ids)


async def _refresh_mute_cache(user_id=None, client=None):
    """Возвращает множество chat_id, где в Telegram выключен звук."""
    owner = _normalize_user_id(user_id)
    if client is None:
        client = _clients.get(owner)
    if client is None:
        return set()
    known_ids = set()
    muted_ids = set()
    async for d in client.iter_dialogs():
        try:
            dialog_ids = _chat_id_variants(int(d.id))
        except (TypeError, ValueError):
            continue
        known_ids.update(dialog_ids)
        if _is_muted(d):
            muted_ids.update(dialog_ids)
    _muted_chat_ids_by_user[owner] = (time.monotonic(), muted_ids)
    _apply_telegram_mute_cache(owner, known_ids, muted_ids)
    return muted_ids


async def _chat_muted_by_telegram(chat_id, user_id=None, client=None):
    """Best effort: знает ли Telegram, что в диалоге выключен звук."""
    if not _skip_muted():
        return None
    owner = _normalize_user_id(user_id)
    now = time.monotonic()
    cached = _muted_chat_ids_by_user.get(owner)
    if cached is None or now - cached[0] > _MUTE_CACHE_TTL:
        try:
            ids = await _refresh_mute_cache(owner, client)
        except Exception:  # noqa: BLE001
            return None
    else:
        ids = cached[1]
    variants = _chat_id_variants(chat_id)
    if not variants:
        return None
    return bool(variants & ids)


def _notify_peer_chat_id(peer):
    """Достать обычный chat_id из NotifyPeer, который прислал Telegram."""
    inner = getattr(peer, "peer", None)
    if inner is None:
        return None
    try:
        from telethon import utils
        return int(utils.get_peer_id(inner))
    except Exception:  # noqa: BLE001
        for attr in ("user_id", "chat_id", "channel_id"):
            value = getattr(inner, attr, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
    return None


async def _handle_notify_settings_update(update, user_id=None):
    """Telegram сообщил, что у диалога поменялись настройки уведомлений."""
    if not _skip_muted():
        return
    chat_id = _notify_peer_chat_id(getattr(update, "peer", None))
    if chat_id is None:
        return
    muted = _notify_settings_muted(getattr(update, "notify_settings", None))
    _set_cached_mute_state(user_id, chat_id, muted)
    _apply_telegram_mute_state(user_id, chat_id, muted)


async def _handle_folder_peers_update(update, user_id=None):
    """Telegram сообщил, что диалог переместили в/из архива."""
    if not _skip_archived():
        return
    folder_peers = getattr(update, "folder_peers", None) or []
    for folder_peer in folder_peers:
        peer = getattr(folder_peer, "peer", None)
        chat_id = None
        try:
            from telethon import utils
            chat_id = int(utils.get_peer_id(peer))
        except Exception:  # noqa: BLE001
            for attr in ("user_id", "chat_id", "channel_id"):
                value = getattr(peer, attr, None)
                if value is None:
                    continue
                try:
                    chat_id = int(value)
                    break
                except (TypeError, ValueError):
                    continue
        if chat_id is None:
            continue
        archived = int(getattr(folder_peer, "folder_id", 0) or 0) == 1
        _set_cached_archive_state(user_id, chat_id, archived)
        _apply_telegram_archive_state(
            user_id, chat_id, archived,
            allowed_types=_archive_handle_types_for_peer(peer))


async def _refresh_filter_cache(user_id=None, client=None):
    """Пересобирает кэши Telegram-состояний, влияющих на контакты."""
    global _skip_chat_ids
    owner = _normalize_user_id(user_id)
    if client is None:
        client = _clients.get(owner)
    if client is None:
        return
    _skip_chat_ids = set()
    if _skip_muted():
        await _refresh_mute_cache(owner, client)
    else:
        _muted_chat_ids_by_user.pop(owner, None)
    if _skip_archived():
        await _refresh_archive_cache(owner, client)
    else:
        _archived_chat_ids_by_user.pop(owner, None)


async def _refresh_archive_cache(user_id=None, client=None):
    """Сверить локальные контакты с фактическими папками Telegram."""
    owner = _normalize_user_id(user_id)
    if client is None:
        client = _clients.get(owner)
    if client is None:
        return set()
    ids = set()
    states = []
    # archived=None возвращает все диалоги. Это важно: иначе мы узнаем только
    # о добавлении в архив, но не сможем вернуть локальный контакт обратно.
    async for d in client.iter_dialogs():
        try:
            chat_id = int(d.id)
        except (TypeError, ValueError):
            continue
        archived = int(getattr(d, 'folder_id', 0) or 0) == 1
        if archived:
            ids.add(chat_id)
        states.append((chat_id, archived,
                       _archive_handle_types_for_entity(
                           getattr(d, 'entity', None))))
    _archived_chat_ids_by_user[owner] = (time.monotonic(), ids)
    _apply_telegram_archive_snapshot(owner, states)
    return ids


async def _chat_archived_by_telegram(chat_id, user_id=None, client=None):
    """Best effort: знает ли Telegram, что диалог сейчас в архиве."""
    if not _skip_archived():
        return False
    owner = _normalize_user_id(user_id)
    now = time.monotonic()
    cached = _archived_chat_ids_by_user.get(owner)
    if cached is None or now - cached[0] > _ARCHIVE_CACHE_TTL:
        try:
            ids = await _refresh_archive_cache(owner, client)
        except Exception:  # noqa: BLE001
            return False
    else:
        ids = cached[1]
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return False
    return cid in ids


async def _periodic_refresh(user_id=None, expected_lifecycle=None):
    """Поддерживает live-поток и раз в 5 минут обновляет mute/archive.

    Нативный ``client.catch_up()`` здесь намеренно не используется: его
    события проходят обычный live-handler и могут разом скачать старые
    видео/файлы и отправить повторные Web Push. После реального reconnect
    запускаем нашу ограниченную догонку свежих сообщений без уведомлений.
    """
    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    next_filter_refresh = time.monotonic() + 300
    while True:
        await asyncio.sleep(10)
        if _lifecycle_token(owner) != expected_lifecycle:
            return
        try:
            current = _clients.get(owner)
            was_connected = bool(current and current.is_connected())
            client = await _get_client(owner)
            if not await client.is_user_authorized():
                continue
            await _register_handler(owner, client=client)
            if not was_connected:
                await _sync_recent_dialogs(owner, client=client)
            if time.monotonic() >= next_filter_refresh:
                await _refresh_filter_cache(owner, client=client)
                next_filter_refresh = time.monotonic() + 300
        except Exception as exc:  # noqa: BLE001
            _state_for(owner)["error"] = f"telegram reconnect: {exc}"
            logger.exception("Telegram maintenance failed for user_id=%s",
                             owner)


async def _activate(user_id=None, client=None):
    """Общий «после авторизации»: вешает обработчик входящих, строит
    кэш фильтрации и запускает периодическое обновление."""
    global _refresh_task
    owner = _normalize_user_id(user_id)
    expected_lifecycle = _lifecycle_token(owner)
    state = _state_for(owner)
    if client is None:
        client = await _get_client(owner)
    newly_registered = owner not in _handler_registered_users
    await _register_handler(owner, client=client)
    if _lifecycle_token(owner) != expected_lifecycle:
        return
    if newly_registered:
        try:
            await _refresh_filter_cache(owner, client=client)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"filter: {exc}"
    task = _refresh_tasks.get(owner)
    if task is None or task.done():
        task = asyncio.ensure_future(_periodic_refresh(
            owner, expected_lifecycle=expected_lifecycle))
        _refresh_tasks[owner] = task
        if owner == _owner_user_id():
            _refresh_task = task


async def _startup(user_id=None, expected_lifecycle=None):
    # Подгружаем известные discussion-группы каналов из БД, чтобы
    # события о новых комментариях (приходящие сразу после старта моста)
    # не успели породить «призрачный» Contact.
    _ensure_discussion_groups_loaded()
    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    if _lifecycle_token(owner) != expected_lifecycle:
        return
    state = _state_for(owner)
    authorized = False
    async with _auth_lock_for(owner):
        client = await _get_client(owner)
        if _lifecycle_token(owner) != expected_lifecycle:
            return
        authorized = await client.is_user_authorized()
        if _lifecycle_token(owner) != expected_lifecycle:
            return
        if authorized:
            state["authorized"] = True
            if owner not in _handler_registered_users:
                await _activate(owner, client=client)
    if authorized:
        # Ограниченная догонка: максимум несколько свежих сообщений,
        # только небольшие фото и без Web Push за уже прошедшую историю.
        await _sync_recent_dialogs(owner, client=client)


def _quiet_telethon_logging():
    """Глушит шумные WARNING'и Telethon о неудачных попытках подключения:
    без VPN до серверов Telegram не достучаться — это ожидаемо, и валить
    этим консоль не нужно. Реальная ошибка всё равно видна на /telegram."""
    import logging
    logging.getLogger("telethon").setLevel(logging.CRITICAL)


async def _safe_startup(user_id=None, expected_lifecycle=None):
    owner = _normalize_user_id(user_id)
    if expected_lifecycle is None:
        expected_lifecycle = _lifecycle_token(owner)
    delay = 5
    while True:
        if _lifecycle_token(owner) != expected_lifecycle:
            return
        try:
            await _startup(owner, expected_lifecycle=expected_lifecycle)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state_for(owner)["error"] = f"startup: {exc}"
            logger.exception("Telegram startup failed for user_id=%s", owner)
            await asyncio.sleep(delay)
            delay = min(120, delay * 3)


async def _refresh_status(user_id=None):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    async with _auth_lock_for(owner):
        client = await _get_client(owner)
        if await client.is_user_authorized():
            state["authorized"] = True
            await _activate(owner, client=client)
        else:
            state["authorized"] = False
    return state


def start(user_id=None):
    """Вызывается при старте приложения. Тихо выходит, если мост
    не настроен или telethon недоступен (тесты/обычный режим).

    Подключение к Telegram идёт в ФОНЕ: если Telegram недоступен
    (например, нет VPN), сервер Synapse всё равно стартует сразу и
    полностью работает — недоступна только Telegram-интеграция."""
    if not is_configured() or not telethon_available():
        return
    _quiet_telethon_logging()
    _ensure_loop()
    owners = {_normalize_user_id(user_id)} if user_id is not None else {
        _owner_user_id()
    }
    if user_id is None:
        session_dir = os.path.join(os.getcwd(), "db", "tg_sessions")
        try:
            for name in os.listdir(session_dir):
                match = re.fullmatch(r"user_(\d+)\.session", name)
                if match:
                    owners.add(int(match.group(1)))
        except OSError:
            pass
    for owner in owners:
        task = _startup_tasks.get(owner)
        if task is None or task.done():
            expected_lifecycle = _lifecycle_token(owner)
            _startup_tasks[owner] = asyncio.run_coroutine_threadsafe(
                _safe_startup(owner, expected_lifecycle=expected_lifecycle),
                _loop)


class TelegramAuthError(RuntimeError):
    """Безопасная русская ошибка, которую можно показать в интерфейсе."""


def _auth_lock_for(owner):
    lock = _auth_locks.get(owner)
    if lock is None:
        lock = asyncio.Lock()
        _auth_locks[owner] = lock
    return lock


def _auth_retry_key(owner, phone):
    return _normalize_user_id(owner), str(phone or "")


def _auth_retry_seconds(owner, phone):
    deadline = float(_auth_retry_after.get(
        _auth_retry_key(owner, phone), 0) or 0)
    wait = max(0, math.ceil(deadline - time.time()))
    if wait <= 0:
        _auth_retry_after.pop(_auth_retry_key(owner, phone), None)
    return wait


def _set_auth_retry(owner, phone, seconds):
    if not phone:
        return
    try:
        seconds = max(0, int(seconds or 0))
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return
    key = _auth_retry_key(owner, phone)
    _auth_retry_after[key] = max(
        float(_auth_retry_after.get(key, 0) or 0), time.time() + seconds)


def _normalize_phone(phone):
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) < 7 or len(digits) > 15:
        raise TelegramAuthError(
            "Проверьте номер телефона. Используйте международный формат, "
            "например +79991234567.")
    return "+" + digits


def _auth_error_message(exc):
    name = exc.__class__.__name__
    seconds = getattr(exc, "seconds", None)
    if name == "FloodWaitError":
        wait = f" Подождите {int(seconds)} сек." if seconds else ""
        return "Telegram временно ограничил повторные запросы." + wait
    messages = {
        "SendCodeUnavailableError": (
            "Telegram уже использовал все доступные способы доставки кода "
            "для этой попытки. Подождите и попробуйте позже либо измените номер."),
        "PhoneNumberFloodError": (
            "Для этого номера было слишком много попыток входа. "
            "Подождите и попробуйте позже."),
        "PhoneNumberInvalidError": (
            "Telegram не распознал номер. Проверьте международный формат."),
        "PhoneNumberBannedError": "Этот номер заблокирован Telegram.",
        "PhoneCodeInvalidError": "Неверный код Telegram. Проверьте и введите снова.",
        "PhoneCodeExpiredError": (
            "Срок действия кода истёк. Запросите новый код или измените номер."),
        "PhoneCodeEmptyError": "Введите код из Telegram.",
        "PhoneCodeHashEmptyError": (
            "Попытка входа устарела. Измените номер и запросите код заново."),
        "PasswordHashInvalidError": "Неверный пароль двухфакторной защиты.",
        "PhonePasswordFloodError": (
            "Слишком много попыток ввода пароля. Попробуйте позже."),
        "ApiIdInvalidError": "Ключи Telegram API на сервере настроены неверно.",
    }
    if name in messages:
        return messages[name]
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return "Не удалось связаться с Telegram. Попробуйте ещё раз чуть позже."
    return "Telegram не смог выполнить запрос. Попробуйте позже или измените номер."


def _raise_auth_error(action, owner, exc, phone=None):
    logger.warning("Telegram auth %s failed for user_id=%s: %s",
                   action, owner, exc.__class__.__name__)
    state = _state_for(owner)
    phone = phone or state.get("phone")
    name = exc.__class__.__name__
    seconds = getattr(exc, "seconds", None)
    if seconds:
        state["resend_available_at"] = max(
            float(state.get("resend_available_at") or 0),
            time.time() + int(seconds))
        _set_auth_retry(owner, phone, seconds)
    if name == "SendCodeUnavailableError":
        state["resend_supported"] = False
        state["resend_available_at"] = None
        _set_auth_retry(owner, phone, 300)
    elif name == "PhoneNumberFloodError":
        state["resend_supported"] = False
        state["resend_available_at"] = None
        _set_auth_retry(owner, phone, 900)
    elif name in ("PhoneCodeExpiredError", "PhoneCodeHashEmptyError"):
        _clear_pending_code(state, keep_phone=True)
        _set_auth_retry(owner, phone, 10)
    raise TelegramAuthError(_auth_error_message(exc)) from None


def _sent_code_method(sent):
    next_type = getattr(sent, "next_type", None)
    type_name = next_type.__class__.__name__ if next_type is not None else ""
    if "Sms" in type_name:
        return "SMS"
    if "Call" in type_name or "FlashCall" in type_name:
        return "звонок"
    if type_name:
        return "другой способ Telegram"
    return None


def _remember_sent_code(state, phone, sent):
    phone_code_hash = getattr(sent, "phone_code_hash", None)
    if not phone_code_hash:
        raise TelegramAuthError(
            "Telegram не создал новую попытку входа. Измените номер и "
            "запросите код заново.")
    timeout = getattr(sent, "timeout", None)
    try:
        timeout = max(0, int(timeout if timeout is not None else 60))
    except (TypeError, ValueError):
        timeout = 60
    method = _sent_code_method(sent)
    state.update({
        "phone": phone,
        "phone_code_hash": phone_code_hash,
        "code_hint": _sent_code_hint(sent),
        "resend_available_at": time.time() + timeout if method else None,
        "resend_supported": bool(method),
        "resend_method": method,
        "needs_password": False,
        "error": None,
    })
    return timeout


def _clear_pending_code(state, keep_phone=True):
    phone = state.get("phone") if keep_phone else None
    state.update({
        "phone": phone,
        "phone_code_hash": None,
        "code_hint": None,
        "resend_available_at": None,
        "resend_supported": False,
        "resend_method": None,
        "needs_password": False,
    })


async def _request_code(phone, user_id=None, force_sms=False):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    async with _auth_lock_for(owner):
        wait = _auth_retry_seconds(owner, phone)
        if wait > 0:
            raise TelegramAuthError(
                f"Новый код для этого номера можно запросить через {wait} сек.")
        if state.get("phone_code_hash"):
            raise TelegramAuthError(
                "Код уже запрошен. Введите его или нажмите «Изменить номер».")
        client = await _get_client(owner)
        if await client.is_user_authorized():
            state["authorized"] = True
            return
        try:
            # force_sms в новых Telethon не работает; способ выбирает Telegram.
            sent = await client.send_code_request(phone)
            if (not getattr(sent, "phone_code_hash", None)
                    and await client.is_user_authorized()):
                state["phone"] = phone
                state["authorized"] = True
                _clear_pending_code(state, keep_phone=True)
                await _activate(owner, client=client)
                return
            timeout = _remember_sent_code(state, phone, sent)
            _set_auth_retry(owner, phone, timeout)
        except TelegramAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _raise_auth_error("request_code", owner, exc, phone=phone)


def request_code(phone, user_id=None, force_sms=False):
    normalized = _normalize_phone(phone)
    owner = _normalize_user_id(user_id)
    try:
        _call(_request_code(normalized, user_id=owner,
                            force_sms=False))
    except TelegramAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_auth_error("request_code", owner, exc)


async def _resend_code(user_id=None):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    async with _auth_lock_for(owner):
        phone = state.get("phone")
        phone_code_hash = state.get("phone_code_hash")
        if not phone or not phone_code_hash:
            raise TelegramAuthError(
                "Сначала укажите номер и запросите код Telegram.")
        if not state.get("resend_supported"):
            raise TelegramAuthError(
                "Telegram пока не предложил другой способ доставки. "
                "Текущий код ещё можно ввести.")
        wait = math.ceil(float(state.get("resend_available_at") or 0)
                         - time.time())
        wait = max(wait, _auth_retry_seconds(owner, phone))
        if wait > 0:
            raise TelegramAuthError(
                f"Новый способ доставки станет доступен через {wait} сек.")
        client = await _get_client(owner)
        try:
            from telethon.tl.functions.auth import ResendCodeRequest
            sent = await client(ResendCodeRequest(phone, phone_code_hash))
            if (not getattr(sent, "phone_code_hash", None)
                    and await client.is_user_authorized()):
                state["authorized"] = True
                _clear_pending_code(state, keep_phone=True)
                await _activate(owner, client=client)
                return
            timeout = _remember_sent_code(state, phone, sent)
            _set_auth_retry(owner, phone, timeout)
        except TelegramAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _raise_auth_error("resend_code", owner, exc, phone=phone)


def resend_code(user_id=None):
    owner = _normalize_user_id(user_id)
    try:
        _call(_resend_code(user_id=owner))
    except TelegramAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_auth_error("resend_code", owner, exc)


async def _submit_code(code, user_id=None):
    from telethon.errors import SessionPasswordNeededError
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    async with _auth_lock_for(owner):
        if not state.get("phone") or not state.get("phone_code_hash"):
            raise TelegramAuthError(
                "Попытка входа устарела. Укажите номер и запросите новый код.")
        client = await _get_client(owner)
        try:
            await client.sign_in(
                state["phone"], code,
                phone_code_hash=state["phone_code_hash"])
        except SessionPasswordNeededError:
            state["needs_password"] = True
            return
        except Exception as exc:  # noqa: BLE001
            _raise_auth_error("submit_code", owner, exc)
        state["authorized"] = True
        _clear_pending_code(state, keep_phone=True)
        await _activate(owner, client=client)


def submit_code(code, user_id=None):
    try:
        _call(_submit_code(code.strip(), user_id=user_id))
    except TelegramAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_auth_error("submit_code", _normalize_user_id(user_id), exc)


async def _submit_password(password, user_id=None):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    async with _auth_lock_for(owner):
        if not state.get("needs_password"):
            raise TelegramAuthError(
                "Сейчас Telegram не ожидает пароль двухфакторной защиты.")
        client = await _get_client(owner)
        try:
            await client.sign_in(password=password)
        except Exception as exc:  # noqa: BLE001
            _raise_auth_error("submit_password", owner, exc)
        state["authorized"] = True
        _clear_pending_code(state, keep_phone=True)
        await _activate(owner, client=client)


def submit_password(password, user_id=None):
    try:
        _call(_submit_password(password, user_id=user_id))
    except TelegramAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_auth_error("submit_password", _normalize_user_id(user_id), exc)


async def _reset_login(user_id=None):
    """Отменяет только незавершённый вход и позволяет исправить номер."""
    global _client, _handler_registered, _refresh_task
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    # Startup может ждать сеть, удерживая auth-lock. Отменяем его ДО входа
    # в lock, иначе reset сам не сможет дойти до cancel и зависнет по timeout.
    startup = _startup_tasks.pop(owner, None)
    if startup is not None and not startup.done():
        startup.cancel()
    async with _auth_lock_for(owner):
        client = _clients.get(owner)
        if client is not None and await client.is_user_authorized():
            raise TelegramAuthError(
                "Telegram уже подключён. Для смены аккаунта сначала отключите его.")
        refresh_task = _refresh_tasks.pop(owner, None)
        if refresh_task is not None:
            refresh_task.cancel()
        sync_future = _recent_sync_futures.pop(owner, None)
        if sync_future is not None and not sync_future.done():
            sync_future.cancel()
        _recent_sync_inflight_users.discard(owner)
        _bump_lifecycle(owner)
        async with _handler_lock_for(owner):
            _handler_registered_users.discard(owner)
            _handler_registered = bool(_handler_registered_users)
        phone = state.get("phone")
        phone_code_hash = state.get("phone_code_hash")
        async with _client_lock_for(owner):
            client = _clients.get(owner)
            if client is not None and phone and phone_code_hash:
                try:
                    from telethon.tl.functions.auth import CancelCodeRequest
                    await client(CancelCodeRequest(phone, phone_code_hash))
                except Exception:  # noqa: BLE001
                    # Код мог уже истечь — локальный сброс всё равно нужен.
                    pass
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            _clients.pop(owner, None)
        if owner == _owner_user_id():
            _client = None
            _refresh_task = None
        _clear_pending_code(state, keep_phone=True)
        state["authorized"] = False
        state["error"] = None


def reset_login(user_id=None):
    try:
        _call(_reset_login(user_id=user_id))
    except TelegramAuthError:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_auth_error("reset_login", _normalize_user_id(user_id), exc)


async def _logout(user_id=None):
    global _client, _handler_registered, _refresh_task, _skip_chat_ids
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    startup = _startup_tasks.pop(owner, None)
    if startup is not None and not startup.done():
        startup.cancel()
    sync_future = _recent_sync_futures.pop(owner, None)
    if sync_future is not None and not sync_future.done():
        sync_future.cancel()
    _recent_sync_inflight_users.discard(owner)
    async with _auth_lock_for(owner):
        # Меняем поколение только когда logout действительно получил lock.
        # Иначе HTTP-timeout мог отменить logout, оставив авторизованный
        # аккаунт со всеми прежними handlers уже навсегда неактивными.
        _bump_lifecycle(owner)
        refresh_task = _refresh_tasks.pop(owner, None)
        if refresh_task is not None:
            refresh_task.cancel()
        async with _handler_lock_for(owner):
            _handler_registered_users.discard(owner)
            _handler_registered = bool(_handler_registered_users)
        async with _client_lock_for(owner):
            client = _clients.pop(owner, None)
            if client is not None:
                try:
                    await client.log_out()
                except Exception:  # noqa: BLE001
                    pass
        if owner == _owner_user_id():
            _client = None
            _refresh_task = None
        _skip_chat_ids = set()
        state.update(dict(_STATE_TEMPLATE))
        if owner == _owner_user_id():
            _state.update(dict(_STATE_TEMPLATE))


def logout(user_id=None):
    try:
        _call(_logout(user_id=user_id))
    except Exception as exc:  # noqa: BLE001
        _state_for(user_id)["error"] = str(exc)


_participants_cache = {}


async def _fetch_participants(chat_id, user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    result = []
    async for p in client.iter_participants(int(chat_id), limit=200):
        result.append({
            "name": _sender_name(p),
            "username": ("@" + p.username) if getattr(p, "username", None) else None,
            # tg_user_id нужен, чтобы клик по участнику в правой панели
            # мог найти Contact по этому id в нашей БД или открыть
            # https://t.me/<username> в новой вкладке.
            "tg_user_id": getattr(p, "id", None),
        })
    return result


def get_participants(chat_id, user_id=None):
    """Список участников группы (имя + username). Кэш на 10 минут,
    чтобы повторные открытия чата не били Telegram запросами."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    owner = _normalize_user_id(user_id)
    key = (owner, int(chat_id))
    now = time.monotonic()
    cached = _participants_cache.get(key)
    if cached and now - cached[1] < 600:
        return cached[0]
    data = _call(_fetch_participants(key[1], user_id=owner), timeout=60)
    _participants_cache[key] = (data, now)
    return data


# Кэш для расширенной информации профиля Telegram-контакта.
# 5 минут — компромисс между свежестью и числом запросов к Telegram API.
_user_info_cache = {}
_common_chats_cache = {}


async def _fetch_user_info(chat_id, user_id=None):
    """Через GetFullUserRequest достаём bio (about), phone и username."""
    from telethon.tl.functions.users import GetFullUserRequest
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    input_user = await client.get_input_entity(int(chat_id))
    full = await client(GetFullUserRequest(input_user))
    user = full.users[0] if getattr(full, "users", None) else None
    about = getattr(full.full_user, "about", None) if full.full_user else None
    return {
        "about": about or None,
        "phone": getattr(user, "phone", None) if user else None,
        "username": getattr(user, "username", None) if user else None,
        "first_name": getattr(user, "first_name", None) if user else None,
        "last_name": getattr(user, "last_name", None) if user else None,
        "is_bot": bool(getattr(user, "bot", False)) if user else False,
        "is_self": bool(getattr(user, "is_self", False)) if user else False,
    }


def get_user_info(chat_id, user_id=None):
    """Bio / телефон / @username Telegram-контакта. Кэш 5 минут."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    owner = _normalize_user_id(user_id)
    key = (owner, int(chat_id))
    now = time.monotonic()
    cached = _user_info_cache.get(key)
    if cached and now - cached[1] < 300:
        return cached[0]
    data = _call(_fetch_user_info(key[1], user_id=owner), timeout=30)
    _user_info_cache[key] = (data, now)
    return data


async def _fetch_common_chats(chat_id, limit, user_id=None):
    """Общие группы с пользователем через GetCommonChatsRequest."""
    from telethon.tl.functions.messages import GetCommonChatsRequest
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    input_user = await client.get_input_entity(int(chat_id))
    res = await client(GetCommonChatsRequest(
        user_id=input_user, max_id=0, limit=int(limit)))
    out = []
    for c in getattr(res, "chats", []) or []:
        out.append({
            "id": getattr(c, "id", None),
            "title": getattr(c, "title", "") or "",
            "is_channel": bool(getattr(c, "broadcast", False)),
            "participants_count": getattr(c, "participants_count", None),
        })
    return out


def get_common_chats(chat_id, limit=20, user_id=None):
    """Общие группы/каналы с пользователем. Кэш 5 минут."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    owner = _normalize_user_id(user_id)
    key = (owner, int(chat_id), int(limit))
    now = time.monotonic()
    cached = _common_chats_cache.get(key)
    if cached and now - cached[1] < 300:
        return cached[0]
    data = _call(_fetch_common_chats(key[1], key[2], user_id=owner),
                 timeout=30)
    _common_chats_cache[key] = (data, now)
    return data


# Множество id linked discussion-групп, известных нам как «комментарии
# к каналу». Заполняется лениво при каждом get_comments/send_comment
# и из БД при первом обращении (см. _ensure_discussion_groups_loaded).
# Используется в _handle_message чтобы НЕ создавать в БД Contact с именем
# «Комментарии» — события из этих групп игнорируются.
_known_discussion_groups = set()
_discussion_groups_loaded = False


def _remember_discussion_group(chat_id):
    """Запомнить linked discussion и удалить созданный ранее чат-призрак."""
    variants = _chat_id_variants(chat_id)
    _known_discussion_groups.update(variants)
    _persist_discussion_group(chat_id)
    try:
        _cleanup_discussion_contact(chat_id)
    except Exception:  # noqa: BLE001
        # Фильтр в памяти уже включён, поэтому новые сообщения и push всё
        # равно остановлены; очистку повторит загрузка реестра после рестарта.
        pass


async def _is_linked_discussion_group(chat, chat_id, user_id=None,
                                      client=None):
    """Проверяет, является ли megagroup комментариями Telegram-канала.

    У linked discussion-supergroup поле ChannelFull.linked_chat_id указывает
    обратно на канал. Проверка выполняется до сохранения сообщения, чтобы
    неизвестная ранее группа комментариев не создавала контакт и Web Push.
    """
    _ensure_discussion_groups_loaded()
    variants = _chat_id_variants(chat_id)
    if variants & _known_discussion_groups:
        return True

    # Broadcast-канал тоже имеет linked_chat_id, но сам канал скрывать нельзя:
    # отбрасываем только связанную с ним megagroup комментариев.
    if (chat is None
            or not (getattr(chat, "megagroup", False)
                    or getattr(chat, "gigagroup", False))
            or getattr(chat, "broadcast", False)):
        return False

    owner = _normalize_user_id(user_id)
    positive_ids = [value for value in variants if value > 0]
    canonical_id = min(positive_ids) if positive_ids else int(chat_id)
    key = (owner, canonical_id)
    now = time.monotonic()
    cached = _discussion_check_cache.get(key)
    if cached is not None and now - cached[0] < _DISCUSSION_CHECK_TTL:
        return bool(cached[1])

    if client is None:
        client = await _get_client(owner)
    try:
        from telethon.tl.functions.channels import GetFullChannelRequest
        peer = chat
        get_input_entity = getattr(client, "get_input_entity", None)
        if get_input_entity is not None:
            peer = await get_input_entity(chat)
        full = await client(GetFullChannelRequest(channel=peer))
        linked_chat_id = getattr(
            getattr(full, "full_chat", None), "linked_chat_id", None)
        is_discussion = linked_chat_id is not None
    except Exception as exc:  # noqa: BLE001
        # Ошибка MTProto не должна ломать приём обычных групп. Не кэшируем её:
        # следующий апдейт сможет повторить проверку после восстановления сети.
        _state_for(owner)["error"] = f"discussion_check: {exc}"
        return False

    _discussion_check_cache[key] = (now, is_discussion)
    if len(_discussion_check_cache) > 1024:
        expired_before = now - _DISCUSSION_CHECK_TTL
        for cache_key, value in list(_discussion_check_cache.items()):
            if value[0] < expired_before:
                _discussion_check_cache.pop(cache_key, None)
    if is_discussion:
        _remember_discussion_group(chat_id)
    return is_discussion


def _persist_discussion_group(chat_id):
    """Пишем id discussion-группы в БД. После рестарта Flask in-memory
    set обнуляется, а эта запись остаётся — следующее событие из этой
    группы (новый комментарий от другого пользователя, например)
    корректно игнорируется."""
    try:
        from data import db_sessions
        from data.discussion_groups import DiscussionGroup
        from data.telegram_ids import chat_id_variants
        variants = chat_id_variants(chat_id)
        store_id = min((x for x in variants if x > 0), default=int(chat_id))
        db = db_sessions.create_session()
        try:
            existing = (db.query(DiscussionGroup)
                        .filter(DiscussionGroup.tg_chat_id == int(store_id))
                        .first())
            if existing is None:
                db.add(DiscussionGroup(tg_chat_id=int(store_id)))
                db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass


def _ensure_discussion_groups_loaded():
    """Lazy-load discussion-групп из БД в in-memory set. Делается один
    раз за процесс — флаг `_discussion_groups_loaded`. Заодно чистим
    уже сохранённые в контактах «призрачные» чаты — на случай если
    Contact успел создаться до того, как мы зарегистрировали группу
    (или до этого фикса)."""
    global _discussion_groups_loaded
    if _discussion_groups_loaded:
        return
    try:
        from data import db_sessions
        from data.discussion_groups import DiscussionGroup
        from data.telegram_ids import chat_id_variants
        db = db_sessions.create_session()
        try:
            chat_ids = [int(d.tg_chat_id)
                        for d in db.query(DiscussionGroup).all()]
        finally:
            db.close()
        for cid in chat_ids:
            variants = chat_id_variants(cid)
            _known_discussion_groups.update(variants)
            try:
                for v in variants:
                    _cleanup_discussion_contact(v)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    _discussion_groups_loaded = True


def _cleanup_discussion_contact(chat_id):
    """Если для discussion-группы канала уже успел создаться Contact
    (комментарий пришёл echo'м до того, как мы пометили чат как
    discussion) — удаляем его и связанные handle'ы/сообщения.
    Контакт пользователя, который реально вёл переписку в этой группе
    как в обычной (а не через комментарии), мы здесь тоже снесём — это
    рассматривается как редкий edge-case."""
    from data import db_sessions
    from data.contacts import Contact, MessengerHandle
    from data.telegram_ids import chat_id_variants
    from data.users import Messages as _Messages
    db = db_sessions.create_session()
    try:
        variants = chat_id_variants(chat_id)
        handles = (db.query(MessengerHandle)
                   .filter(MessengerHandle.tg_chat_id.in_(variants))
                   .all())
        contact_ids = {h.contact_id for h in handles}
        if handles:
            handle_ids = [h.id for h in handles]
            db.query(_Messages).filter(
                _Messages.handle_id.in_(handle_ids)).delete(
                synchronize_session=False)
            db.query(MessengerHandle).filter(
                MessengerHandle.id.in_(handle_ids)).delete(
                synchronize_session=False)
        for cid in contact_ids:
            remaining = (db.query(MessengerHandle)
                         .filter(MessengerHandle.contact_id == cid)
                         .count())
            if remaining == 0:
                db.query(Contact).filter(Contact.id == cid).delete(
                    synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _comment_media_kind(m):
    """Какого вида медиа в комментарии (грубо: photo / video / file / None)."""
    if getattr(m, "photo", None):
        return "photo"
    if getattr(m, "video", None):
        return "video"
    if getattr(m, "voice", None):
        return "voice"
    if getattr(m, "media", None):
        return "file"
    return None


async def _get_comments(chat_id, msg_id, limit, user_id=None):
    """Получает комментарии к посту канала через linked discussion group.
    Возвращает dict: {available, items, discussion_chat_id, top_msg_id}.
    Если у канала нет discussion group — available=False."""
    from telethon.tl.functions.messages import GetDiscussionMessageRequest
    from telethon.tl.types import PeerChannel
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    peer = await client.get_input_entity(int(chat_id))
    try:
        res = await client(GetDiscussionMessageRequest(
            peer=peer, msg_id=int(msg_id)))
    except Exception as exc:  # noqa: BLE001
        # Канал без discussion group — Telegram возвращает ошибку.
        return {"available": False, "reason": "no_discussion",
                "detail": str(exc), "items": []}
    if not getattr(res, "messages", None):
        return {"available": False, "reason": "no_discussion", "items": []}
    top = res.messages[0]
    # peer_id у top — PeerChannel(channel_id) discussion-группы.
    disc_peer = getattr(top, "peer_id", None)
    if isinstance(disc_peer, PeerChannel):
        disc_id = int(disc_peer.channel_id)
    else:
        disc_id = int(getattr(disc_peer, "channel_id", 0)
                       or getattr(disc_peer, "chat_id", 0)
                       or getattr(disc_peer, "user_id", 0))
    # Запоминаем discussion-группу — события из неё не должны рождать
    # «призрачный» Contact в нашей БД. Дублируем в БД, чтобы пережило
    # рестарт Flask (in-memory set обнуляется).
    if disc_id:
        from data.telegram_ids import chat_id_variants
        _known_discussion_groups.update(chat_id_variants(disc_id))
        _persist_discussion_group(disc_id)
    items = []
    try:
        async for m in client.iter_messages(disc_peer,
                                             reply_to=top.id,
                                             limit=int(limit)):
            sender = await m.get_sender() if m.from_id else None
            author = "Аноним"
            if sender is not None:
                author = (" ".join(filter(None, [
                    getattr(sender, "first_name", None),
                    getattr(sender, "last_name", None),
                ])) or getattr(sender, "username", None)
                    or getattr(sender, "title", None) or "Аноним")
            media_kind = _comment_media_kind(m)
            items.append({
                "id": int(m.id),
                "author": author,
                "text": m.text or "",
                "date": m.date.isoformat() if m.date else None,
                "outgoing": bool(getattr(m, "out", False)),
                "has_media": media_kind is not None,
                "media_kind": media_kind,
            })
    except Exception:  # noqa: BLE001
        # iter_messages может упасть если у канала нет comments вообще.
        pass
    # iter_messages возвращает свежие сверху — переворачиваем, чтобы
    # старые шли первыми (как в самом Telegram).
    items.reverse()
    return {"available": True, "items": items,
            "discussion_chat_id": disc_id, "top_msg_id": int(top.id)}


def get_comments(chat_id, msg_id, limit=50, user_id=None):
    """Sync-обёртка для _get_comments."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    data = _call(_get_comments(chat_id, msg_id, limit, user_id=user_id),
                 timeout=60)
    # Если discussion-группа известна, заодно подчистим «фейковый»
    # Contact (если он уже успел создаться до того, как мы её узнали).
    disc_id = data.get("discussion_chat_id") if isinstance(data, dict) else None
    if disc_id:
        try:
            _cleanup_discussion_contact(disc_id)
        except Exception:  # noqa: BLE001
            pass
    return data


async def _download_comment_media(disc_chat_id, msg_id, user_id=None):
    """Скачивает медиа конкретного комментария по (disc_chat_id, msg_id).
    Возвращает (bytes, mime) или (None, None)."""
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    msg = await client.get_messages(int(disc_chat_id), ids=int(msg_id))
    if msg is None or msg.media is None:
        return None, None
    data = await client.download_media(msg, file=bytes)
    mime = "image/jpeg"
    doc = getattr(msg, "document", None)
    if doc is not None and getattr(doc, "mime_type", None):
        mime = doc.mime_type
    return data, mime


def download_comment_media(disc_chat_id, msg_id, user_id=None):
    """Sync-обёртка."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_download_comment_media(disc_chat_id, msg_id,
                                         user_id=user_id), timeout=60)


async def _send_comment(discussion_chat_id, top_msg_id, text, user_id=None):
    """Отправляет комментарий в linked discussion group канала."""
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    sent = await client.send_message(int(discussion_chat_id), text,
                                      reply_to=int(top_msg_id))
    return {"id": int(sent.id) if sent else None}


def send_comment(discussion_chat_id, top_msg_id, text, user_id=None):
    """Sync-обёртка для _send_comment."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    # Запоминаем discussion-группу ДО отправки, чтобы echo собственного
    # сообщения не успел породить Contact. Параллельно сохраняем
    # в БД — для устойчивости к рестарту.
    from data.telegram_ids import chat_id_variants
    _known_discussion_groups.update(chat_id_variants(discussion_chat_id))
    _persist_discussion_group(int(discussion_chat_id))
    result = _call(_send_comment(discussion_chat_id, top_msg_id, text,
                                 user_id=user_id), timeout=30)
    # И на всякий случай чистим, если он всё-таки успел создаться.
    try:
        _cleanup_discussion_contact(int(discussion_chat_id))
    except Exception:  # noqa: BLE001
        pass
    return result


async def _fetch_profile_photos(chat_id, limit, user_id=None):
    """Возвращает список id всех фотографий профиля пользователя/чата.
    Бинарник каждого скачивается лениво по запросу — здесь только id."""
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    try:
        entity = await client.get_entity(int(chat_id))
    except Exception:  # noqa: BLE001
        return []
    photos = []
    async for ph in client.iter_profile_photos(entity, limit=int(limit)):
        photos.append({"id": int(getattr(ph, "id", 0))})
    return photos


def fetch_profile_photos(chat_id, limit=20, user_id=None):
    """Sync-обёртка."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_fetch_profile_photos(chat_id, limit, user_id=user_id),
                 timeout=60)


async def _download_profile_photo_by_id(chat_id, photo_id, user_id=None):
    """Скачивает конкретное фото профиля по id (через iter_profile_photos
    с лимитом 50 — обычно у людей сильно меньше)."""
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    try:
        entity = await client.get_entity(int(chat_id))
    except Exception:  # noqa: BLE001
        return None
    async for ph in client.iter_profile_photos(entity, limit=50):
        if int(getattr(ph, "id", 0)) == int(photo_id):
            return await client.download_media(ph, file=bytes)
    return None


def download_profile_photo_by_id(chat_id, photo_id, user_id=None):
    """Sync-обёртка."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_download_profile_photo_by_id(chat_id, photo_id,
                                               user_id=user_id), timeout=120)


async def _download_profile_photo(chat_id, user_id=None):
    """Скачивает текущую аватарку Telegram-сущности по id или username."""
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    try:
        entity = await client.get_entity(
            chat_id if isinstance(chat_id, str) else int(chat_id))
        return await client.download_profile_photo(entity, file=bytes)
    except Exception:  # noqa: BLE001
        return None


def download_profile_photo(chat_id, user_id=None):
    """Sync-обёртка для текущей аватарки профиля."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_download_profile_photo(chat_id, user_id=user_id),
                 timeout=60)


async def _resolve_entity_info(chat_id, user_id=None):
    """Получает имя/тип Telegram-сущности (пользователь / группа / канал)
    по её peer-id. Нужно, чтобы создавать локальный Contact для
    пользователей, с которыми мы ещё не переписывались (клик по
    участнику группы или по общему чату в профиле)."""
    from telethon.tl.types import User as _User, Chat as _Chat, Channel as _Channel
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    e = await client.get_entity(chat_id if isinstance(chat_id, str) else int(chat_id))
    if isinstance(e, _User):
        name = " ".join(filter(None, [
            getattr(e, "first_name", None),
            getattr(e, "last_name", None),
        ])) or getattr(e, "username", None) or f"user{e.id}"
        return {
            "kind": "private",
            "chat_id": int(e.id),
            "title": name,
            "username": getattr(e, "username", None),
        }
    if isinstance(e, _Channel):
        return {
            "kind": "channel" if getattr(e, "broadcast", False) else "group",
            "chat_id": int(e.id),
            "title": getattr(e, "title", "") or f"chat{e.id}",
            "username": getattr(e, "username", None),
        }
    if isinstance(e, _Chat):
        return {
            "kind": "group",
            "chat_id": int(e.id),
            "title": getattr(e, "title", "") or f"chat{e.id}",
            "username": None,
        }
    raise RuntimeError("Неизвестный тип Telegram-сущности")


def resolve_entity_info(chat_id, user_id=None):
    """Sync-обёртка для _resolve_entity_info."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_resolve_entity_info(chat_id, user_id=user_id), timeout=30)


def resolve_username_info(username, user_id=None):
    """Проверяет Telegram @username и возвращает данные найденной сущности."""
    username = (username or "").strip()
    if username.startswith("@"):
        username = username[1:]
    if not username:
        raise RuntimeError("Пустой username")
    return resolve_entity_info(username, user_id=user_id)


async def _set_block(chat_id, block, user_id=None):
    """Block/Unblock пользователя в Telegram. block=True — заблокировать,
    block=False — снять блокировку."""
    from telethon.tl.functions.contacts import (BlockRequest,
                                                 UnblockRequest)
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    input_user = await client.get_input_entity(int(chat_id))
    if block:
        await client(BlockRequest(id=input_user))
    else:
        await client(UnblockRequest(id=input_user))


def set_block(chat_id, block=True, user_id=None):
    """Заблокировать (или разблокировать) пользователя в самом Telegram."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_set_block(chat_id, bool(block), user_id=user_id), timeout=30)


async def _set_mute(chat_id, muted, user_id=None):
    """Mute/Unmute Telegram-диалога через настройки уведомлений."""
    import datetime as _dt
    from telethon.tl import functions, types
    owner = _normalize_user_id(user_id)
    client = await _get_client(owner)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    peer = await client.get_input_entity(int(chat_id))
    if muted:
        mute_until = _dt.datetime(2038, 1, 19, 3, 14, 7,
                                  tzinfo=_dt.timezone.utc)
    else:
        mute_until = _dt.datetime.fromtimestamp(0, tz=_dt.timezone.utc)
    settings = types.InputPeerNotifySettings(mute_until=mute_until)
    await client(functions.account.UpdateNotifySettingsRequest(
        peer=types.InputNotifyPeer(peer),
        settings=settings,
    ))
    _set_cached_mute_state(owner, chat_id, muted)
    _apply_telegram_mute_state(owner, chat_id, muted)


def set_mute(chat_id, muted=True, user_id=None):
    """Выключить или включить звук у Telegram-диалога."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_set_mute(chat_id, bool(muted), user_id=user_id), timeout=30)


async def _set_archive(chat_id, archived, user_id=None):
    """Перенести Telegram-диалог в архив или вернуть в общий список."""
    owner = _normalize_user_id(user_id)
    client = await _get_client(owner)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    peer = await client.get_input_entity(int(chat_id))
    await client.edit_folder(peer, 1 if archived else 0)
    try:
        from telethon import utils
        resolved_chat_id = int(utils.get_peer_id(peer))
    except Exception:  # noqa: BLE001
        resolved_chat_id = int(chat_id)
    _set_cached_archive_state(owner, resolved_chat_id, archived)
    _apply_telegram_archive_state(
        owner, resolved_chat_id, archived,
        allowed_types=_archive_handle_types_for_peer(peer))


def set_archive(chat_id, archived=True, user_id=None):
    """Архивировать или вернуть Telegram-диалог в самом Telegram."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_set_archive(chat_id, bool(archived), user_id=user_id), timeout=30)


async def _send_message(chat_id, text, reply_to=None, parse_mode=None,
                         silent=False, schedule=None, user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    # Регистрируем ДО отправки: Telegram пришлёт это сообщение обратно
    # как исходящее событие, и обработчик должен его узнать и не дублировать.
    # При parse_mode='md' Telethon снимает markdown-метки из text — мы
    # кладём в self-sent тот же plain, что web-panel уже сохранила в БД,
    # чтобы дедупликация по тексту совпала.
    # Для scheduled-сообщений echo не придёт сразу, поэтому в self_sent
    # не пишем — оно нужно только для дедупа реального события.
    if not schedule:
        if parse_mode == 'md':
            try:
                from telethon.extensions import markdown as _tg_md
                plain, _ = _tg_md.parse(text)
                _recent_self_sent.append((int(chat_id), plain, time.monotonic()))
            except Exception:  # noqa: BLE001
                _recent_self_sent.append((int(chat_id), text, time.monotonic()))
        else:
            _recent_self_sent.append((int(chat_id), text, time.monotonic()))
    kwargs = {"reply_to": reply_to, "parse_mode": parse_mode}
    if silent:
        kwargs["silent"] = True
    if schedule:
        kwargs["schedule"] = schedule
    sent = await client.send_message(int(chat_id), text, **kwargs)
    return getattr(sent, "id", None)


def send_message(chat_id, text, reply_to=None, parse_mode=None,
                  silent=False, schedule=None, user_id=None):
    """Отправляет текст в Telegram-чат от имени владельца аккаунта.
    `reply_to` — id telegram-сообщения, на которое отвечаем (или None).
    `parse_mode='md'` — Telethon разберёт markdown.
    `silent=True` — сообщение без уведомления у получателя.
    `schedule=datetime` — отправить отложенно (попадёт в Scheduled).
    Возвращает id отправленного сообщения (для scheduled — id будущего)."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_send_message(chat_id, text, reply_to, parse_mode,
                               silent, schedule, user_id=user_id))


async def _send_file(chat_id, data, filename, caption, reply_to=None,
                     parse_mode=None, silent=False, schedule=None,
                     user_id=None, voice_note=False):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    import io
    bio = io.BytesIO(data)
    bio.name = filename or "file"
    kwargs = {"caption": caption or None, "reply_to": reply_to,
              "parse_mode": parse_mode}
    if voice_note:
        kwargs["voice_note"] = True
        if (filename or "").lower().endswith(".ogg"):
            kwargs["mime_type"] = "audio/ogg"
        try:
            from telethon.tl.types import DocumentAttributeAudio
            kwargs["attributes"] = [
                DocumentAttributeAudio(duration=0, voice=True)
            ]
        except Exception:  # noqa: BLE001
            pass
    if silent:
        kwargs["silent"] = True
    if schedule:
        kwargs["schedule"] = schedule
    sent = await client.send_file(int(chat_id), bio, **kwargs)
    sent_id = getattr(sent, "id", None)
    if not schedule and sent_id is not None:
        now = time.monotonic()
        _recent_self_sent_ids[:] = [
            row for row in _recent_self_sent_ids if now - row[3] < 120]
        _recent_self_sent_ids.append((
            _normalize_user_id(user_id), int(chat_id), int(sent_id), now))
    return sent_id


def send_file(chat_id, data, filename, caption="", reply_to=None,
              parse_mode=None, silent=False, schedule=None, user_id=None,
              voice_note=False):
    """Отправляет файл в Telegram-чат. Поддерживает `silent` (без звука)
    и `schedule` (отложенная отправка — datetime). Возвращает id
    отправленного Telegram-сообщения."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_send_file(chat_id, data, filename, caption, reply_to,
                            parse_mode, silent, schedule, user_id=user_id,
                            voice_note=voice_note),
                 timeout=120)


async def _queued_file_send(chat_id, data, filename, caption, callback,
                            reply_to=None, parse_mode=None, silent=False,
                            schedule=None, user_id=None, voice_note=False):
    """Фоновая оболочка: Telethon работает в своём loop, callback с БД —
    в worker-thread, чтобы не задерживать входящие Telegram-события."""
    sent_id = None
    error = None
    try:
        global _file_send_semaphore, _file_send_semaphore_loop
        running_loop = asyncio.get_running_loop()
        if (_file_send_semaphore is None
                or _file_send_semaphore_loop is not running_loop):
            try:
                limit = max(1, int(os.environ.get(
                    "TELEGRAM_FILE_SEND_CONCURRENCY", "2")))
            except ValueError:
                limit = 2
            _file_send_semaphore = asyncio.Semaphore(limit)
            _file_send_semaphore_loop = running_loop
        async with _file_send_semaphore:
            payload = (await asyncio.to_thread(data)
                       if callable(data) else data)
            if payload is None:
                raise RuntimeError("Локальный файл отправки недоступен")
            sent_id = await asyncio.wait_for(
                _send_file(
                    chat_id, payload, filename, caption, reply_to, parse_mode,
                    silent, schedule, user_id=user_id,
                    voice_note=voice_note),
                timeout=180)
    except Exception as exc:  # noqa: BLE001
        error = exc
        _state_for(user_id)["error"] = f"media send: {exc}"
    if callback is not None:
        callback_error = None
        for attempt in range(3):
            try:
                await asyncio.to_thread(callback, sent_id, error)
                callback_error = None
                break
            except Exception as exc:  # noqa: BLE001
                callback_error = exc
                if attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
        if callback_error is not None:
            _state_for(user_id)["error"] = (
                f"media callback: {callback_error}")
    return sent_id


def queue_file(chat_id, data, filename, caption="", callback=None,
               reply_to=None, parse_mode=None, silent=False, schedule=None,
               user_id=None, voice_note=False):
    """Ставит медиа в уже существующий Telethon-loop и сразу возвращает
    concurrent Future. HTTP-запрос не ждёт загрузку файла в Telegram."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _ensure_loop()
    try:
        max_pending = max(1, int(os.environ.get(
            "TELEGRAM_FILE_QUEUE_MAX", "8")))
    except ValueError:
        max_pending = 8
    global _pending_file_sends
    with _pending_file_sends_lock:
        if _pending_file_sends >= max_pending:
            raise RuntimeError(
                "Очередь медиа заполнена — повторите отправку позже")
        _pending_file_sends += 1
    try:
        future = asyncio.run_coroutine_threadsafe(
            _queued_file_send(
                chat_id, data, filename, caption, callback, reply_to,
                parse_mode, silent, schedule, user_id=user_id,
                voice_note=voice_note),
            _loop)
    except Exception:
        with _pending_file_sends_lock:
            _pending_file_sends = max(0, _pending_file_sends - 1)
        raise

    def _release_slot(_future):
        global _pending_file_sends
        with _pending_file_sends_lock:
            _pending_file_sends = max(0, _pending_file_sends - 1)

    future.add_done_callback(_release_slot)
    return future


async def _delete_message(chat_id, message_id, revoke, user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    message_ids = (message_id if isinstance(message_id, (list, tuple, set))
                   else [message_id])
    await client.delete_messages(int(chat_id),
                                 [int(value) for value in message_ids],
                                 revoke=bool(revoke))


def delete_message(chat_id, message_id, revoke=True, user_id=None):
    """Удаляет сообщение в самом Telegram. revoke=True — у всех (где это
    разрешено правилами TG: своё личное/групповое сообщение, либо если ты
    админ группы). revoke=False — удалить только из своего клиента."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_delete_message(chat_id, message_id, revoke, user_id=user_id))


def delete_messages(chat_id, message_ids, revoke=True, user_id=None):
    """Удаляет несколько сообщений одним Telegram-запросом."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    ids = [int(message_id) for message_id in message_ids]
    if not ids:
        return
    _call(_delete_message(chat_id, ids, revoke, user_id=user_id))


async def _edit_message(chat_id, message_id, text, user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    await client.edit_message(int(chat_id), int(message_id), text)


def edit_message(chat_id, message_id, text, user_id=None):
    """Редактирует своё сообщение в Telegram."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_edit_message(chat_id, message_id, text, user_id=user_id))


async def _pin_message(chat_id, message_id, notify, user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    await client.pin_message(int(chat_id), int(message_id),
                             notify=notify)


def pin_message(chat_id, message_id, notify=False, user_id=None):
    """Закрепляет сообщение в Telegram-чате. `notify=False` — закрепляем
    «тихо» (без шумного «вы закрепили это сообщение» всем участникам).
    Это симметрично UI-сценарию «📌 в контекстном меню»."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_pin_message(chat_id, message_id, notify, user_id=user_id))


async def _unpin_message(chat_id, message_id, user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    await client.unpin_message(int(chat_id), int(message_id))


def unpin_message(chat_id, message_id, user_id=None):
    """Снимает закреп с сообщения в Telegram-чате."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_unpin_message(chat_id, message_id, user_id=user_id))


async def _fetch_forum_topics(chat_id: int, user_id=None) -> list:
    """Тянет список тем у Telegram-форума через MTProto.
    Возвращает [{id, title, top_message_id}] или [] если чат не форум /
    Telethon не настроен."""
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        return []
    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest
        entity = await client.get_input_entity(int(chat_id))
        # offset_date=0/offset_id=0 — с начала, limit=100 (хватает на
        # большинство форумов; для очень крупных можно пагинировать).
        res = await client(GetForumTopicsRequest(
            channel=entity, offset_date=0, offset_id=0,
            offset_topic=0, limit=100))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for t in (getattr(res, "topics", None) or []):
        tid = getattr(t, "id", None)
        title = getattr(t, "title", None)
        if tid is None or not title:
            continue
        out.append({
            "id": int(tid),
            "title": title,
            "top_message_id": getattr(t, "top_message", None),
        })
    return out


# Кэш на список тем форум-чата. Запрос MTProto тяжёлый (~0.5-2 сек),
# а fetch_forum_topics дёргается при каждом открытии форум-контакта.
# Без кэша интерфейс тупит как раз там, где должен быстро отвечать.
_forum_topics_cache = {}  # chat_id -> (topics, monotonic_ts)
_FORUM_TOPICS_TTL = 60  # секунд


def fetch_forum_topics(chat_id: int, user_id=None) -> list:
    """Sync-обёртка с кэшем на 60 сек. Тянет темы форума с сервера
    Telegram через MTProto. Возвращает пустой список при любой ошибке."""
    if not is_configured() or not telethon_available():
        return []
    owner = _normalize_user_id(user_id)
    now = time.monotonic()
    cache_key = (owner, int(chat_id))
    cached = _forum_topics_cache.get(cache_key)
    if cached and now - cached[1] < _FORUM_TOPICS_TTL:
        return cached[0]
    try:
        topics = _call(_fetch_forum_topics(chat_id, user_id=owner),
                       timeout=20)
    except Exception:  # noqa: BLE001
        topics = []
    # Кэшируем даже пустой ответ — иначе при «not a forum» мы будем
    # лупиться по MTProto на каждом открытии. Пустота протухнет за 60 с.
    _forum_topics_cache[cache_key] = (topics, now)
    return topics


def invalidate_forum_topics_cache(chat_id: int = None):
    """Сбросить кэш (всю или для одного чата). Вызывать когда мы знаем,
    что в форуме создалась/переименовалась тема (например после новой
    `MessageActionTopicCreate`)."""
    if chat_id is None:
        _forum_topics_cache.clear()
    else:
        _forum_topics_cache.pop(int(chat_id), None)


async def _forward_message(source_chat_id, message_id, target_chat_id,
                           user_id=None):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    client = await _get_client(owner)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    # forward_messages принимает list — берём первый из результата.
    res = await client.forward_messages(
        int(target_chat_id), [int(message_id)], int(source_chat_id))
    msg = res[0] if isinstance(res, list) else res

    # Записываем пересланное сразу в нашу БД, иначе UI ждёт NewMessage-эха
    # из Telethon, которое для forward'нутых нередко приходит с задержкой
    # или вовсе не приходит — и сообщение «теряется» в нашем чате, хотя в
    # Telegram оно ушло. Anti-dupe в `_handle_message` отбросит эхо, если
    # оно всё-таки прилетит позже.
    if msg is not None:
        try:
            chat = await client.get_entity(int(target_chat_id))
            chat_key = _chat_title(chat)
            from telethon.tl.types import (User as _TgUser, Chat as _TgChat,
                                           Channel as _TgChannel)
            if isinstance(chat, _TgUser):
                chat_type = "private"
            elif isinstance(chat, _TgChannel) and not getattr(chat, "megagroup", False):
                chat_type = "channel"
            else:
                chat_type = "group"
            kind = _media_kind(msg)
            text = getattr(msg, "message", None) or ""
            if text or kind is not None:
                await _persist_telegram_message(
                    msg, int(target_chat_id), chat, chat_key, chat_type,
                    True, "Вы", kind, text, user_id=owner, client=client)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"forward_persist: {exc}"

    return getattr(msg, "id", None)


def forward_message(source_chat_id, message_id, target_chat_id, user_id=None):
    """Пересылает сообщение из source-чата в target-чат через Telethon.
    Возвращает id нового сообщения в target."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_forward_message(source_chat_id, message_id, target_chat_id,
                                  user_id=user_id))


async def _forward_messages_bulk(source_chat_id, message_ids, target_chat_id,
                                 user_id=None):
    """Массовый forward — одним запросом гонит несколько сообщений.
    Telegram сохраняет порядок и группирует медиа-альбомы. Локально для
    каждого результата пишем Messages+Attachment, чтобы UI не ждал echo."""
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    client = await _get_client(owner)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    ids = [int(x) for x in message_ids]
    res = await client.forward_messages(
        int(target_chat_id), ids, int(source_chat_id))
    msgs = res if isinstance(res, list) else [res]
    sent_ids = []
    try:
        chat = await client.get_entity(int(target_chat_id))
        chat_key = _chat_title(chat)
        from telethon.tl.types import (User as _TgUser, Chat as _TgChat,
                                       Channel as _TgChannel)
        if isinstance(chat, _TgUser):
            chat_type = "private"
        elif isinstance(chat, _TgChannel) and not getattr(
                chat, "megagroup", False):
            chat_type = "channel"
        else:
            chat_type = "group"
        for msg in msgs:
            if msg is None:
                continue
            sent_ids.append(int(getattr(msg, "id", 0)))
            try:
                kind = _media_kind(msg)
                text = getattr(msg, "message", None) or ""
                if text or kind is not None:
                    await _persist_telegram_message(
                        msg, int(target_chat_id), chat, chat_key,
                        chat_type, True, "Вы", kind, text,
                        user_id=owner, client=client)
            except Exception as exc:  # noqa: BLE001
                state["error"] = f"forward_bulk_persist: {exc}"
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"forward_bulk_entity: {exc}"
    return sent_ids


def forward_messages_bulk(source_chat_id, message_ids, target_chat_id,
                          user_id=None):
    """Sync-обёртка: пересылает список сообщений одним вызовом."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_forward_messages_bulk(
        source_chat_id, message_ids, target_chat_id, user_id=user_id),
        timeout=120)


def _sticker_set_from_message(msg):
    """Возвращает stickerset из Telegram-стикера или None для одиночного файла."""
    document = getattr(msg, "document", None)
    for attr in getattr(document, "attributes", []) or []:
        if getattr(attr, "stickerset", None) is not None:
            stickerset = attr.stickerset
            try:
                from telethon.tl.types import InputStickerSetEmpty
                if isinstance(stickerset, InputStickerSetEmpty):
                    return None
            except Exception:  # noqa: BLE001
                pass
            return stickerset
    return None


async def _save_sticker_from_message(chat_id, message_id, mode,
                                     user_id=None):
    owner = _normalize_user_id(user_id)
    client = await _get_client(owner)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    msg = await client.get_messages(int(chat_id), ids=int(message_id))
    if msg is None or not getattr(msg, "sticker", None):
        raise RuntimeError("Это сообщение не является стикером")
    document = getattr(msg, "document", None)
    if document is None:
        raise RuntimeError("Telegram не отдал файл стикера")
    if mode == "single":
        from telethon.tl.functions.messages import FaveStickerRequest
        await client(FaveStickerRequest(id=document, unfave=False))
        return {"ok": True, "mode": "single"}
    if mode == "pack":
        stickerset = _sticker_set_from_message(msg)
        if stickerset is None:
            raise RuntimeError("У этого стикера нет доступного стикерпака")
        from telethon.tl.functions.messages import InstallStickerSetRequest
        await client(InstallStickerSetRequest(stickerset=stickerset,
                                              archived=False))
        return {"ok": True, "mode": "pack"}
    raise RuntimeError("Неизвестный режим сохранения")


def save_sticker_from_message(chat_id, message_id, mode, user_id=None):
    """Сохраняет Telegram-стикер: mode=single в избранное, mode=pack ставит пак."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_save_sticker_from_message(
        chat_id, message_id, mode, user_id=user_id), timeout=30)


async def _sticker_pack_from_message(chat_id, message_id, user_id=None):
    owner = _normalize_user_id(user_id)
    client = await _get_client(owner)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    msg = await client.get_messages(int(chat_id), ids=int(message_id))
    if msg is None or not getattr(msg, "sticker", None):
        raise RuntimeError("Это сообщение не является стикером")
    stickerset = _sticker_set_from_message(msg)
    if stickerset is None:
        raise RuntimeError("У этого стикера нет доступного стикерпака")
    from telethon.tl.functions.messages import GetStickerSetRequest
    pack = await client(GetStickerSetRequest(stickerset=stickerset, hash=0))
    docs = list(getattr(pack, "documents", []) or [])
    pack_set = getattr(pack, "set", None)
    title = getattr(pack_set, "title", None) or "Стикерпак"
    pack_key = _sticker_pack_key_from_set(stickerset, pack_set)
    items = []
    for doc in docs:
        mime = getattr(doc, "mime_type", None) or "application/octet-stream"
        alt = ""
        for attr in getattr(doc, "attributes", []) or []:
            alt = getattr(attr, "alt", None) or alt
        data = await client.download_media(doc, file=bytes)
        if not data:
            continue
        items.append({
            "id": str(getattr(doc, "id", "") or ""),
            "item_key": str(getattr(doc, "id", "") or ""),
            "mime": mime,
            "alt": alt,
            "data_url": "data:{};base64,{}".format(
                mime, base64.b64encode(data).decode("ascii")),
        })
    return {"ok": True, "title": title, "count": len(items),
            "pack_key": pack_key, "stickers": items}


def sticker_pack_from_message(chat_id, message_id, user_id=None):
    """Возвращает название и inline-превью стикеров из пака исходного сообщения."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_sticker_pack_from_message(
        chat_id, message_id, user_id=user_id), timeout=90)


async def _send_reaction(chat_id, message_id, emoji, user_id=None):
    """Toggle: emoji=None или '' снимает мою реакцию."""
    from telethon.tl.functions.messages import SendReactionRequest
    from telethon.tl.types import ReactionEmoji
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    entity = await client.get_input_entity(int(chat_id))
    reactions = []
    if emoji:
        reactions = [ReactionEmoji(emoticon=emoji)]
    await client(SendReactionRequest(peer=entity, msg_id=int(message_id),
                                     reaction=reactions))


def send_reaction(chat_id, message_id, emoji, user_id=None):
    """Поставить (или снять, если emoji пуст) реакцию на сообщение."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_send_reaction(chat_id, message_id, emoji, user_id=user_id))


def status(user_id=None, refresh=True) -> dict:
    if refresh and is_configured() and telethon_available():
        try:
            _call(_refresh_status(user_id), timeout=10)
        except Exception as exc:  # noqa: BLE001
            _state_for(user_id)["error"] = (
                "Не удалось обновить соединение с Telegram. "
                "Попробуйте ещё раз чуть позже.")
            logger.warning("Telegram status refresh failed for user_id=%s: %s",
                           _normalize_user_id(user_id),
                           exc.__class__.__name__)
    state = _state_for(user_id)
    resend_available_at = float(state.get("resend_available_at") or 0)
    resend_seconds = max(0, math.ceil(resend_available_at - time.time()))
    return {
        "available": telethon_available(),
        "configured": is_configured(),
        "authorized": state["authorized"],
        "needs_password": state["needs_password"],
        "phone": state["phone"],
        "awaiting_code": bool(state.get("phone_code_hash")),
        "code_hint": state["code_hint"],
        "resend_supported": bool(state.get("resend_supported")),
        "resend_method": state.get("resend_method"),
        "resend_seconds": resend_seconds,
        "resend_available_at_ms": int(resend_available_at * 1000),
        # В state/log остаётся техническая причина, но в HTML не выводим
        # внутренние английские исключения Telethon/SQLite.
        "error": ("Telegram временно недоступен. Мост автоматически "
                  "попробует подключиться снова."
                  if state["error"] else None),
        "last_media_skip": state["last_media_skip"],
        "skip_muted": _skip_muted(),
        "skip_archived": _skip_archived(),
        "ghost_mode": ghost_mode_enabled(),
    }
