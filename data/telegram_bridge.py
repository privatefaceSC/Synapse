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
import json
import os
import threading
import time

_loop = None
_thread = None
_client = None
_clients = {}
_handler_registered = False
_handler_registered_users = set()
_refresh_task = None
# chat_id диалогов, сообщения из которых мост игнорирует
# (архив + выключенные уведомления). Обновляется периодически.
_skip_chat_ids = set()
# Недавние отправки из веб-панели (chat_id, text, monotonic-время) —
# чтобы не записать их повторно, когда Telegram пришлёт их обратно
# как исходящее событие.
_recent_self_sent = []
# chat_id -> monotonic-время, до которого считаем, что собеседник печатает.
_typing = {}
_DEFAULT_MEDIA_MAX_MB = 20
_STATE_TEMPLATE = {
    "phone": None,
    "phone_code_hash": None,
    "code_hint": None,
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
    """Игнорировать ли сообщения из чатов с выключенными уведомлениями.
    Настройка из веб-панели имеет приоритет над переменной окружения."""
    s = _load_settings()
    if "skip_muted" in s:
        return bool(s["skip_muted"])
    return _env_flag("TELEGRAM_SKIP_MUTED", True)


def _skip_archived():
    """Игнорировать ли сообщения из архивированных чатов."""
    s = _load_settings()
    if "skip_archived" in s:
        return bool(s["skip_archived"])
    return _env_flag("TELEGRAM_SKIP_ARCHIVED", True)


def update_filters(skip_muted=None, skip_archived=None, user_id=None):
    """Меняет настройки фильтрации (из веб-панели) и сразу пересобирает
    кэш, чтобы изменение применилось без перезапуска сервера."""
    s = _load_settings()
    if skip_muted is not None:
        s["skip_muted"] = bool(skip_muted)
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
    return fut.result(timeout=timeout)


async def _get_client(user_id=None):
    global _client
    user_id = _normalize_user_id(user_id)
    if user_id in _clients:
        return _clients[user_id]
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
    if getattr(msg, "video", None) or getattr(msg, "gif", None):
        return "video"
    if getattr(msg, "voice", None):
        return "voice"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "sticker", None):
        return "sticker"
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


def _save_attachment(db, user_id, message_id, kind, data, msg):
    """Шифрует и кладёт медиа в media/<user_id>/, создаёт Attachment.
    Хранилище и шифрование — те же, что у Android-клиента."""
    import uuid

    from data.attachments import Attachment
    from data.crypto import encrypt_bytes

    root = _media_root()
    rel_dir = str(user_id)
    os.makedirs(os.path.join(root, rel_dir), exist_ok=True)
    stored_path = f"{rel_dir}/{uuid.uuid4().hex}.enc"
    with open(os.path.join(root, stored_path), "wb") as f:
        f.write(encrypt_bytes(data))

    file_obj = getattr(msg, "file", None)
    att = Attachment(
        user_id=user_id,
        message_id=message_id,
        kind=kind,
        mime=getattr(file_obj, "mime_type", None),
        original_name=getattr(file_obj, "name", None),
        stored_path=stored_path,
        size=len(data),
        dedup_key=None,
    )
    db.add(att)
    db.commit()


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
    try:
        data = await msg.download_media(file=bytes)
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"download: {exc}"
        return None, None
    if data is None:
        state["last_media_skip"] = f"{kind}: Telethon не вернул данные"
        return None, None
    state["last_media_skip"] = None
    return kind, data


async def _maybe_fetch_avatar(chat, chat_id, user_id=None, client=None):
    """Лениво скачивает фото профиля чата и сохраняет его контакту.
    Качает только если у контакта аватара ещё нет."""
    from data import db_sessions
    from data.contacts import Contact, MessengerHandle
    from data.crypto import encrypt_bytes

    owner = _normalize_user_id(user_id)
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
        if contact is None or contact.avatar_path:
            return
        contact_id = contact.id

        if client is None:
            client = await _get_client(owner)
        photo = await client.download_profile_photo(chat, file=bytes)
        if not photo:
            return  # у чата нет фото профиля

        rel_dir = f"{owner}/tg_avatars"
        os.makedirs(os.path.join(_media_root(), rel_dir), exist_ok=True)
        rel_path = f"{rel_dir}/{contact_id}.enc"
        with open(os.path.join(_media_root(), rel_path), "wb") as f:
            f.write(encrypt_bytes(photo))
        contact.avatar_path = rel_path
        db.commit()
    finally:
        db.close()


async def _handle_message(event, user_id=None, client=None):
    # Пропускаем чаты из архива и с выключенными уведомлениями.
    if event.chat_id in _skip_chat_ids:
        return
    # Discussion-группы каналов (комментарии) — не создаём из них
    # отдельный Contact в БД. См. _known_discussion_groups.
    _ensure_discussion_groups_loaded()
    if event.chat_id in _known_discussion_groups:
        return
    msg = event.message
    is_out = bool(getattr(msg, "out", False))

    # Anti-dupe: при пересылке (и в принципе любых исходящих, записанных
    # синхронно из веб-панели) мы уже создали запись в БД с этим
    # tg_message_id. Если эхо прилетело — не дублируем.
    tg_message_id_for_dupe = getattr(msg, "id", None)
    if tg_message_id_for_dupe is not None:
        from data import db_sessions
        from data.contacts import MessengerHandle
        from data.users import Messages as _Messages
        db = db_sessions.create_session()
        try:
            handle_ids = [hid for (hid,) in db.query(MessengerHandle.id).filter(
                MessengerHandle.user_id == _normalize_user_id(user_id),
                MessengerHandle.messenger_name == "Telegram",
                MessengerHandle.tg_chat_id == event.chat_id).all()]
            if handle_ids:
                exists = db.query(_Messages.id).filter(
                    _Messages.user_id == _normalize_user_id(user_id),
                    _Messages.tg_message_id == int(tg_message_id_for_dupe),
                    _Messages.handle_id.in_(handle_ids)).first()
                if exists is not None:
                    return
        finally:
            db.close()

    # Контакт = сам чат: для лички это собеседник, для группы/канала —
    # название чата. event.get_chat() возвращает собеседника и для
    # входящих, и для исходящих, поэтому исходящие тоже попадают куда надо.
    chat = await event.get_chat()
    chat_key = _chat_title(chat)

    if event.is_private:
        chat_type = "private"
    elif event.is_group:
        chat_type = "group"
    else:
        chat_type = "channel"

    # Автор подписи над сообщением.
    if is_out:
        author = "Вы"
    elif chat_type == "group":
        author = _sender_name(await event.get_sender())
    else:  # личка или канал
        author = chat_key

    kind = _media_kind(msg)
    text = msg.message or ""
    if not text and kind is None:
        # Ни текста, ни понятного вложения (например системное событие).
        return

    # Своё сообщение, отправленное через веб-панель, уже записано
    # маршрутом /send — не дублируем его эхом из Telegram.
    if is_out and _pop_self_sent(event.chat_id, text):
        return

    await _persist_telegram_message(msg, event.chat_id, chat, chat_key,
                                    chat_type, is_out, author, kind, text,
                                    user_id=user_id, client=client)


async def _persist_telegram_message(msg, chat_id, chat, chat_key, chat_type,
                                    is_out, author, kind, text, user_id=None,
                                    client=None):
    """Скачивает медиа (если есть), пишет запись в БД и тянет аватар чата.
    Вынесено из `_handle_message`, чтобы тем же кодом сохранять и сообщения,
    созданные синхронно прямо из веб-панели (forward / send) — иначе UI ждёт
    NewMessage-эха из Telethon, которое может задержаться или потеряться."""
    # Скачиваем медиа, если оно есть и не слишком большое.
    kind, data = await _download_media_payload(msg, kind, user_id=user_id)

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

    from data import db_sessions
    from data.contacts import record_message
    db = db_sessions.create_session()
    try:
        owner = _normalize_user_id(user_id)
        message = record_message(db, owner, "Telegram", chat_key, text,
                                 tg_chat_id=chat_id, author=author,
                                 outgoing=is_out, tg_chat_type=chat_type,
                                 tg_message_id=getattr(msg, "id", None),
                                 reply_to_tg_id=reply_to_tg_id,
                                 tg_ttl_seconds=ttl,
                                 fwd_from_name=fwd_name,
                                 fwd_from_tg_chat_id=fwd_chat_id,
                                 tg_topic_id=int(topic_id) if topic_id else None,
                                 tg_topic_title=topic_title,
                                 tg_is_forum=is_forum_chat,
                                 text_html=text_html)
        # message is None — контакт в блок-листе, медиа тоже пропускаем.
        if message is not None and data is not None and kind is not None:
            _save_attachment(db, owner, message.id, kind, data, msg)
    finally:
        db.close()

    # Фото профиля собеседника/группы — лениво, один раз.
    try:
        await _maybe_fetch_avatar(chat, chat_id, user_id=user_id,
                                  client=client)
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


async def _register_handler(user_id=None, client=None):
    global _handler_registered
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    if owner in _handler_registered_users:
        return
    from telethon import events
    if client is None:
        client = await _get_client(owner)

    # Без incoming=True — ловим и входящие, и исходящие (мои ответы
    # с любого устройства Telegram тоже попадают в ленту).
    @client.on(events.NewMessage())
    async def _on_new(event):
        try:
            await _handle_message(event, user_id=owner, client=client)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"incoming: {exc}"

    # Событие «печатает…» + смена онлайн-статуса. UserUpdate приходит и на
    # то, и на другое — какие именно поля выставлены, зависит от Telegram.
    @client.on(events.UserUpdate())
    async def _on_user_update(event):
        try:
            if getattr(event, "typing", False):
                _typing[event.chat_id] = time.monotonic() + 6
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
        try:
            await _handle_read_outbox(update, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"read_outbox: {exc}"

    # Удаление сообщений собеседником или мной с другого устройства.
    # У нас в БД сообщение не сносится, а помечается deleted_at — в ленте
    # вместо текста показывается «🗑 Сообщение удалено».
    @client.on(events.MessageDeleted())
    async def _on_deleted(event):
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
        try:
            await _handle_reactions(update, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"reactions: {exc}"

    # Редактирование сообщений (мои с другого устройства и собеседника).
    # Telegram в UI показывает только финальный текст с пометкой «ред.»;
    # мы храним ВСЕ прошлые версии в `message_edits`, чтобы видеть, что
    # человек хотел сказать изначально.
    @client.on(events.MessageEdited())
    async def _on_edited(event):
        try:
            await _handle_edited(event, user_id=owner)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"edited: {exc}"

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
                _save_attachment(db, owner, target.id, saved_kind, data, msg)
                target_has_media = True
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
    try:
        exp = _typing.get(int(chat_id))
    except (TypeError, ValueError):
        return False
    return exp is not None and exp > time.monotonic()


def _is_muted(dialog) -> bool:
    """True, если у диалога выключены уведомления (mute_until в будущем)."""
    import datetime as _dt
    ns = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    mute_until = getattr(ns, "mute_until", None)
    if mute_until is None:
        return False
    now = _dt.datetime.now(_dt.timezone.utc)
    if mute_until.tzinfo is None:
        mute_until = mute_until.replace(tzinfo=_dt.timezone.utc)
    return mute_until > now


async def _refresh_filter_cache(user_id=None, client=None):
    """Пересобирает множество chat_id, которые мост игнорирует."""
    global _skip_chat_ids
    owner = _normalize_user_id(user_id)
    if client is None:
        client = _clients.get(owner)
    if client is None:
        return
    skip_muted, skip_archived = _skip_muted(), _skip_archived()
    if not skip_muted and not skip_archived:
        _skip_chat_ids = set()
        return
    skip = set()
    async for d in client.iter_dialogs():
        if (skip_archived and getattr(d, "archived", False)) or \
                (skip_muted and _is_muted(d)):
            skip.add(d.id)
    _skip_chat_ids = skip


async def _periodic_refresh(user_id=None):
    """Раз в 5 минут обновляет кэш фильтрации (чаты мьютят/архивируют
    уже после старта моста)."""
    while True:
        await asyncio.sleep(300)
        try:
            await _refresh_filter_cache(user_id)
        except Exception as exc:  # noqa: BLE001
            _state_for(user_id)["error"] = f"filter: {exc}"


async def _activate(user_id=None, client=None):
    """Общий «после авторизации»: вешает обработчик входящих, строит
    кэш фильтрации и запускает периодическое обновление."""
    global _refresh_task
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    if client is None:
        client = await _get_client(owner)
    await _register_handler(owner, client=client)
    try:
        await _refresh_filter_cache(owner, client=client)
    except Exception as exc:  # noqa: BLE001
        state["error"] = f"filter: {exc}"
    if _refresh_task is None:
        _refresh_task = asyncio.ensure_future(_periodic_refresh(owner))


async def _startup(user_id=None):
    # Подгружаем известные discussion-группы каналов из БД, чтобы
    # события о новых комментариях (приходящие сразу после старта моста)
    # не успели породить «призрачный» Contact.
    _ensure_discussion_groups_loaded()
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    client = await _get_client(owner)
    if await client.is_user_authorized():
        state["authorized"] = True
        if owner not in _handler_registered_users:
            await _activate(owner, client=client)


def _quiet_telethon_logging():
    """Глушит шумные WARNING'и Telethon о неудачных попытках подключения:
    без VPN до серверов Telegram не достучаться — это ожидаемо, и валить
    этим консоль не нужно. Реальная ошибка всё равно видна на /telegram."""
    import logging
    logging.getLogger("telethon").setLevel(logging.CRITICAL)


async def _safe_startup(user_id=None):
    try:
        await _startup(user_id)
    except Exception as exc:  # noqa: BLE001
        _state_for(user_id)["error"] = str(exc)


async def _refresh_status(user_id=None):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
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
    asyncio.run_coroutine_threadsafe(_safe_startup(user_id), _loop)


async def _request_code(phone, user_id=None, force_sms=False):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    client = await _get_client(owner)
    if await client.is_user_authorized():
        state["authorized"] = True
        return
    sent = await client.send_code_request(phone, force_sms=bool(force_sms))
    state["phone"] = phone
    state["phone_code_hash"] = sent.phone_code_hash
    state["code_hint"] = _sent_code_hint(sent)
    state["needs_password"] = False
    state["error"] = None


def request_code(phone, user_id=None, force_sms=False):
    _call(_request_code(phone.strip(), user_id=user_id,
                        force_sms=force_sms))


async def _submit_code(code, user_id=None):
    from telethon.errors import SessionPasswordNeededError
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    client = await _get_client(owner)
    try:
        await client.sign_in(
            state["phone"], code,
            phone_code_hash=state["phone_code_hash"])
    except SessionPasswordNeededError:
        state["needs_password"] = True
        return
    state["authorized"] = True
    state["needs_password"] = False
    await _activate(owner, client=client)


def submit_code(code, user_id=None):
    _call(_submit_code(code.strip(), user_id=user_id))


async def _submit_password(password, user_id=None):
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    client = await _get_client(owner)
    await client.sign_in(password=password)
    state["authorized"] = True
    state["needs_password"] = False
    await _activate(owner, client=client)


def submit_password(password, user_id=None):
    _call(_submit_password(password, user_id=user_id))


async def _logout(user_id=None):
    global _client, _handler_registered, _refresh_task, _skip_chat_ids
    owner = _normalize_user_id(user_id)
    state = _state_for(owner)
    if _refresh_task is not None:
        _refresh_task.cancel()
        _refresh_task = None
    client = _clients.pop(owner, None)
    if client is not None:
        try:
            await client.log_out()
        except Exception:  # noqa: BLE001
            pass
    if owner == _owner_user_id():
        _client = None
        _state.update({"authorized": False, "needs_password": False,
                       "phone": None, "phone_code_hash": None, "error": None,
                       "last_media_skip": None})
    _handler_registered = False
    _handler_registered_users.discard(owner)
    _skip_chat_ids = set()
    state.update(dict(_STATE_TEMPLATE))


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
                     user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    import io
    bio = io.BytesIO(data)
    bio.name = filename or "file"
    kwargs = {"caption": caption or None, "reply_to": reply_to,
              "parse_mode": parse_mode}
    if silent:
        kwargs["silent"] = True
    if schedule:
        kwargs["schedule"] = schedule
    sent = await client.send_file(int(chat_id), bio, **kwargs)
    return getattr(sent, "id", None)


def send_file(chat_id, data, filename, caption="", reply_to=None,
              parse_mode=None, silent=False, schedule=None, user_id=None):
    """Отправляет файл в Telegram-чат. Поддерживает `silent` (без звука)
    и `schedule` (отложенная отправка — datetime). Возвращает id
    отправленного Telegram-сообщения."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    return _call(_send_file(chat_id, data, filename, caption, reply_to,
                            parse_mode, silent, schedule, user_id=user_id),
                 timeout=120)


async def _delete_message(chat_id, message_id, revoke, user_id=None):
    client = await _get_client(user_id)
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram не авторизован")
    await client.delete_messages(int(chat_id), [int(message_id)],
                                 revoke=bool(revoke))


def delete_message(chat_id, message_id, revoke=True, user_id=None):
    """Удаляет сообщение в самом Telegram. revoke=True — у всех (где это
    разрешено правилами TG: своё личное/групповое сообщение, либо если ты
    админ группы). revoke=False — удалить только из своего клиента."""
    if not is_configured() or not telethon_available():
        raise RuntimeError("Telegram-мост не настроен")
    _call(_delete_message(chat_id, message_id, revoke, user_id=user_id))


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


def status(user_id=None) -> dict:
    if is_configured() and telethon_available():
        try:
            _call(_refresh_status(user_id), timeout=10)
        except Exception as exc:  # noqa: BLE001
            _state_for(user_id)["error"] = str(exc)
    state = _state_for(user_id)
    return {
        "available": telethon_available(),
        "configured": is_configured(),
        "authorized": state["authorized"],
        "needs_password": state["needs_password"],
        "phone": state["phone"],
        "code_hint": state["code_hint"],
        "error": state["error"],
        "last_media_skip": state["last_media_skip"],
        "skip_muted": _skip_muted(),
        "skip_archived": _skip_archived(),
        "ghost_mode": ghost_mode_enabled(),
    }
