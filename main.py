import logging
import os
import random
import re
import shutil
import string
import subprocess
import tempfile
import threading
import time
import uuid
import base64
import hashlib
from datetime import datetime, timedelta

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session)
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash

from data import db_sessions
from data.users import Messages, User


logger = logging.getLogger(__name__)


_AVATAR_PALETTE = [
    "#ef4444", "#f59e0b", "#10b981", "#3b82f6",
    "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
]
SYNAPSE_MESSENGER = "Synapse"
CREATOR_USER_IDS = {1}
LEGACY_CREATOR_USERNAMES = {"ivan", "dfyzkjcmrjd_cdby"}
CREATOR_BADGE = "Создатель"
STICKER_COLLECTION_DISABLED_DETAIL = "Функция в разработке."
USERS_ADMIN_EMAILS = {"korobka170111@gmail.com"}


_TELEGRAM_RESTORABLE_MEDIA_KINDS = {
    'image', 'video', 'video_note', 'voice', 'audio', 'file',
}
_MEDIA_RESTORE_WORKERS = 2
_MEDIA_RESTORE_MAX_PENDING = 8
_MEDIA_RESTORE_MAX_PER_USER = 2
_media_restore_locks = tuple(threading.Lock() for _ in range(64))
_media_restore_slots = threading.BoundedSemaphore(_MEDIA_RESTORE_WORKERS)
_media_restore_jobs = {}
_media_restore_jobs_guard = threading.Lock()
_MEDIA_RESTORE_JOB_TTL_SECONDS = 10 * 60
_PRESENCE_ONLINE_SECONDS = 75
_PRESENCE_WRITE_INTERVAL_SECONDS = 20
_NOTIFICATION_REPLY_MAX_AGE_SECONDS = 180
_NOTIFICATION_REPLY_RETRY_SECONDS = 12
_RETRYABLE_NOTIFICATION_REPLY_ERRORS = {
    'no_active_notification',
    'no_cached_reply',
    'pending_intent_dead',
    'pending_intent_canceled',
}


def _media_restore_lock(key):
    """Полосатые локи без бесконечного роста dict на старых message id."""
    return _media_restore_locks[hash(key) % len(_media_restore_locks)]


def _sticker_collection_disabled_response():
    return jsonify({
        'error': 'sticker_collection_in_development',
        'detail': STICKER_COLLECTION_DISABLED_DETAIL,
    }), 501


def _configured_creator_ids() -> set[int]:
    raw = os.environ.get('SKILLWOOD_CREATOR_IDS')
    if raw is None:
        return set(CREATOR_USER_IDS)
    ids = set()
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


def _is_creator_user(user) -> bool:
    if bool(getattr(user, 'is_creator', False)):
        return True
    try:
        if int(getattr(user, 'id', 0) or 0) in _configured_creator_ids():
            return True
    except (TypeError, ValueError):
        pass
    return False


def _is_legacy_creator_user(user) -> bool:
    username = (getattr(user, 'username', None) or '').strip().lower()
    return username in LEGACY_CREATOR_USERNAMES


def _username_reserved_for_creator(username: str) -> bool:
    return (username or '').strip().lower() in LEGACY_CREATOR_USERNAMES


def _seed_legacy_creator_flags(db) -> None:
    """Одноразово переносим старую привязку creator-username в флаг БД.

    После этого бейдж живёт от user.id/is_creator: если создатель сменит
    username, статус не исчезнет.
    """
    if not LEGACY_CREATOR_USERNAMES:
        return
    from sqlalchemy import func

    rows = (db.query(User)
            .filter(func.lower(User.username).in_(
                sorted(LEGACY_CREATOR_USERNAMES)))
            .filter(User.is_creator.isnot(True))
            .all())
    if not rows:
        return
    for user in rows:
        user.is_creator = True
    db.commit()


def _creator_user_ids(db) -> set[int]:
    from sqlalchemy import or_

    _seed_legacy_creator_flags(db)
    configured_ids = _configured_creator_ids()
    filters = [User.is_creator.is_(True)]
    if configured_ids:
        filters.append(User.id.in_(configured_ids))
    rows = db.query(User).filter(or_(*filters)).all()
    changed = False
    ids = set()
    for user in rows:
        ids.add(user.id)
        if not bool(getattr(user, 'is_creator', False)):
            user.is_creator = True
            changed = True
    if changed:
        db.flush()
    return ids


def _mark_contact_creator_from_handles(contact, handles, creator_ids):
    contact.is_creator = any(
        h.messenger_name == SYNAPSE_MESSENGER
        and _synapse_partner_id(h) in creator_ids
        for h in handles
    )
    contact.creator_title = CREATOR_BADGE if contact.is_creator else ''
    return contact


def _mark_profile_badges(user):
    user.is_creator = _is_creator_user(user)
    user.creator_title = CREATOR_BADGE if user.is_creator else ''
    return user


def _configure_timezone():
    os.environ.setdefault("TZ", "Europe/Moscow")
    if hasattr(time, "tzset"):
        time.tzset()


def _avatar_for(contact):
    name = (contact.display_name or "?").strip()
    contact.initial = name[:1].upper() if name else "?"
    contact.avatar_color = _AVATAR_PALETTE[contact.id % len(_AVATAR_PALETTE)]
    contact.avatar_url = _contact_avatar_url(contact)
    return contact


def _contact_avatar_url(contact):
    avatar_path = getattr(contact, 'avatar_path', None)
    if avatar_path and _media_rel_path_exists(avatar_path):
        return f'/contacts/{contact.id}/photo'
    try:
        cached = _cached_tg_avatar_photo_id(contact)
    except Exception:  # noqa: BLE001
        cached = None
    return (f'/contacts/{contact.id}/avatar/{cached}' if cached else None)


def _media_rel_path_exists(rel_path):
    if not rel_path:
        return False
    try:
        return os.path.isfile(_safe_media_full_path(rel_path))
    except (OSError, ValueError):
        return False


def _safe_media_full_path(rel_path):
    """Преобразует DB-relative media path, не позволяя выйти из media/."""
    if not rel_path or os.path.isabs(str(rel_path)):
        raise ValueError('Некорректный путь медиа')
    root = os.path.realpath(_media_root())
    full = os.path.realpath(os.path.join(root, str(rel_path)))
    try:
        inside = os.path.commonpath([root, full]) == root
    except ValueError as exc:
        raise ValueError('Некорректный путь медиа') from exc
    if not inside:
        raise ValueError('Некорректный путь медиа')
    return full


def _telegram_placeholder_media_kind(text):
    """Определяет тип старого Telegram-медиа, у которого не осталось row.

    Такие записи появились до введения ленивого кэша: в БД есть только
    технический текст, но Telegram message id всё ещё позволяет вернуть файл.
    """
    # Только строки, которые сам bridge использовал как технические
    # заглушки. По одному эмодзи определять нельзя: обычное сообщение
    # «📷 фото с прогулки» иначе исчезнет и превратится в ложную кнопку.
    return {
        '📷 Фото': 'image',
        '🎬 Видео': 'video',
        '🎤 Голосовое сообщение': 'voice',
        '🎙 Голосовое': 'voice',
        '🎵 Аудио': 'audio',
        '📎 Файл': 'file',
        '📎 Вложение': 'file',
    }.get((text or '').strip())


def _telegram_media_is_restorable(message, kind):
    return bool(
        message is not None
        and kind in _TELEGRAM_RESTORABLE_MEDIA_KINDS
        and getattr(message, 'messenger_name', None) == 'Telegram'
        and getattr(message, 'tg_message_id', None) is not None
        and getattr(message, 'tg_ttl_seconds', None) is None
        and getattr(message, 'deleted_at', None) is None
        and getattr(message, 'delivery_status', None) in (None, 'sent')
    )


def _attachment_availability(attachment, message):
    if _media_rel_path_exists(getattr(attachment, 'stored_path', None)):
        return 'local'
    if _telegram_media_is_restorable(message, getattr(attachment, 'kind', None)):
        return 'remote'
    return 'missing'


def _cached_tg_avatar_photo_id(contact):
    if not getattr(contact, 'user_id', None) or not getattr(contact, 'id', None):
        return None
    cache_dir = os.path.join(
        _media_root(), str(contact.user_id), 'tg_avatars', str(contact.id))
    if not os.path.isdir(cache_dir):
        return None
    candidates = []
    for name in os.listdir(cache_dir):
        stem, ext = os.path.splitext(name)
        if ext != '.enc' or not stem.isdigit():
            continue
        full = os.path.join(cache_dir, name)
        candidates.append((os.path.getmtime(full), stem))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _user_presence(user, now=None):
    """Компактное состояние присутствия для HTML и JSON."""
    now = now or datetime.now()
    last_seen = getattr(user, 'last_seen_at', None)
    online = bool(
        last_seen is not None
        and last_seen >= now - timedelta(seconds=_PRESENCE_ONLINE_SECONDS)
    )
    if online:
        detail = 'Сейчас на сайте'
    elif last_seen is None:
        detail = 'Ещё не заходил(а)'
    elif last_seen.date() == now.date():
        detail = f'Последний вход: сегодня в {last_seen:%H:%M}'
    elif last_seen.date() == (now - timedelta(days=1)).date():
        detail = f'Последний вход: вчера в {last_seen:%H:%M}'
    else:
        detail = f'Последний вход: {last_seen:%d.%m.%Y} в {last_seen:%H:%M}'
    return {
        'online': online,
        'label': 'В сети' if online else 'Не в сети',
        'detail': detail,
        'last_seen_at_iso': _iso_dt(last_seen),
    }


def _enrich_with_last_message(db, contacts):
    from data.contacts import Contact, MessengerHandle
    from sqlalchemy import func

    creator_ids = _creator_user_ids(db)
    if not contacts:
        return contacts

    contact_ids = [c.id for c in contacts]
    contacts_by_id = {c.id: c for c in contacts}
    handles_by_contact = {c.id: [] for c in contacts}
    handles = (
        db.query(MessengerHandle)
        .filter(MessengerHandle.contact_id.in_(contact_ids))
        .order_by(MessengerHandle.id.asc())
        .all()
    )
    for handle in handles:
        handles_by_contact.setdefault(handle.contact_id, []).append(handle)

    synapse_partner_ids = {
        partner_id
        for handle in handles
        if handle.messenger_name == SYNAPSE_MESSENGER
        for partner_id in [_synapse_partner_id(handle)]
        if partner_id is not None
    }
    synapse_users = {
        user.id: user
        for user in (db.query(User)
                     .filter(User.id.in_(synapse_partner_ids)).all())
    } if synapse_partner_ids else {}

    for c in contacts:
        _avatar_for(c)
        contact_handles = handles_by_contact.get(c.id, [])
        _mark_contact_creator_from_handles(c, contact_handles, creator_ids)
        # Уникальные мессенджеры контакта (для «папки» с выбором чата).
        msgrs = []
        for h in contact_handles:
            if h.messenger_name not in msgrs:
                msgrs.append(h.messenger_name)
        c.messengers = msgrs
        c.last_preview = None
        c.last_time = None
        c.last_at = None
        c.last_outgoing = False
        c.last_tg_read = None
        c.unread_count = 0
        c.presence = None
        for handle in contact_handles:
            if handle.messenger_name != SYNAPSE_MESSENGER:
                continue
            partner = synapse_users.get(_synapse_partner_id(handle))
            if partner is not None:
                c.presence = _user_presence(partner)
                break
        if c.presence is None:
            # Telegram presence хранится в памяти моста и читается здесь без
            # сетевого запроса, поэтому список чатов остаётся быстрым.
            from data import telegram_bridge
            for handle in contact_handles:
                if (handle.messenger_name == 'Telegram'
                        and handle.tg_chat_id is not None
                        and not _is_group_or_channel_handle(handle)):
                    c.presence = telegram_bridge.presence_status(
                        handle.tg_chat_id, user_id=c.user_id)
                    if c.presence is not None:
                        break

    if contact_ids:
        ranked = (
            db.query(
                MessengerHandle.contact_id.label('contact_id'),
                Messages.id.label('message_id'),
                func.row_number().over(
                    partition_by=MessengerHandle.contact_id,
                    order_by=(
                        Messages.created_at.desc().nullslast(),
                        Messages.id.desc(),
                    ),
                ).label('rn'),
            )
            .join(Messages, Messages.handle_id == MessengerHandle.id)
            .filter(MessengerHandle.contact_id.in_(contact_ids))
            .filter(or_(Messages.delivery_status.is_(None),
                        Messages.delivery_status != 'scheduled'))
            .subquery()
        )
        last_rows = (
            db.query(ranked.c.contact_id, ranked.c.message_id)
            .filter(ranked.c.rn == 1)
            .all()
        )
        last_ids = [row.message_id for row in last_rows]
        if last_ids:
            messages_by_id = {
                m.id: m for m in
                db.query(Messages).filter(Messages.id.in_(last_ids)).all()
            }
            for row in last_rows:
                contact = contacts_by_id.get(row.contact_id)
                last = messages_by_id.get(row.message_id)
                if contact is None or last is None:
                    continue
                contact.last_preview = last.text
                contact.last_time = last.time
                contact.last_at = last.created_at
                contact.last_outgoing = bool(last.outgoing)
                contact.last_tg_read = (
                    bool(last.tg_read_at) if last.tg_message_id else None
                )

        # Свои исходящие в «непрочитанные» не считаем — иначе после отправки
        # сообщения собственный чат подсвечивается красным «1». В Telegram,
        # очевидно, тоже не подсвечивает то, что ты сам только что написал.
        unread_rows = (
            db.query(MessengerHandle.contact_id, func.count(Messages.id))
            .join(Messages, Messages.handle_id == MessengerHandle.id)
            .join(Contact, Contact.id == MessengerHandle.contact_id)
            .filter(MessengerHandle.contact_id.in_(contact_ids))
            .filter(or_(Messages.outgoing.is_(None),
                        Messages.outgoing.is_(False)))
            .filter(or_(Contact.last_read_at.is_(None),
                        Messages.created_at > Contact.last_read_at))
            .group_by(MessengerHandle.contact_id)
            .all()
        )
        for contact_id, count in unread_rows:
            contact = contacts_by_id.get(contact_id)
            if contact is not None:
                contact.unread_count = int(count or 0)

    # Сортировка: сначала pinned (по времени закрепления, новые pin'ы выше),
    # потом обычные по времени последнего сообщения.
    contacts.sort(
        key=lambda c: (
            1 if c.pinned_at else 0,
            c.pinned_at or datetime.min,
            c.last_at or datetime.min,
        ),
        reverse=True,
    )
    return contacts


def _filter_discussion_contacts(db, contacts):
    """Убирает из UI контакты linked discussion-групп каналов.

    Комментарии живут в отдельной шторке под конкретным постом, а не как
    самостоятельные чаты в левом списке.
    """
    if not contacts:
        return contacts
    try:
        from data.contacts import MessengerHandle
        from data.discussion_groups import DiscussionGroup
        from data.telegram_ids import chat_id_variants
        disc_ids = set()
        for (tg_chat_id,) in db.query(DiscussionGroup.tg_chat_id).all():
            disc_ids.update(chat_id_variants(tg_chat_id))
        if not disc_ids:
            return contacts
        contact_ids = {cid for (cid,) in db.query(
            MessengerHandle.contact_id
        ).filter(MessengerHandle.tg_chat_id.in_(disc_ids)).all()}
        if not contact_ids:
            return contacts
        return [c for c in contacts if c.id not in contact_ids]
    except Exception:  # noqa: BLE001
        return contacts


def _telegram_reply_handle(handles):
    """Возвращает Telegram-личность контакта с известным chat_id (на неё
    можно отправить ответ из веб-панели), либо None."""
    for h in handles:
        if (h.messenger_name == 'Telegram' and h.tg_chat_id is not None
                and h.tg_chat_type != 'channel'):
            return h
    return None


def _message_date_label(value):
    if value is None:
        return ''
    today = datetime.now().date()
    day = value.date()
    if day == today:
        return 'Сегодня'
    if day == today - timedelta(days=1):
        return 'Вчера'
    months = [
        'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
        'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
    ]
    if day.year == today.year:
        return f'{day.day} {months[day.month - 1]}'
    return day.strftime('%d.%m.%Y')


def _notify_webpush_message(message_id):
    if not message_id:
        return
    try:
        from data import webpush
        result = webpush.notify_message(message_id)
        if result.get('failed') or result.get('disabled'):
            print(f"Web Push: message={message_id} result={result}")
    except Exception as exc:  # noqa: BLE001
        print(f"Web Push: message={message_id} error={exc}")


def _commit_best_effort(db, context: str) -> bool:
    """Пробуем записать неключевое состояние, но не ломаем чтение чата.

    Например, при переполненной квоте SQLite может отказать на journal-файле:
    открыть переписку всё равно полезнее, чем вернуть пользователю 500.
    """
    try:
        db.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        print(f"Не удалось сохранить служебное состояние ({context}): {exc}")
        return False


def _message_author_avatar_url(msg):
    if (getattr(msg, 'author_avatar_path', None)
            and _media_rel_path_exists(msg.author_avatar_path)
            and not getattr(msg, 'outgoing', False)):
        return f'/messages/{int(msg.id)}/author-photo'
    author_id = getattr(msg, 'author_tg_chat_id', None)
    if author_id and not getattr(msg, 'outgoing', False):
        return f'/contacts/telegram-author/{int(author_id)}/photo'
    return None


def _message_author_profile_url(msg):
    author_id = getattr(msg, 'author_tg_chat_id', None)
    if author_id and not getattr(msg, 'outgoing', False):
        return f'/contacts/from-tg/{int(author_id)}'
    return None


def _is_group_handle(handle) -> bool:
    return bool(getattr(handle, 'is_group', False)
                or getattr(handle, 'tg_chat_type', None) == 'group')


def _is_group_or_channel_handle(handle) -> bool:
    return bool(_is_group_handle(handle)
                or getattr(handle, 'tg_chat_type', None) == 'channel')


def _form_bool(value) -> bool:
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _archive_mode_from_request() -> bool:
    return _form_bool(request.args.get('archived'))


def _filter_archived_contacts(contacts, archive_mode: bool):
    return [c for c in contacts
            if bool(getattr(c, 'archived', False)) == bool(archive_mode)]


def _reply_channel(handles):
    """Какой канал ответа доступен для этих хэндлов.

    Приоритет — Telegram через Telethon (полнофункциональный: текст, медиа,
    reply, edit). Иначе — `notification reply` через Android-клиент: ищем
    хэндл с известным `package_name` (только текст, требует активного
    уведомления в шторке телефона). Возвращает `(tg_handle, notif_handle)` —
    хотя бы один из них None."""
    tg = _telegram_reply_handle(handles)
    if tg is not None:
        return tg, None
    for h in handles:
        if _package_for_handle(h):
            return None, h
    return None, None


# Дефолтные `package_name` для известных мессенджеров. Используются как
# fallback для старых handles, у которых поле `package_name` ещё пустое
# (запись была создана до того, как Android-клиент начал его слать). Без
# этого MAX/VK/WhatsApp выглядели бы read-only до прихода нового входящего.
# Ключи в нижнем регистре — lookup case-insensitive, иначе для handle с
# messenger_name='MAX' (как реально приходит из Android-уведомлений Max)
# fallback не срабатывал и композер не показывался.
_DEFAULT_PACKAGE_NAMES = {
    'max': 'ru.oneme.app',
    'вконтакте': 'com.vkontakte.android',
    'whatsapp': 'com.whatsapp',
}


def _package_for_handle(handle) -> str | None:
    """Реальный или дефолтный `package_name` для хэндла."""
    if handle.package_name:
        return handle.package_name
    name = (handle.messenger_name or '').strip().lower()
    return _DEFAULT_PACKAGE_NAMES.get(name)


_MESSENGER_PRIORITY = ('Telegram', 'Max', 'ВКонтакте', 'WhatsApp')


def _pick_messenger(available, requested=None):
    """Какой чат-мессенджер показать у контакта-«папки». Если запрошенный
    есть — он; иначе приоритетный; иначе первый."""
    if requested and requested in available:
        return requested
    for m in _MESSENGER_PRIORITY:
        if m in available:
            return m
    return available[0] if available else None


# Признаки markdown-разметки в тексте: **жирный**, ||спойлер||, `моноширный`,
# [текст](url), __подчёркивание__, ~~зачёркивание~~. Если ничего из этого
# нет — не дёргаем parse_mode, отдаём plain. Регулярка намеренно простая;
# даже если ложное срабатывание попадёт в Telethon, тот вернёт plain
# (markdown-парсер мягкий, не падает на «лишних» звёздочках).
_MARKDOWN_RE = re.compile(
    r"\*\*.+?\*\*|__.+?__|~~.+?~~|\|\|.+?\|\||`.+?`|\[[^\]]+\]\([^)]+\)",
    re.DOTALL)


def _has_markdown(text: str) -> bool:
    return bool(text and _MARKDOWN_RE.search(text))


def _attach_media(db, msgs):
    from data.attachments import Attachment
    from data.matching import is_media_placeholder
    ids = [m.id for m in msgs]
    by_msg = {}
    if ids:
        for a in (db.query(Attachment)
                  .filter(Attachment.message_id.in_(ids))
                  .order_by(Attachment.id.asc()).all()):
            by_msg.setdefault(a.message_id, []).append(a)
    for m in msgs:
        m.media = by_msg.get(m.id, [])
        for attachment in m.media:
            attachment.availability = _attachment_availability(attachment, m)
            attachment.url = (f'/attachments/{attachment.id}'
                              if attachment.availability == 'local' else None)
            attachment.restore_url = (
                f'/attachments/{attachment.id}/restore'
                if attachment.availability == 'remote' else None)
        restore_kind = _telegram_placeholder_media_kind(m.text)
        if m.media or not _telegram_media_is_restorable(m, restore_kind):
            restore_kind = None
        m.media_restore_kind = restore_kind
        m.media_restore_url = (
            f'/messages/{m.id}/telegram-media/restore'
            if restore_kind else None)
        # Если у сообщения есть вложение, а текст — это технический
        # плейсхолдер вида «📷 Фото», прячем его: само фото и так в bubble.
        # Реальная подпись остаётся как есть.
        if (m.media or m.media_restore_kind) and is_media_placeholder(m.text):
            m.visible_text = ''
            m.visible_text_html = None
        else:
            m.visible_text = m.text or ''
            # text_html отдаём только если visible_text непустой —
            # иначе UI получит «пустой html» и нарисует пустой <div>.
            m.visible_text_html = getattr(m, 'text_html', None) or None
    return msgs


def _attach_reactions(db, msgs):
    """Подгружает реакции (emoji + count + mine) к каждому сообщению."""
    from data.reactions import MessageReaction
    ids = [m.id for m in msgs]
    by_msg = {}
    if ids:
        for r in (db.query(MessageReaction)
                  .filter(MessageReaction.message_id.in_(ids))
                  .order_by(MessageReaction.id.asc()).all()):
            by_msg.setdefault(r.message_id, []).append(
                {'emoji': r.emoji, 'count': r.count, 'mine': bool(r.mine)})
    for m in msgs:
        m.reactions = by_msg.get(m.id, [])
    return msgs


def _topics_full(db, contact_id, topic_id, handle_ids,
                 topic_filter_args, max_id, cap):
    """ПОЛНЫЙ анализ: тянем ВСЕ сообщения (или последние `cap`, если
    их слишком много), извлекаем темы чанками, переписываем сохранённый
    набор. Используется при первом анализе и при force=1.

    Эвристика-фолбэк: если LLM вернула тему с одним только start_id —
    дозаполняем message_ids сегментом до следующей темы (с разрывом
    по времени).

    Возвращает {count: N} (сколько проанализировано) или
    {error: <jsonable_dict>} при ошибке LLM."""
    from data import ollama as _ollama
    from data.chat_topics import replace_topics as _replace_topics
    from data.users import Messages

    msgs = (db.query(Messages)
            .filter(*topic_filter_args)
            .filter(Messages.deleted_at.is_(None))
            .order_by(Messages.created_at.asc().nullsfirst(),
                      Messages.id.asc())
            .all())
    # Если сообщений больше cap — берём ПОСЛЕДНИЕ cap (свежие важнее).
    if len(msgs) > cap:
        msgs = msgs[-cap:]
    items = [{'id': m.id, 'text': (m.text or '').strip()}
             for m in msgs if (m.text or '').strip()]
    if not items:
        return {'error': {'status': 'no_messages', 'topics': []}}

    try:
        topics = _ollama.extract_topics_chunked(items)
    except RuntimeError as exc:
        return {'error': {'status': 'llm_error', 'detail': str(exc)}}

    valid_ids = {it['id'] for it in items}
    msg_by_id = {m.id: m for m in msgs}
    clean = []
    seen_starts = set()
    for t in topics:
        sid = t.get('start_id')
        if sid not in valid_ids or sid in seen_starts:
            continue
        seen_starts.add(sid)
        related = [mid for mid in (t.get('message_ids') or [sid])
                   if mid in valid_ids]
        if sid not in related:
            related = [sid] + related
        clean.append({
            'title': t['title'],
            'start_id': sid,
            'message_ids': related,
        })

    if clean:
        from datetime import timedelta as _td
        pos_by_id = {it['id']: i for i, it in enumerate(items)}
        clean.sort(key=lambda c: pos_by_id.get(c['start_id'], 0))
        max_gap = _td(minutes=60)
        for idx, tt in enumerate(clean):
            if len(tt['message_ids']) > 1:
                continue
            sid = tt['start_id']
            start_pos = pos_by_id[sid]
            end_pos = len(items)
            if idx + 1 < len(clean):
                np_ = pos_by_id.get(clean[idx + 1]['start_id'])
                if np_ is not None and np_ > start_pos:
                    end_pos = np_
            related = {sid}
            prev_time = (msg_by_id[sid].created_at
                         if sid in msg_by_id else None)
            for j in range(start_pos, end_pos):
                cur_id = items[j]['id']
                cur_msg = msg_by_id.get(cur_id)
                cur_time = cur_msg.created_at if cur_msg else None
                if (prev_time is not None and cur_time is not None
                        and cur_time - prev_time > max_gap):
                    break
                related.add(cur_id)
                if cur_time is not None:
                    prev_time = cur_time
            tt['message_ids'] = sorted(
                related, key=lambda mid: pos_by_id.get(mid, 0))

        _replace_topics(db, contact_id, topic_id, clean,
                        analyzed_count=len(items),
                        fingerprint_max_id=max_id)
    return {'count': len(items)}


def _topics_incremental(db, contact_id, topic_id, handle_ids,
                        topic_filter_args, saved_fp, max_id):
    """ИНКРЕМЕНТАЛЬНЫЙ анализ: берём только сообщения с id > saved_fp
    (то, что появилось после прошлого анализа) и классифицируем их —
    LLM решает, добавить к существующей теме или завести новую.

    Если новых сообщений с текстом меньше 3 — просто обновляем
    fingerprint и не дёргаем LLM (не стоит того).

    Возвращает {count: N} или {error: ...}."""
    from data import ollama as _ollama
    from data.chat_topics import (get_topics as _get_topics,
                                   append_to_topic as _append_to_topic,
                                   add_topic as _add_topic,
                                   bump_fingerprint as _bump_fingerprint,
                                   ChatTopic)
    from data.users import Messages

    new_msgs = (db.query(Messages)
                .filter(*topic_filter_args)
                .filter(Messages.deleted_at.is_(None))
                .filter(Messages.id > saved_fp)
                .order_by(Messages.created_at.asc().nullsfirst(),
                          Messages.id.asc())
                .all())
    new_items = [{'id': m.id, 'text': (m.text or '').strip()}
                 for m in new_msgs if (m.text or '').strip()]
    if len(new_items) < 3:
        # Слишком мало нового материала — обновляем fingerprint и выходим
        # (иначе при каждом тике LLM будет дёргаться без толку).
        _bump_fingerprint(db, contact_id, topic_id, max_id)
        return {'count': len(new_items)}

    # Готовим существующие темы для LLM: title + один пример текста
    # (start-сообщение, чтобы LLM поняла контекст темы).
    saved = _get_topics(db, contact_id, topic_id)
    if not saved:
        # Кэш есть в fingerprint, но тем нет — fallback к полному.
        return _topics_full(db, contact_id, topic_id, handle_ids,
                            topic_filter_args, max_id, 5000)

    start_ids = [s['start_id'] for s in saved]
    msg_by_id = {m.id: m for m in (
        db.query(Messages)
        .filter(Messages.id.in_(start_ids))
        .all())}
    existing_for_llm = []
    for s in saved:
        ms = msg_by_id.get(s['start_id'])
        existing_for_llm.append({
            'title': s['title'],
            'sample_text': (ms.text or '') if ms else '',
        })

    try:
        cls = _ollama.classify_new_messages(existing_for_llm, new_items)
    except RuntimeError as exc:
        return {'error': {'status': 'llm_error', 'detail': str(exc)}}

    # Группируем классификации:
    # 1) добавление к существующей теме (по индексу) — список message_ids
    # 2) новые темы — сгруппированы по title (LLM может разнести подряд
    #    идущие сообщения одной темы под одним title — мерджим).
    append_map = {}      # index → [msg_ids]
    new_topics_map = {}  # title.lower() → {'title': str, 'ids': [ids]}
    for c in cls:
        if c['topic'] == 'NEW':
            key = c['title'].strip().lower()
            if key not in new_topics_map:
                new_topics_map[key] = {'title': c['title'].strip(),
                                       'ids': []}
            new_topics_map[key]['ids'].append(c['id'])
        else:
            idx = c['topic']
            if 0 <= idx < len(saved):
                append_map.setdefault(idx, []).append(c['id'])

    # Применяем: достаём id-шники строк ChatTopic для append.
    saved_rows = (db.query(ChatTopic)
                  .filter(ChatTopic.contact_id == contact_id,
                          ChatTopic.topic_id.is_(None) if topic_id is None
                          else ChatTopic.topic_id == int(topic_id))
                  .order_by(ChatTopic.position.asc(),
                            ChatTopic.id.asc()).all())
    for idx, mids in append_map.items():
        if idx < len(saved_rows):
            _append_to_topic(db, saved_rows[idx].id, mids)
    for entry in new_topics_map.values():
        ids = sorted(set(entry['ids']))
        if not ids:
            continue
        _add_topic(db, contact_id, topic_id,
                   title=entry['title'],
                   start_id=ids[0],
                   message_ids=ids,
                   fingerprint_max_id=max_id)
    _bump_fingerprint(db, contact_id, topic_id, max_id)
    return {'count': len(new_items)}


def _topics_with_time(db, topics, handle_ids):
    """Добавляет к каждой теме `time`/`date` начала — для UI.
    Тянет одним запросом по start_id, чтобы не дёргать в цикле."""
    if not topics:
        return []
    start_ids = [t['start_id'] for t in topics]
    msg_by_id = {m.id: m for m in (
        db.query(Messages)
        .filter(Messages.id.in_(start_ids),
                Messages.handle_id.in_(handle_ids))
        .all())}
    out = []
    for t in topics:
        msg = msg_by_id.get(t['start_id'])
        out.append({
            'title': t['title'],
            'start_id': t['start_id'],
            'message_ids': t.get('message_ids') or [t['start_id']],
            'time': msg.time if msg else '',
            'date': (msg.created_at.strftime('%d.%m.%Y')
                     if msg and msg.created_at else ''),
        })
    return out


def _attach_edits(db, msgs):
    """Проставляет каждому сообщению `edit_history` — список прошлых версий
    текста, от самой старой к самой свежей. Финальная (текущая) версия
    лежит в `Messages.text` и в этот список НЕ входит."""
    from data.edits import MessageEdit
    ids = [m.id for m in msgs]
    history = {}
    if ids:
        for e in (db.query(MessageEdit)
                  .filter(MessageEdit.message_id.in_(ids))
                  .order_by(MessageEdit.message_id.asc(),
                            MessageEdit.edited_at.asc(),
                            MessageEdit.id.asc()).all()):
            history.setdefault(e.message_id, []).append({
                'text': e.text or '',
                'edited_at': e.edited_at.strftime('%H:%M')
                              if e.edited_at else '',
            })
    for m in msgs:
        m.edit_history = history.get(m.id, [])
    return msgs


def _attach_forwards(db, msgs, user_id):
    """Проставляет каждому сообщению `fwd_quote` для шапки пересылки."""
    from data.contacts import MessengerHandle

    chat_ids = {m.fwd_from_tg_chat_id for m in msgs
                if getattr(m, 'fwd_from_tg_chat_id', None) is not None}
    cid_by_chat = {}
    if chat_ids:
        for h in (db.query(MessengerHandle)
                  .filter(MessengerHandle.user_id == user_id,
                          MessengerHandle.messenger_name == 'Telegram',
                          MessengerHandle.tg_chat_id.in_(chat_ids)).all()):
            # Один tg_chat_id может попасться только на одном handle у юзера
            # (это уникальная личность). На случай дублей берём первый.
            cid_by_chat.setdefault(h.tg_chat_id, h.contact_id)
    for m in msgs:
        name = getattr(m, 'fwd_from_name', None)
        if not name:
            m.fwd_quote = None
            continue
        chat_id = getattr(m, 'fwd_from_tg_chat_id', None)
        synapse_user_id = getattr(m, 'fwd_from_synapse_user_id', None)
        messenger = getattr(m, 'fwd_from_messenger', None)
        if not messenger and chat_id is not None:
            messenger = 'Telegram'
        contact_id = cid_by_chat.get(chat_id)
        url = None
        if synapse_user_id:
            url = f'/messenger/{int(synapse_user_id)}'
        elif chat_id is not None:
            url = (f'/contacts/{int(contact_id)}?m=Telegram'
                   if contact_id else f'/contacts/from-tg/{int(chat_id)}')
        m.fwd_quote = {
            'name': name,
            'messenger': messenger,
            'contact_id': contact_id,
            'url': url,
        }
        if hasattr(m, 'visible_text'):
            stripped = _strip_forward_prefix(
                m.visible_text, _existing_forward_meta(m))
            if stripped != m.visible_text:
                m.visible_text = stripped
                m.visible_text_html = None
    return msgs


def _attach_replies(db, msgs, contact):
    """Проставляет каждому сообщению `reply_quote` — короткую цитату того
    сообщения, на которое это — ответ (reply), либо None."""
    from data.matching import display_author
    ids = {m.reply_to_message_id for m in msgs
           if getattr(m, 'reply_to_message_id', None)}
    targets = {}
    if ids:
        for t in db.query(Messages).filter(Messages.id.in_(ids)).all():
            targets[t.id] = t
    for m in msgs:
        rid = getattr(m, 'reply_to_message_id', None)
        target = targets.get(rid) if rid else None
        if target is None:
            m.reply_quote = None
            continue
        text = target.text or ''
        if len(text) > 120:
            text = text[:120] + '…'
        m.reply_quote = {
            'id': target.id,
            'author': ('Вы' if target.outgoing
                       else display_author(target.sender, contact.display_name)),
            'text': text,
        }
    return msgs


_last_delivery_recovery_at = 0.0


def _recover_interrupted_media_deliveries(force=False):
    """После перезапуска in-memory Telethon-задач уже нет.

    Не трогаем свежие задачи другого WSGI worker. Очередь ограничена восемью
    задачами и каждая попытка имеет timeout, поэтому 15 минут — безопасный
    порог: более старая запись уже потеряла исполняющую coroutine.
    """
    global _last_delivery_recovery_at
    now_mono = time.monotonic()
    if not force and now_mono - _last_delivery_recovery_at < 60:
        return 0
    _last_delivery_recovery_at = now_mono
    db = db_sessions.create_session()
    try:
        cutoff = datetime.now() - timedelta(minutes=15)
        candidates = db.query(Messages).filter(
            Messages.delivery_status == 'sending',
            Messages.tg_message_id.is_(None)).all()
        rows = [msg for msg in candidates
                if (msg.delivery_started_at or msg.created_at) is not None
                and (msg.delivery_started_at or msg.created_at) < cutoff]
        if not rows:
            return 0
        for msg in rows:
            msg.delivery_status = 'failed'
            msg.delivery_error = (
                'Отправка прервалась из-за перезапуска сервера. '
                'Нажмите «Повторить».')
            msg.delivery_started_at = None
        db.commit()
        return len(rows)
    finally:
        db.close()


def create_app(db_path: str = "db/blogs.db") -> Flask:
    _configure_timezone()
    db_sessions.global_init(db_path)
    _recover_interrupted_media_deliveries(force=True)

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=(os.environ.get('SKILLWOOD_SECRET_KEY')
                    or 'yandexlyceum_secret_key'),
        MAX_CONTENT_LENGTH=25 * 1024 * 1024,
        # Обычная Flask-сессия живёт лишь до закрытия браузера. Постоянная
        # сессия позволяет телефону/PWA помнить вход между запусками, а при
        # регулярном использовании срок продлевается автоматически.
        PERMANENT_SESSION_LIFETIME=timedelta(days=180),
        SESSION_REFRESH_EACH_REQUEST=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
    )

    @app.teardown_appcontext
    def _close_db(_exc):
        sess = g.pop('db', None)
        if sess is not None:
            sess.close()

    register_routes(app)

    from data import telegram_bridge

    @app.errorhandler(telegram_bridge.MediaStoreFullError)
    def _media_store_full(error):
        detail = (str(error) + '. Освободите место в медиа и повторите '
                  'отправку.')
        wants_json = (request.path.startswith('/api/')
                      or request.path == '/add_media'
                      or request.headers.get('X-Requested-With')
                      == 'XMLHttpRequest')
        if wants_json:
            return jsonify({'error': 'storage_full',
                            'detail': detail}), 507
        return detail, 507

    telegram_bridge.start()
    return app


def get_db():
    if 'db' not in g:
        g.db = db_sessions.create_session()
    return g.db


def _media_root() -> str:
    return os.environ.get('SKILLWOOD_MEDIA_ROOT') or os.path.join(os.getcwd(), 'media')


def _write_encrypted_media_path(full_path: str, data: bytes) -> None:
    """Единая квотированная и атомарная запись media-файлов."""
    from data import telegram_bridge
    from data.crypto import encrypt_bytes

    encrypted = encrypt_bytes(data)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    try:
        replacing_size = (os.path.getsize(full_path)
                          if os.path.exists(full_path) else 0)
    except OSError:
        replacing_size = 0
    reservation = telegram_bridge.reserve_media_write(
        len(encrypted), replacing_size=replacing_size)
    temp_path = full_path + '.tmp-' + uuid.uuid4().hex
    committed = False
    try:
        with open(temp_path, 'wb') as f:
            f.write(encrypted)
        os.replace(temp_path, full_path)
        committed = True
    finally:
        if not committed:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        telegram_bridge.finish_media_write(reservation, committed)


def _store_media_bytes(owner_id: int, data: bytes, subdir: str | None = None):
    rel_dir = str(owner_id)
    if subdir:
        rel_dir = f"{rel_dir}/{subdir.strip('/')}"
    stored_path = f"{rel_dir}/{uuid.uuid4().hex}.enc"
    _write_encrypted_media_path(
        os.path.join(_media_root(), stored_path), data)
    return stored_path


def _read_media_bytes(stored_path: str) -> bytes | None:
    from data.crypto import decrypt_bytes
    from cryptography.fernet import InvalidToken

    if not stored_path:
        return None
    try:
        full = _safe_media_full_path(stored_path)
    except ValueError:
        return None
    try:
        with open(full, 'rb') as f:
            return decrypt_bytes(f.read())
    except (OSError, ValueError, InvalidToken):
        return None


def _telegram_media_restore_handle(db, user_id, message, kind):
    """Проверяет, что файл действительно можно повторно запросить у TG."""
    from data.contacts import MessengerHandle

    if message is None or message.user_id != user_id:
        return None
    if not _telegram_media_is_restorable(message, kind):
        return None
    handle = db.get(MessengerHandle, message.handle_id)
    if (handle is None or handle.user_id != user_id
            or handle.messenger_name != 'Telegram'
            or handle.tg_chat_id is None):
        return None
    return handle


def _restore_telegram_attachment(db, user_id, message, handle, attachment):
    """Возвращает Telegram-файл в тот же cache path и Attachment id."""
    from data import telegram_bridge
    from data.attachments import Attachment

    if _media_rel_path_exists(attachment.stored_path):
        return attachment

    message_id = int(message.id)
    attachment_id = int(attachment.id)
    tg_message_id = int(message.tg_message_id)
    tg_chat_id = int(handle.tg_chat_id)
    stored_path = attachment.stored_path
    original_kind = attachment.kind
    # Не держим SQLite read-транзакцию во время MTProto-запроса, который
    # может длиться до 90 секунд. После сети всё перечитаем заново.
    db.rollback()
    result = telegram_bridge.download_message_media(
        tg_chat_id, tg_message_id, user_id=user_id)
    if not isinstance(result, dict):
        raise telegram_bridge.TelegramMediaUnavailableError(
            'Telegram не вернул медиафайл')
    data = result.get('data')
    kind = result.get('kind') or original_kind
    if (kind not in _TELEGRAM_RESTORABLE_MEDIA_KINDS
            or not isinstance(data, (bytes, bytearray)) or not data):
        raise telegram_bridge.TelegramMediaUnavailableError(
            'Telegram больше не отдаёт этот медиафайл')

    # Сообщение могли удалить, пока Telegram отдавал байты. Перечитываем
    # строки после новой транзакции и не создаём бесхозный encrypted-файл.
    message = (db.query(Messages)
               .filter(Messages.id == message_id,
                       Messages.user_id == user_id).first())
    attachment = (db.query(Attachment)
                  .filter(Attachment.id == attachment_id,
                          Attachment.user_id == user_id,
                          Attachment.message_id == message_id).first())
    handle = _telegram_media_restore_handle(db, user_id, message, kind)
    if (attachment is None or handle is None
            or attachment.stored_path != stored_path):
        raise telegram_bridge.TelegramMediaUnavailableError(
            'Сообщение было удалено во время загрузки')
    if _media_rel_path_exists(stored_path):
        return attachment

    full_path = _safe_media_full_path(stored_path)
    file_written = False
    try:
        _write_encrypted_media_path(full_path, bytes(data))
        file_written = True
        attachment.kind = kind
        attachment.mime = result.get('mime') or attachment.mime
        attachment.original_name = result.get('name') or attachment.original_name
        attachment.size = int(result.get('size') or len(data))
        db.commit()
    except Exception:
        db.rollback()
        if file_written:
            # Если delete успел закоммититься между повторной проверкой и
            # нашим commit, удаляем blob лишь когда на него больше нет ссылок.
            _remove_media_paths([stored_path])
        raise
    return attachment


def _run_media_restore_job(job, user_id, message_id, attachment_id):
    """Фоновая загрузка: долгий MTProto-запрос не держит WSGI worker."""
    from data import db_sessions, telegram_bridge
    from data.attachments import Attachment

    db = db_sessions.create_session()
    try:
        # Не держим striped lock во время сетевого запроса (до 90 секунд):
        # polling старого placeholder должен мгновенно получать 202.
        # Повторную job уже дедуплицирует `_media_restore_jobs`, а запись
        # файла атомарна через os.replace.
        message = (db.query(Messages)
                   .filter(Messages.id == message_id,
                           Messages.user_id == user_id).first())
        attachment = (db.query(Attachment)
                      .filter(Attachment.id == attachment_id,
                              Attachment.user_id == user_id,
                              Attachment.message_id == message_id).first())
        kind = attachment.kind if attachment is not None else None
        handle = _telegram_media_restore_handle(
            db, user_id, message, kind)
        if attachment is None or handle is None:
            raise telegram_bridge.TelegramMediaUnavailableError(
                'Медиа больше нельзя получить из Telegram.')
        attachment = _restore_telegram_attachment(
            db, user_id, message, handle, attachment)
        result = {
            'ok': True,
            'status': 'ready',
            'attachment': {
                'id': attachment.id,
                'kind': attachment.kind,
                'mime': attachment.mime,
                'name': attachment.original_name,
                'availability': 'local',
                'url': f'/attachments/{attachment.id}',
            },
        }
        with _media_restore_jobs_guard:
            job['status'] = 'ready'
            job['http_status'] = 200
            job['result'] = result
            # Готовность уже отражена самим файлом на диске. Не держим
            # terminal job: следующий POST увидит local и сразу вернёт 200.
            if _media_restore_jobs.get(job['key']) is job:
                _media_restore_jobs.pop(job['key'], None)
    except telegram_bridge.MediaStoreFullError as exc:
        db.rollback()
        with _media_restore_jobs_guard:
            job['status'] = 'error'
            job['http_status'] = 507
            job['result'] = {'error': 'storage_full', 'detail': str(exc)}
    except telegram_bridge.TelegramMediaUnavailableError as exc:
        db.rollback()
        with _media_restore_jobs_guard:
            job['status'] = 'error'
            job['http_status'] = 409
            job['result'] = {'error': 'media_unavailable', 'detail': str(exc)}
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception(
            'Telegram media restore failed for user_id=%s message_id=%s',
            user_id, message_id)
        with _media_restore_jobs_guard:
            job['status'] = 'error'
            job['http_status'] = 502
            job['result'] = {
                'error': 'telegram_unavailable',
                'detail': ('Не удалось загрузить медиа из Telegram. '
                           'Попробуйте ещё раз.'),
            }
    finally:
        db.close()


def _run_bounded_media_restore_job(job, user_id, message_id, attachment_id):
    with _media_restore_slots:
        _run_media_restore_job(job, user_id, message_id, attachment_id)


def _queue_media_restore(user_id, message_id, attachment_id):
    """Возвращает общую job для повторных кликов по одному сообщению."""
    key = (int(user_id), int(message_id))
    now = time.monotonic()
    with _media_restore_jobs_guard:
        for old_key, old_job in list(_media_restore_jobs.items()):
            if (old_job.get('status') != 'loading'
                    and now - old_job['started_at']
                    > _MEDIA_RESTORE_JOB_TTL_SECONDS):
                _media_restore_jobs.pop(old_key, None)
        job = _media_restore_jobs.get(key)
        if job is not None:
            return key, job
        loading_jobs = [
            value for value in _media_restore_jobs.values()
            if value.get('status') == 'loading'
        ]
        user_loading = sum(
            1 for value in loading_jobs
            if value.get('key', (None,))[0] == key[0]
        )
        if (len(loading_jobs) >= _MEDIA_RESTORE_MAX_PENDING
                or user_loading >= _MEDIA_RESTORE_MAX_PER_USER):
            return key, {
                'key': key,
                'status': 'error',
                'http_status': 429,
                'result': {
                    'error': 'restore_queue_full',
                    'detail': ('Сейчас загружается слишком много файлов. '
                               'Дождитесь завершения и повторите.'),
                },
                'started_at': now,
            }
        job = {
            'key': key,
            'status': 'loading',
            'http_status': 202,
            'result': {'ok': True, 'status': 'loading'},
            'started_at': now,
        }
        _media_restore_jobs[key] = job
    try:
        thread = threading.Thread(
            target=_run_bounded_media_restore_job,
            args=(job, int(user_id), int(message_id), int(attachment_id)),
            name=f'tg-media-restore-{message_id}', daemon=True)
        thread.start()
    except RuntimeError:
        with _media_restore_jobs_guard:
            if _media_restore_jobs.get(key) is job:
                _media_restore_jobs.pop(key, None)
            job['status'] = 'error'
            job['http_status'] = 503
            job['result'] = {
                'error': 'restore_unavailable',
                'detail': 'Загрузка временно недоступна. Повторите позже.',
            }
    return key, job


def _media_restore_job_response(key, job):
    """Снимок job; завершённый результат отдаётся один раз."""
    with _media_restore_jobs_guard:
        status = job.get('status')
        http_status = int(job.get('http_status') or 500)
        result = dict(job.get('result') or {})
        if status != 'loading' and _media_restore_jobs.get(key) is job:
            _media_restore_jobs.pop(key, None)
    return result, http_status


def _message_attachment_rows(db, message_id: int):
    from data.attachments import Attachment

    return (db.query(Attachment)
            .filter(Attachment.message_id == message_id)
            .order_by(Attachment.id.asc())
            .all())


def _purge_message_attachments(db, message_ids):
    """Удаляет только вложения сообщений и возвращает их media paths."""
    from data.attachments import Attachment
    from data.stickers import SavedSticker

    ids = [int(value) for value in message_ids]
    if not ids:
        return []
    attachments = (db.query(Attachment)
                   .filter(Attachment.message_id.in_(ids)).all())
    stored_paths = [row.stored_path for row in attachments
                    if row.stored_path]
    attachment_ids = [row.id for row in attachments]
    if attachment_ids:
        (db.query(SavedSticker)
         .filter(SavedSticker.source_attachment_id.in_(attachment_ids))
         .update({SavedSticker.source_attachment_id: None},
                 synchronize_session=False))
    for row in attachments:
        db.delete(row)
    return stored_paths


def _purge_message_dependencies(db, messages):
    """Удаляет строки, принадлежащие сообщениям; возвращает media paths.

    Файлы удаляются вызывающей стороной только после успешного commit, чтобы
    ошибка БД не оставила живую запись без вложения на диске.
    """
    from data.edits import MessageEdit
    from data.pending_replies import PendingReply
    from data.reactions import MessageReaction

    ids = [int(message.id) for message in messages if message is not None]
    if not ids:
        return []
    stored_paths = _purge_message_attachments(db, ids)
    (db.query(MessageReaction)
     .filter(MessageReaction.message_id.in_(ids))
     .delete(synchronize_session=False))
    (db.query(MessageEdit)
     .filter(MessageEdit.message_id.in_(ids))
     .delete(synchronize_session=False))
    (db.query(PendingReply)
     .filter(PendingReply.reply_to_message_id.in_(ids))
     .update({PendingReply.reply_to_message_id: None},
             synchronize_session=False))
    (db.query(Messages)
     .filter(Messages.reply_to_message_id.in_(ids))
     .update({Messages.reply_to_message_id: None},
             synchronize_session=False))
    for message in messages:
        db.delete(message)
    return stored_paths


def _remove_media_paths(stored_paths):
    from data.attachments import Attachment
    from data.direct import DirectAttachment
    from data.stickers import SavedSticker

    root = os.path.abspath(_media_root())
    db = db_sessions.create_session()
    try:
        for stored_path in set(stored_paths or ()):
            # Один encrypted blob может одновременно принадлежать зеркалу
            # Synapse, пересланной копии и сохранённому стикеру.
            still_used = (
                db.query(Attachment.id).filter(
                    Attachment.stored_path == stored_path).first()
                or db.query(DirectAttachment.id).filter(
                    DirectAttachment.stored_path == stored_path).first()
                or db.query(SavedSticker.id).filter(
                    SavedSticker.stored_path == stored_path).first()
            )
            if still_used:
                continue
            full = os.path.abspath(os.path.join(root, stored_path))
            try:
                if os.path.commonpath([root, full]) != root:
                    continue
                if os.path.isfile(full):
                    os.remove(full)
            except (OSError, ValueError):
                # БД уже очищена; оставшийся файл безопаснее убрать следующей
                # плановой чисткой, чем откатывать удаление сообщения в UI.
                continue
    finally:
        db.close()


def _client_send_key(raw_value: str | None) -> str | None:
    key = (raw_value or '').strip()
    if not key:
        return None
    return key[:160]


def _manual_send_duplicate_payload(db, msg, user_id: int) -> dict:
    _attach_media(db, [msg])
    _attach_forwards(db, [msg], user_id)
    return {
        'ok': True,
        'duplicate': True,
        'id': msg.id,
        'time': msg.time,
        'date_label': _message_date_label(msg.created_at),
        'text': getattr(msg, 'visible_text', msg.text or ''),
        'text_html': getattr(msg, 'visible_text_html', None),
        'messenger_name': msg.messenger_name,
        'client_send_key': getattr(msg, 'notification_dedup_key', None),
        'media': bool(getattr(msg, 'media', [])),
        'queued': getattr(msg, 'delivery_status', None) == 'sending',
        'scheduled': getattr(msg, 'delivery_schedule_at', None) is not None,
        'when': (msg.delivery_schedule_at.isoformat()
                 if getattr(msg, 'delivery_schedule_at', None) else None),
        'delivery_status': getattr(msg, 'delivery_status', None),
        'delivery_error': getattr(msg, 'delivery_error', None),
        'fwd_from': getattr(msg, 'fwd_quote', None),
        'attachments': [
            {'id': a.id, 'kind': a.kind, 'mime': a.mime,
             'name': a.original_name,
             'has_sticker_pack': bool(a.sticker_pack_key)}
            for a in getattr(msg, 'media', [])
        ],
    }


def _finish_telegram_media_delivery(message_id: int, sent_id,
                                    error) -> None:
    """Callback фоновой Telethon-отправки. Работает вне Flask request,
    поэтому использует отдельную SQLAlchemy-сессию."""
    db = db_sessions.create_session()
    stale_paths = []
    try:
        msg = db.query(Messages).filter(Messages.id == message_id).first()
        if msg is None:
            return
        if error is None:
            telegram_id = int(sent_id) if sent_id is not None else None
            # Если NewMessage-эхо успело сохраниться раньше callback,
            # оставляем исходную queued-запись и убираем только дубль.
            duplicate = None
            if telegram_id is not None:
                duplicate = (db.query(Messages)
                             .filter(Messages.user_id == msg.user_id,
                                     Messages.handle_id == msg.handle_id,
                                     Messages.tg_message_id == telegram_id,
                                     Messages.id != msg.id)
                             .order_by(Messages.id.asc()).first())
            if duplicate is not None:
                msg.tg_read_at = duplicate.tg_read_at
                msg.tg_topic_id = duplicate.tg_topic_id
                msg.tg_topic_title = duplicate.tg_topic_title
                stale_paths = _purge_message_dependencies(db, [duplicate])
            msg.tg_message_id = telegram_id
            msg.delivery_status = 'sent'
            msg.delivery_error = None
            msg.delivery_started_at = None
        else:
            msg.delivery_status = 'failed'
            msg.delivery_error = str(error)[:1000]
            msg.delivery_started_at = None
        db.commit()
        _remove_media_paths(stale_paths)
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()


def _finish_scheduled_media_delivery(message_id: int, sent_id,
                                     error) -> None:
    """После принятия schedule оставляет скрытую idempotency-запись.

    При ошибке сохраняем сообщение/файл со статусом failed для retry.
    При успехе файл больше не нужен локально, но строка Messages с
    client_send_key должна жить до реального NewMessage: повтор потерянного
    HTTP-ответа тогда не создаст вторую отложенную отправку.
    """
    db = db_sessions.create_session()
    stored_paths = []
    try:
        msg = db.query(Messages).filter(Messages.id == message_id).first()
        if msg is None:
            return
        if error is not None:
            msg.delivery_status = 'failed'
            msg.delivery_error = str(error)[:1000]
            msg.delivery_started_at = None
        else:
            msg.tg_message_id = int(sent_id) if sent_id is not None else None
            msg.delivery_status = 'scheduled'
            msg.delivery_error = None
            msg.delivery_started_at = None
            stored_paths = _purge_message_attachments(db, [msg.id])
        db.commit()
        _remove_media_paths(stored_paths)
    except Exception:  # noqa: BLE001
        db.rollback()
        raise
    finally:
        db.close()


def _attach_message_file(db, user_id: int, message_id: int, kind: str,
                         mime: str | None, original_name: str | None,
                         stored_path: str, size: int | None,
                         dedup_key: str | None = None,
                         sticker_pack_key: str | None = None,
                         sticker_pack_title: str | None = None,
                         sticker_item_key: str | None = None):
    from data.attachments import Attachment

    existing = (db.query(Attachment)
                .filter(Attachment.user_id == user_id,
                        Attachment.message_id == message_id,
                        Attachment.stored_path == stored_path)
                .first())
    if existing is not None:
        if sticker_pack_key and not existing.sticker_pack_key:
            existing.sticker_pack_key = sticker_pack_key
            existing.sticker_pack_title = sticker_pack_title
            existing.sticker_item_key = sticker_item_key
            db.flush()
        return existing
    att = Attachment(
        user_id=user_id,
        message_id=message_id,
        kind=kind,
        mime=mime or None,
        original_name=original_name or None,
        stored_path=stored_path,
        size=size,
        dedup_key=dedup_key,
        sticker_pack_key=sticker_pack_key,
        sticker_pack_title=sticker_pack_title,
        sticker_item_key=sticker_item_key,
    )
    db.add(att)
    db.flush()
    return att


_MAX_NOTIFICATION_AVATAR_BYTES = 512 * 1024


def _save_notification_avatar(user_id, encoded):
    raw_value = (encoded or '').strip()
    if not raw_value:
        return None
    if raw_value.startswith('data:') and ',' in raw_value:
        raw_value = raw_value.split(',', 1)[1]
    try:
        data = base64.b64decode(raw_value, validate=True)
    except Exception:  # noqa: BLE001
        return None
    if not data or len(data) > _MAX_NOTIFICATION_AVATAR_BYTES:
        return None
    if not (data.startswith(b'\x89PNG\r\n\x1a\n')
            or data.startswith(b'\xff\xd8\xff')):
        return None

    digest = hashlib.sha256(data).hexdigest()[:32]
    rel_dir = f"{user_id}/notification_avatars"
    rel_path = f"{rel_dir}/{digest}.enc"
    full_path = os.path.join(_media_root(), rel_path)
    if not os.path.exists(full_path):
        try:
            _write_encrypted_media_path(full_path, data)
        except OSError:
            # Аватар в Android payload опционален: нехватка места или сбой
            # его записи не должны потерять само текстовое MAX-сообщение.
            return None
    return rel_path


def _avatar_file(user_id) -> str:
    return os.path.join(_media_root(), str(user_id), 'avatar.enc')


def _avatar_mime_file(user_id) -> str:
    return os.path.join(_media_root(), str(user_id), 'avatar.mime')


def _read_avatar(user_id):
    from data.crypto import decrypt_bytes
    path = _avatar_file(user_id)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        raw = decrypt_bytes(f.read())
    mime = 'image/jpeg'
    mime_path = _avatar_mime_file(user_id)
    if os.path.exists(mime_path):
        with open(mime_path, 'r', encoding='utf-8') as f:
            mime = f.read().strip() or mime
    return raw, mime


def _user_avatar_for(user):
    label = (user.name or "?").strip()
    user.initial = label[:1].upper() if label else "?"
    user.avatar_color = _AVATAR_PALETTE[user.id % len(_AVATAR_PALETTE)]
    user.has_avatar = os.path.exists(_avatar_file(user.id))
    return user


def _user_can_manage_users(user) -> bool:
    if user is None:
        return False
    if int(getattr(user, 'id', 0) or 0) == 1:
        return True
    email = (getattr(user, 'email', None) or '').strip().lower()
    extra = {
        value.strip().lower()
        for value in os.environ.get('SKILLWOOD_USERS_ADMIN_EMAILS', '').split(',')
        if value.strip()
    }
    return email in USERS_ADMIN_EMAILS | extra


def _is_admin() -> bool:
    user_id = session.get('user_id')
    if not user_id:
        return False
    if int(user_id) == 1:
        session['can_manage_users'] = True
        return True
    cached = session.get('can_manage_users')
    if cached is not None:
        return bool(cached)
    user = get_db().query(User).filter(User.id == user_id).first()
    allowed = _user_can_manage_users(user)
    session['can_manage_users'] = allowed
    return allowed


def _fmt_dt(value) -> str:
    return value.strftime('%d.%m.%Y %H:%M') if value else '—'


def _iso_dt(value):
    return value.isoformat(timespec='seconds') if value else None


def _format_bytes(size) -> str:
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    units = ['Б', 'КБ', 'МБ', 'ГБ']
    value = float(max(size, 0))
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == 'Б' or value.is_integer():
        return f'{int(value)} {unit}'
    return f'{value:.1f}'.replace('.', ',') + f' {unit}'


def _dir_size(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return total
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _endpoint_host(endpoint: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(endpoint or '').netloc
    return host or 'push-сервис'


def _telegram_admin_status(user_id: int) -> dict:
    try:
        from data import telegram_bridge
        status = telegram_bridge.status(user_id)
    except Exception as exc:  # noqa: BLE001
        status = {'available': False, 'configured': False, 'authorized': False,
                  'needs_password': False, 'phone': None, 'error': str(exc)}

    configured = bool(status.get('configured'))
    authorized = bool(status.get('authorized'))
    if not configured:
        label = 'ключи API не настроены'
        badge = 'secondary'
    elif authorized:
        label = 'подключён'
        badge = 'success'
    elif status.get('needs_password'):
        label = 'нужен пароль 2FA'
        badge = 'warning'
    else:
        label = 'не подключён'
        badge = 'warning'

    return {
        'available': bool(status.get('available')),
        'configured': configured,
        'authorized': authorized,
        'needs_password': bool(status.get('needs_password')),
        'phone': status.get('phone'),
        'error': status.get('error'),
        'ghost_mode': bool(status.get('ghost_mode')),
        'skip_muted': bool(status.get('skip_muted')),
        'skip_archived': bool(status.get('skip_archived')),
        'label': label,
        'badge': badge,
    }


def _kick_telegram_recent_sync(user_id: int):
    """Неблокирующая догонка Telegram-сообщений для UI polling."""
    try:
        from data import telegram_bridge
        telegram_bridge.sync_recent(user_id)
    except Exception:
        pass


def _user_media_bytes(user_id: int) -> int:
    """Фактически занятое место в media/, без удалённого ленивого кэша.

    Поле Attachment.size описывает исходное вложение и остаётся в базе
    после вытеснения Telegram-кэша, поэтому суммировать его для дисковой
    квоты нельзя.
    """
    return _dir_size(os.path.join(_media_root(), str(user_id)))


def _admin_user_card_summary(db, user) -> dict:
    """Только данные компактной админской карточки пользователя."""
    from sqlalchemy import func
    from data.devices import Device
    from data.webpush_subscriptions import WebPushSubscription

    _user_avatar_for(user)
    device_total = (db.query(func.count(Device.id))
                    .filter(Device.user_id == user.id)
                    .scalar() or 0)
    push_total = (db.query(func.count(WebPushSubscription.id))
                  .filter(WebPushSubscription.user_id == user.id)
                  .scalar() or 0)
    push_active = (db.query(func.count(WebPushSubscription.id))
                   .filter(WebPushSubscription.user_id == user.id,
                           WebPushSubscription.enabled.is_(True))
                   .scalar() or 0)
    media_bytes = _user_media_bytes(user.id)

    return {
        'user': {
            'id': user.id,
            'display_name': (((user.name or '') + ' '
                              + (user.surname or '')).strip()
                             or user.username or user.email or f'user{user.id}'),
            'email': user.email or '—',
            'username': user.username or '',
            'preferred_lang': user.preferred_lang or 'ru',
            'about_seen': bool(getattr(user, 'about_seen_at', None)),
        },
        'avatar': {
            'has_avatar': bool(user.has_avatar),
            'initial': user.initial,
            'color': user.avatar_color,
        },
        'presence': _user_presence(user),
        'media': {
            'bytes': media_bytes,
            'label': _format_bytes(media_bytes),
        },
        'android': {
            'connected': bool(device_total),
            'total': int(device_total),
        },
        'telegram': _telegram_admin_status(user.id),
        'webpush': {
            'connected': bool(push_active),
            'active': int(push_active),
            'total': int(push_total),
        },
    }


def _admin_user_summary(db, user) -> dict:
    from sqlalchemy import func, or_
    from data.contacts import Contact, MessengerHandle
    from data.devices import Device
    from data.direct import DirectMessage
    from data.webpush_subscriptions import WebPushSubscription

    _user_avatar_for(user)

    contacts = (db.query(Contact)
                .filter(Contact.user_id == user.id)
                .order_by(Contact.display_name.asc())
                .all())
    handles = (db.query(MessengerHandle)
               .filter(MessengerHandle.user_id == user.id)
               .all())

    msg_rows = (db.query(Messages.messenger_name, func.count(Messages.id))
                .filter(Messages.user_id == user.id)
                .group_by(Messages.messenger_name)
                .all())
    direct_rows = (db.query(DirectMessage.sender_id, DirectMessage.recipient_id)
                   .filter(or_(DirectMessage.sender_id == user.id,
                               DirectMessage.recipient_id == user.id))
                   .all())
    direct_partner_ids = {
        row.recipient_id if row.sender_id == user.id else row.sender_id
        for row in direct_rows
    }
    direct_messages = len(direct_rows)

    messenger_stats = {}
    for handle in handles:
        name = handle.messenger_name or 'Неизвестно'
        item = messenger_stats.setdefault(
            name, {'handles': 0, 'messages': 0, 'groups': 0})
        item['handles'] += 1
        if bool(handle.is_group) or handle.tg_chat_type in ('group', 'channel'):
            item['groups'] += 1
    for name, count in msg_rows:
        item = messenger_stats.setdefault(
            name or 'Неизвестно', {'handles': 0, 'messages': 0, 'groups': 0})
        item['messages'] = int(count or 0)
    if direct_messages or direct_partner_ids:
        synapse = messenger_stats.setdefault(
            SYNAPSE_MESSENGER, {'handles': 0, 'messages': 0, 'groups': 0})
        synapse['handles'] = max(synapse['handles'], len(direct_partner_ids))
        synapse['messages'] = max(synapse['messages'], direct_messages)

    msg_total = sum(row['messages'] for row in messenger_stats.values())

    devices = (db.query(Device)
               .filter(Device.user_id == user.id)
               .order_by(Device.last_seen_at.desc().nullslast(),
                         Device.created_at.desc())
               .all())
    device_items = [
        {
            'id': d.id,
            'name': d.name,
            'created_at': _fmt_dt(d.created_at),
            'created_at_iso': _iso_dt(d.created_at),
            'last_seen_at': _fmt_dt(d.last_seen_at),
            'last_seen_at_iso': _iso_dt(d.last_seen_at),
            'last_seen_ip': d.last_seen_ip or '—',
        }
        for d in devices
    ]

    push_subs = (db.query(WebPushSubscription)
                 .filter(WebPushSubscription.user_id == user.id)
                 .order_by(WebPushSubscription.updated_at.desc().nullslast(),
                           WebPushSubscription.id.desc())
                 .all())
    push_items = [
        {
            'id': sub.id,
            'enabled': bool(sub.enabled),
            'endpoint': _endpoint_host(sub.endpoint),
            'origin': sub.origin or '—',
            'user_agent': sub.user_agent or '—',
            'created_at': _fmt_dt(sub.created_at),
            'created_at_iso': _iso_dt(sub.created_at),
            'updated_at': _fmt_dt(sub.updated_at),
            'updated_at_iso': _iso_dt(sub.updated_at),
            'failed_at': _fmt_dt(sub.failed_at),
            'failed_at_iso': _iso_dt(sub.failed_at),
            'last_error': sub.last_error,
        }
        for sub in push_subs
    ]
    last_push_error = next(
        (sub.last_error for sub in push_subs if sub.last_error), None)

    media_bytes = _user_media_bytes(user.id)
    counts = {
        'contacts': len(contacts),
        'messages': int(msg_total),
        'archived': sum(1 for c in contacts if bool(c.archived)),
        'muted': sum(1 for c in contacts if bool(c.muted)),
        'pinned_chats': sum(1 for c in contacts if bool(c.pinned_at)),
        'blocked': sum(1 for c in contacts if bool(c.blocked_at)),
        'pinned_messages': (db.query(func.count(Messages.id))
                            .filter(Messages.user_id == user.id,
                                    Messages.pinned_at.isnot(None))
                            .scalar() or 0),
    }

    return {
        'user': {
            'id': user.id,
            'name': user.name or '',
            'surname': user.surname or '',
            'display_name': (((user.name or '') + ' '
                              + (user.surname or '')).strip()
                             or user.username or user.email or f'user{user.id}'),
            'email': user.email or '—',
            'username': user.username or '',
            'preferred_lang': user.preferred_lang or 'ru',
            'created_at': _fmt_dt(getattr(user, 'created_at', None)),
            'created_at_iso': _iso_dt(getattr(user, 'created_at', None)),
            'modified_date': _fmt_dt(user.modified_date),
            'modified_date_iso': _iso_dt(user.modified_date),
            'about_seen': bool(getattr(user, 'about_seen_at', None)),
            'about_seen_at': _fmt_dt(getattr(user, 'about_seen_at', None)),
            'about_seen_at_iso': _iso_dt(
                getattr(user, 'about_seen_at', None)),
            'connect_code': user.connect_code or '—',
        },
        'avatar': {
            'has_avatar': bool(user.has_avatar),
            'initial': user.initial,
            'color': user.avatar_color,
        },
        'telegram': _telegram_admin_status(user.id),
        'presence': _user_presence(user),
        'devices': {
            'total': len(devices),
            'connected': bool(devices),
            'last_seen_at': device_items[0]['last_seen_at'] if device_items else '—',
            'last_seen_at_iso': (device_items[0]['last_seen_at_iso']
                                 if device_items else None),
            'last_seen_ip': device_items[0]['last_seen_ip'] if device_items else '—',
            'items': device_items,
        },
        'webpush': {
            'total': len(push_subs),
            'active': sum(1 for sub in push_subs if bool(sub.enabled)),
            'last_error': last_push_error,
            'items': push_items,
        },
        'counts': counts,
        'messengers': dict(sorted(messenger_stats.items())),
        'media': {
            'bytes': media_bytes,
            'label': _format_bytes(media_bytes),
        },
    }


# --- Внутренний мессенджер ----------------------------------------------

_USERNAME_RE = re.compile(r'^[a-z0-9_]{3,32}$')

_DM_PLACEHOLDER = {'image': '📷 Фото', 'video': '🎬 Видео',
                   'audio': '🎵 Аудио', 'voice': '🎤 Голосовое сообщение',
                   'sticker': '🩷 Стикер', 'file': '📎 Файл'}


def _normalize_username(raw: str) -> str:
    """Каноничный вид User ID: без пробелов, без ведущего «@», в нижнем
    регистре. Цифры не обязательны — годится и чисто буквенный логин."""
    return (raw or '').strip().lstrip('@').strip().lower()


def _validate_username(username: str):
    """Возвращает текст ошибки или None, если User ID допустим.

    Допустимый User ID — 3–32 символа из латиницы, цифр и «_».
    Цифры не обязательны.
    """
    if not username:
        return "Укажите User ID"
    if not _USERNAME_RE.match(username):
        return ("User ID: от 3 до 32 символов, только латинские буквы, "
                "цифры и подчёркивание")
    return None


def _kind_from_mime(mime: str) -> str:
    mime = (mime or '').lower()
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('video/'):
        return 'video'
    if mime.startswith('audio/'):
        return 'audio'
    return 'file'


def _voice_input_suffix(filename: str | None, mime: str | None) -> str:
    ext = os.path.splitext(filename or '')[1].lower()
    if ext in ('.webm', '.ogg', '.oga', '.opus', '.wav', '.m4a', '.mp3',
               '.mp4'):
        return ext
    mime = (mime or '').lower()
    if 'ogg' in mime or 'opus' in mime:
        return '.ogg'
    if 'wav' in mime:
        return '.wav'
    if 'mpeg' in mime or 'mp3' in mime:
        return '.mp3'
    if 'mp4' in mime or 'm4a' in mime:
        return '.m4a'
    return '.webm'


def _ffmpeg_executable():
    system_ffmpeg = shutil.which('ffmpeg')
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001
        print(f'ffmpeg для голосовых недоступен: {exc}')
        return None


def _normalize_voice_upload(data: bytes, filename: str | None,
                            mime: str | None):
    """Привести голос из браузера к формату Telegram voice note."""
    fallback_name = filename or 'voice.webm'
    fallback_mime = (mime or 'audio/webm').lower()
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        return data, fallback_name, fallback_mime

    try:
        with tempfile.TemporaryDirectory(prefix='synapse_voice_') as tmp:
            in_path = os.path.join(
                tmp, 'input' + _voice_input_suffix(filename, mime))
            out_path = os.path.join(tmp, 'voice.ogg')
            with open(in_path, 'wb') as f:
                f.write(data)
            subprocess.run(
                [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                 '-i', in_path, '-vn', '-ac', '1', '-c:a', 'libopus',
                 '-b:a', '32k', '-application', 'voip', '-f', 'ogg',
                 out_path],
                timeout=30, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with open(out_path, 'rb') as f:
                converted = f.read()
        if converted.startswith(b'OggS'):
            return converted, 'voice.ogg', 'audio/ogg'
    except Exception as exc:  # noqa: BLE001
        print(f'Не удалось подготовить голосовое сообщение: {exc}')
    return data, fallback_name, fallback_mime


def _dm_user_card(user) -> dict:
    """Краткая карточка пользователя для UI внутреннего мессенджера."""
    full = ((user.name or '') + ' ' + (user.surname or '')).strip()
    label = full or (user.username or '?')
    has_avatar = os.path.exists(_avatar_file(user.id))
    is_creator = _is_creator_user(user)
    return {
        'id': user.id,
        'username': user.username or '',
        'display_name': label,
        'initial': label[:1].upper() if label else '?',
        'avatar_color': _AVATAR_PALETTE[user.id % len(_AVATAR_PALETTE)],
        'avatar_url': f'/messenger/avatar/{user.id}' if has_avatar else None,
        'is_creator': is_creator,
        'creator_title': CREATOR_BADGE if is_creator else '',
    }


def _creator_cards(db, me_id: int) -> list[dict]:
    from sqlalchemy import or_

    _seed_legacy_creator_flags(db)
    configured_ids = _configured_creator_ids()
    filters = [User.is_creator.is_(True)]
    if configured_ids:
        filters.append(User.id.in_(configured_ids))
    users = (db.query(User)
             .filter(or_(*filters))
             .order_by(User.id.asc())
             .all())
    cards = []
    for user in users:
        if not bool(getattr(user, 'is_creator', False)):
            user.is_creator = True
        card = _dm_user_card(user)
        card['is_self'] = user.id == me_id
        cards.append(card)
    db.flush()
    return cards


def _dm_attachments(db, msgs) -> dict:
    """{message_id: [DirectAttachment, ...]} для списка сообщений."""
    from data.direct import DirectAttachment
    ids = [m.id for m in msgs]
    by_msg = {}
    if ids:
        for a in (db.query(DirectAttachment)
                  .filter(DirectAttachment.message_id.in_(ids))
                  .order_by(DirectAttachment.id.asc()).all()):
            by_msg.setdefault(a.message_id, []).append(a)
    return by_msg


def _dm_message_dict(m, me_id, atts) -> dict:
    return {
        'id': m.id,
        'text': m.text or '',
        'time': m.created_at.strftime('%H:%M') if m.created_at else '',
        'outgoing': m.sender_id == me_id,
        'attachments': [{'id': a.id, 'kind': a.kind,
                         'mime': a.mime,
                         'name': a.original_name,
                         'has_sticker_pack': bool(a.sticker_pack_key)}
                        for a in atts.get(m.id, [])],
    }


def _synapse_sender_raw(user_id: int) -> str:
    return f"synapse:{user_id}"


def _synapse_partner_id(handle) -> int | None:
    raw = handle.sender_raw or ''
    if not raw.startswith('synapse:'):
        return None
    try:
        return int(raw.split(':', 1)[1])
    except (TypeError, ValueError):
        return None


def _presence_for_handles(db, owner_id: int, handles,
                          refresh_telegram: bool = False):
    """Присутствие открытого личного чата Synapse или Telegram."""
    for handle in handles:
        if handle.messenger_name != SYNAPSE_MESSENGER:
            continue
        partner_id = _synapse_partner_id(handle)
        if partner_id is None:
            continue
        partner = db.query(User).filter(User.id == partner_id).first()
        if partner is not None:
            return _user_presence(partner)

    from data import telegram_bridge
    for handle in handles:
        if (handle.messenger_name != 'Telegram'
                or handle.tg_chat_id is None
                or _is_group_or_channel_handle(handle)):
            continue
        return telegram_bridge.presence_status(
            handle.tg_chat_id, user_id=owner_id,
            refresh=refresh_telegram)
    return None


def _ensure_synapse_handle(db, owner_id: int, partner):
    from data.contacts import Contact, MessengerHandle

    sender_raw = _synapse_sender_raw(partner.id)
    handle = (db.query(MessengerHandle)
              .filter(MessengerHandle.user_id == owner_id,
                      MessengerHandle.messenger_name == SYNAPSE_MESSENGER,
                      MessengerHandle.sender_raw == sender_raw)
              .first())
    if handle is not None:
        return handle

    contact = Contact(user_id=owner_id,
                      display_name=_dm_user_card(partner)['display_name'])
    db.add(contact)
    db.flush()
    handle = MessengerHandle(
        contact_id=contact.id,
        user_id=owner_id,
        messenger_name=SYNAPSE_MESSENGER,
        sender_raw=sender_raw,
        sender_normalized=sender_raw,
    )
    db.add(handle)
    db.flush()
    return handle


def _mirror_direct_message_for_owner(db, direct_msg, owner_id: int, users_by_id: dict):
    partner_id = (direct_msg.recipient_id
                  if direct_msg.sender_id == owner_id else direct_msg.sender_id)
    partner = users_by_id.get(partner_id)
    if partner is None:
        return None
    handle = _ensure_synapse_handle(db, owner_id, partner)
    outgoing = direct_msg.sender_id == owner_id
    created_at = direct_msg.created_at or datetime.now()
    text = direct_msg.text or 'Вложение'
    existing = (db.query(Messages)
                .filter(Messages.user_id == owner_id,
                        Messages.handle_id == handle.id,
                        Messages.created_at == created_at,
                        Messages.outgoing.is_(outgoing))
                .first())
    if existing is not None:
        _mirror_direct_attachments_to_message(db, direct_msg, owner_id, existing)
        return existing
    msg = Messages(
        sender='Вы' if outgoing else _dm_user_card(partner)['display_name'],
        text=text,
        messenger_name=SYNAPSE_MESSENGER,
        time=created_at.strftime('%H:%M'),
        user_id=owner_id,
        handle_id=handle.id,
        created_at=created_at,
        outgoing=outgoing,
    )
    db.add(msg)
    db.flush()
    _mirror_direct_attachments_to_message(db, direct_msg, owner_id, msg)
    return msg


def _mirror_direct_attachments_to_message(db, direct_msg, owner_id: int,
                                          mirror_msg):
    from data.direct import DirectAttachment

    atts = (db.query(DirectAttachment)
            .filter(DirectAttachment.message_id == direct_msg.id)
            .order_by(DirectAttachment.id.asc())
            .all())
    for att in atts:
        _attach_message_file(
            db, owner_id, mirror_msg.id, att.kind, att.mime,
            att.original_name, att.stored_path, att.size,
            sticker_pack_key=att.sticker_pack_key,
            sticker_pack_title=att.sticker_pack_title,
            sticker_item_key=att.sticker_item_key)


def _add_direct_attachment_ref(db, direct_message_id: int, kind: str,
                               mime: str | None, original_name: str | None,
                               stored_path: str, size: int | None,
                               sticker_pack_key: str | None = None,
                               sticker_pack_title: str | None = None,
                               sticker_item_key: str | None = None):
    from data.direct import DirectAttachment

    att = DirectAttachment(
        message_id=direct_message_id,
        kind=kind,
        mime=mime or None,
        original_name=original_name or None,
        stored_path=stored_path,
        size=size,
        sticker_pack_key=sticker_pack_key,
        sticker_pack_title=sticker_pack_title,
        sticker_item_key=sticker_item_key,
    )
    db.add(att)
    db.flush()
    return att


def _save_direct_upload(db, owner_id: int, direct_message_id: int, upload,
                        voice_upload: bool = False):
    data = upload.read()
    if not data:
        return None, None
    upload_name = upload.filename or 'file'
    upload_mime = (upload.mimetype or '').lower()
    if voice_upload:
        data, upload_name, upload_mime = _normalize_voice_upload(
            data, upload_name or 'voice.webm', upload_mime)
        kind = 'voice'
    else:
        kind = _kind_from_mime(upload_mime)
    stored_path = _store_media_bytes(owner_id, data)
    att = _add_direct_attachment_ref(
        db, direct_message_id, kind, upload_mime, upload_name,
        stored_path, len(data))
    return att, data


def _copy_message_attachments_to_direct(db, source_msg, direct_message_id: int):
    copied = []
    for att in _message_attachment_rows(db, source_msg.id):
        if not _media_rel_path_exists(att.stored_path):
            continue
        copied.append(_add_direct_attachment_ref(
            db, direct_message_id, att.kind, att.mime, att.original_name,
            att.stored_path, att.size,
            sticker_pack_key=att.sticker_pack_key,
            sticker_pack_title=att.sticker_pack_title,
            sticker_item_key=att.sticker_item_key))
    return copied


def _message_tg_chat_id(db, msg):
    """Telegram chat_id чата, которому принадлежит локальное сообщение."""
    from data.contacts import MessengerHandle

    if msg.handle_id is None:
        return None
    handle = db.get(MessengerHandle, msg.handle_id)
    return handle.tg_chat_id if handle is not None else None


def _message_contact_label(db, msg) -> str | None:
    from data.contacts import Contact, MessengerHandle

    handle = db.get(MessengerHandle, msg.handle_id) if msg.handle_id else None
    if handle is None:
        return None
    contact = (db.get(Contact, handle.contact_id)
               if handle.contact_id else None)
    if contact is not None and contact.display_name:
        return contact.display_name
    return handle.sender_raw


def _telegram_forward_label(owner_id: int, chat_id, fallback: str) -> str:
    if chat_id is None:
        return fallback
    try:
        from data import telegram_bridge
        info = telegram_bridge.resolve_entity_info(int(chat_id),
                                                   user_id=owner_id)
    except Exception:  # noqa: BLE001
        return fallback
    username = (info.get('username') or '').strip()
    if username:
        return '@' + username.lstrip('@')
    title = (info.get('display_name') or info.get('title') or '').strip()
    return title or fallback


def _message_synapse_user_id(db, owner_id: int, msg):
    """User.id автора Synapse-сообщения глазами владельца owner_id."""
    if msg.messenger_name != SYNAPSE_MESSENGER:
        return None
    if msg.outgoing:
        return owner_id
    from data.contacts import MessengerHandle

    handle = db.get(MessengerHandle, msg.handle_id) if msg.handle_id else None
    if handle is None:
        return None
    return _synapse_partner_id(handle)


def _existing_forward_meta(msg):
    name = getattr(msg, 'fwd_from_name', None)
    if not name:
        return None
    messenger = getattr(msg, 'fwd_from_messenger', None)
    tg_chat_id = getattr(msg, 'fwd_from_tg_chat_id', None)
    synapse_user_id = getattr(msg, 'fwd_from_synapse_user_id', None)
    if not messenger and tg_chat_id is not None:
        messenger = 'Telegram'
    return {
        'name': name,
        'messenger': messenger or 'мессенджера',
        'tg_chat_id': tg_chat_id,
        'synapse_user_id': synapse_user_id,
    }


def _forward_source_meta(db, owner_id: int, msg) -> dict:
    """Структурный источник пересылки для кликабельной шапки в UI."""
    existing = _existing_forward_meta(msg)
    if existing is not None:
        return existing

    messenger = msg.messenger_name or 'мессенджера'
    if messenger not in ('Telegram', SYNAPSE_MESSENGER):
        return None
    meta = {
        'name': _forward_author_label(db, owner_id, msg),
        'messenger': messenger,
        'tg_chat_id': None,
        'synapse_user_id': None,
    }
    if messenger == 'Telegram':
        author_chat_id = getattr(msg, 'author_tg_chat_id', None)
        chat_id = (author_chat_id
                   if author_chat_id and not msg.outgoing
                   else _message_tg_chat_id(db, msg))
        meta['tg_chat_id'] = chat_id
        fallback = meta['name']
        if not (author_chat_id and not msg.outgoing):
            fallback = _message_contact_label(db, msg) or fallback
        meta['name'] = _telegram_forward_label(owner_id, chat_id, fallback)
    elif messenger == SYNAPSE_MESSENGER:
        meta['synapse_user_id'] = _message_synapse_user_id(
            db, owner_id, msg)
    return meta


def _apply_forward_meta(msg, meta: dict | None):
    if msg is None or not meta:
        return
    msg.fwd_from_name = meta.get('name')
    msg.fwd_from_messenger = meta.get('messenger')
    msg.fwd_from_tg_chat_id = meta.get('tg_chat_id')
    msg.fwd_from_synapse_user_id = meta.get('synapse_user_id')


def _strip_forward_prefix(text: str, meta: dict | None) -> str:
    if not text or not meta:
        return text or ''
    messenger = meta.get('messenger')
    name = meta.get('name')
    if not messenger or not name:
        return text
    prefix = f'Переслано из {messenger} "{name}"\n\n'
    if text.startswith(prefix):
        return text[len(prefix):]
    return text


def _forward_source_body(db, msg) -> str:
    body = (msg.text or '').strip()
    if body:
        return _strip_forward_prefix(body, _existing_forward_meta(msg)).strip()
    atts = _message_attachment_rows(db, msg.id)
    if atts:
        return _DM_PLACEHOLDER.get(atts[0].kind, '📎 Вложение')
    return 'Сообщение'


def _forward_author_label(db, owner_id: int, msg) -> str:
    from data.contacts import Contact, MessengerHandle
    from data.matching import display_author

    messenger = msg.messenger_name or ''
    if messenger == SYNAPSE_MESSENGER:
        source_user = None
        if msg.outgoing:
            source_user = db.get(User, owner_id)
        else:
            handle = db.get(MessengerHandle, msg.handle_id) if msg.handle_id else None
            partner_id = _synapse_partner_id(handle) if handle is not None else None
            if partner_id:
                source_user = db.get(User, partner_id)
        if source_user is not None:
            username = (source_user.username or '').strip()
            if username:
                return '@' + username
            return _dm_user_card(source_user)['display_name']

    handle = db.get(MessengerHandle, msg.handle_id) if msg.handle_id else None
    contact = (db.get(Contact, handle.contact_id)
               if handle is not None and handle.contact_id else None)
    if msg.outgoing:
        me = db.get(User, owner_id)
        if me is not None and (me.username or '').strip():
            return '@' + me.username.strip()
        return 'Вы'
    if contact is not None:
        return display_author(msg.sender or '', contact.display_name)
    return msg.sender or 'Сообщение'


def _forward_delivery_text(db, owner_id: int, msg) -> str:
    meta = _forward_source_meta(db, owner_id, msg) or {}
    messenger = meta.get('messenger') or msg.messenger_name or 'мессенджера'
    author = meta.get('name') or _forward_author_label(db, owner_id, msg)
    body = _forward_source_body(db, msg)
    return f'Переслано из {messenger} "{author}"\n\n{body}'


def _forward_text_for_target(db, owner_id: int, msg,
                             target_messenger: str) -> str:
    if target_messenger == SYNAPSE_MESSENGER and (
            msg.messenger_name in ('Telegram', SYNAPSE_MESSENGER)
            or _existing_forward_meta(msg) is not None):
        return _forward_source_body(db, msg)
    return _forward_delivery_text(db, owner_id, msg)


def _telegram_send_fallback_copy(db, user_id: int, tg_handle, source_msg,
                                 text: str, reply_kw_tg: dict,
                                 options: dict, local_reply_target=None,
                                 forward_meta: dict | None = None):
    from data import telegram_bridge

    sent_text_id = telegram_bridge.send_message(
        tg_handle.tg_chat_id, text, **reply_kw_tg, **options,
        user_id=user_id)
    now = datetime.now()
    local_msg = Messages(
        sender='Вы',
        text=text,
        messenger_name='Telegram',
        time=now.strftime('%H:%M'),
        user_id=user_id,
        handle_id=tg_handle.id,
        created_at=now,
        outgoing=True,
        tg_message_id=sent_text_id,
        reply_to_message_id=(
            local_reply_target.id if local_reply_target else None),
    )
    _apply_forward_meta(local_msg, forward_meta)
    db.add(local_msg)
    db.flush()

    sent_media = []
    for att in _message_attachment_rows(db, source_msg.id):
        raw = _read_media_bytes(att.stored_path)
        if raw is None:
            continue
        sent_id = telegram_bridge.send_file(
            tg_handle.tg_chat_id, raw, att.original_name or 'sticker.webp',
            '', **options, user_id=user_id,
            voice_note=(att.kind == 'voice'))
        media_msg = Messages(
            sender='Вы',
            text=_DM_PLACEHOLDER.get(att.kind, '📎 Файл'),
            messenger_name='Telegram',
            time=datetime.now().strftime('%H:%M'),
            user_id=user_id,
            handle_id=tg_handle.id,
            created_at=datetime.now(),
            outgoing=True,
            tg_message_id=sent_id,
        )
        _apply_forward_meta(media_msg, forward_meta)
        db.add(media_msg)
        db.flush()
        _attach_message_file(
            db, user_id, media_msg.id, att.kind, att.mime,
            att.original_name, att.stored_path, att.size,
            sticker_pack_key=att.sticker_pack_key,
            sticker_pack_title=att.sticker_pack_title,
            sticker_item_key=att.sticker_item_key)
        sent_media.append(sent_id)
    return local_msg, sent_media


def _saved_sticker_dict(sticker) -> dict:
    return {
        'id': sticker.id,
        'kind': sticker.kind,
        'mime': sticker.mime,
        'name': sticker.original_name,
        'url': f'/stickers/{sticker.id}',
        'pack_key': sticker.pack_key,
        'pack_title': sticker.pack_title,
        'created_at': (sticker.created_at.isoformat()
                       if sticker.created_at else None),
    }


def _safe_sticker_ext(mime: str | None) -> str:
    mime = (mime or '').lower()
    return {
        'image/webp': '.webp',
        'image/png': '.png',
        'image/jpeg': '.jpg',
        'image/gif': '.gif',
        'video/webm': '.webm',
        'application/x-tgsticker': '.tgs',
    }.get(mime, '.bin')


def _decode_sticker_data_url(data_url: str):
    raw = (data_url or '').strip()
    if not raw.startswith('data:') or ';base64,' not in raw:
        return None, None
    head, encoded = raw.split(',', 1)
    mime = head[5:].split(';', 1)[0] or 'application/octet-stream'
    try:
        return mime, base64.b64decode(encoded)
    except Exception:  # noqa: BLE001
        return None, None


def _save_sticker_bytes(db, user_id: int, data: bytes,
                        mime: str | None, name: str | None,
                        pack_key: str | None = None,
                        pack_title: str | None = None,
                        item_key: str | None = None,
                        source_attachment_id: int | None = None):
    from data.stickers import SavedSticker

    if not data:
        return None
    if not item_key:
        item_key = hashlib.sha256(data).hexdigest()
    if pack_key:
        existing = (db.query(SavedSticker)
                    .filter(SavedSticker.user_id == user_id,
                            SavedSticker.pack_key == pack_key,
                            SavedSticker.item_key == item_key)
                    .first())
        if existing is not None:
            return existing
    stored_path = _store_media_bytes(user_id, data, subdir='stickers')
    sticker = SavedSticker(
        user_id=user_id,
        source_attachment_id=source_attachment_id,
        kind='sticker',
        mime=mime,
        original_name=name or ('sticker' + _safe_sticker_ext(mime)),
        stored_path=stored_path,
        size=len(data),
        pack_key=pack_key,
        pack_title=pack_title,
        item_key=item_key,
    )
    db.add(sticker)
    db.flush()
    return sticker


def _save_sticker_pack_payload(db, user_id: int, payload: dict,
                               source_attachment=None):
    stickers = []
    items = payload.get('stickers') or []
    title = payload.get('title') or 'Стикерпак'
    pack_key = payload.get('pack_key')
    if not pack_key:
        if source_attachment is not None:
            pack_key = f'attachment-pack:{source_attachment.id}'
        else:
            pack_key = 'local-pack:' + hashlib.sha256(
                (title + ':' + str(len(items))).encode('utf-8')
            ).hexdigest()[:24]
    for idx, item in enumerate(items, 1):
        mime, data = _decode_sticker_data_url(item.get('data_url') or '')
        if not data:
            continue
        mime = item.get('mime') or mime
        item_key = str(item.get('item_key') or item.get('id')
                       or hashlib.sha256(data).hexdigest())
        name = (item.get('name') or item.get('file_name')
                or f'sticker-{idx}{_safe_sticker_ext(mime)}')
        sticker = _save_sticker_bytes(
            db, user_id, data, mime, name,
            pack_key=pack_key, pack_title=title, item_key=item_key,
            source_attachment_id=(source_attachment.id
                                  if source_attachment is not None else None))
        if sticker is not None:
            stickers.append(sticker)
    return stickers, pack_key, title


def _copy_sticker_pack_to_user(db, user_id: int, pack_key: str,
                               source_attachment=None):
    from data.stickers import SavedSticker

    source = (db.query(SavedSticker)
              .filter(SavedSticker.pack_key == pack_key)
              .order_by(SavedSticker.id.asc())
              .all())
    copied = []
    for s in source:
        existing = (db.query(SavedSticker)
                    .filter(SavedSticker.user_id == user_id,
                            SavedSticker.pack_key == s.pack_key,
                            SavedSticker.item_key == s.item_key)
                    .first())
        if existing is not None:
            copied.append(existing)
            continue
        clone = SavedSticker(
            user_id=user_id,
            source_attachment_id=(source_attachment.id
                                  if source_attachment is not None else None),
            kind='sticker',
            mime=s.mime,
            original_name=s.original_name,
            stored_path=s.stored_path,
            size=s.size,
            pack_key=s.pack_key,
            pack_title=s.pack_title,
            item_key=s.item_key,
        )
        db.add(clone)
        db.flush()
        copied.append(clone)
    return copied


def _local_sticker_pack_payload(db, user_id: int, pack_key: str):
    from data.stickers import SavedSticker

    rows = (db.query(SavedSticker)
            .filter(SavedSticker.user_id == user_id,
                    SavedSticker.pack_key == pack_key)
            .order_by(SavedSticker.id.asc())
            .all())
    if not rows:
        rows = (db.query(SavedSticker)
                .filter(SavedSticker.pack_key == pack_key)
                .order_by(SavedSticker.id.asc())
                .all())
    items = []
    for sticker in rows:
        raw = _read_media_bytes(sticker.stored_path)
        if not raw:
            continue
        mime = sticker.mime or 'application/octet-stream'
        items.append({
            'id': sticker.item_key or sticker.id,
            'mime': mime,
            'alt': '',
            'name': sticker.original_name,
            'data_url': 'data:{};base64,{}'.format(
                mime, base64.b64encode(raw).decode('ascii')),
        })
    title = rows[0].pack_title if rows else 'Стикерпак'
    return {'ok': True, 'title': title or 'Стикерпак',
            'pack_key': pack_key, 'count': len(items), 'stickers': items}


def _save_sticker_from_attachment(db, user_id: int, attachment):
    from data.stickers import SavedSticker

    existing = (db.query(SavedSticker)
                .filter(SavedSticker.user_id == user_id,
                        SavedSticker.stored_path == attachment.stored_path)
                .first())
    if existing is not None:
        if attachment.sticker_pack_key and not existing.pack_key:
            existing.pack_key = attachment.sticker_pack_key
            existing.pack_title = attachment.sticker_pack_title
            existing.item_key = attachment.sticker_item_key
            db.flush()
        return existing
    sticker = SavedSticker(
        user_id=user_id,
        source_attachment_id=attachment.id,
        kind='sticker',
        mime=attachment.mime,
        original_name=attachment.original_name,
        stored_path=attachment.stored_path,
        size=attachment.size,
        pack_key=attachment.sticker_pack_key,
        pack_title=attachment.sticker_pack_title,
        item_key=attachment.sticker_item_key,
    )
    db.add(sticker)
    db.flush()
    return sticker


def _sync_direct_messages_to_contacts(db, owner_id: int):
    from sqlalchemy import or_

    from data.direct import DirectMessage

    direct_msgs = (db.query(DirectMessage)
                   .filter(or_(DirectMessage.sender_id == owner_id,
                               DirectMessage.recipient_id == owner_id))
                   .order_by(DirectMessage.created_at.asc().nullsfirst(),
                             DirectMessage.id.asc())
                   .all())
    if not direct_msgs:
        return
    user_ids = {
        pid
        for m in direct_msgs
        for pid in (m.sender_id, m.recipient_id)
    }
    users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(user_ids)).all()}
    for direct_msg in direct_msgs:
        _mirror_direct_message_for_owner(db, direct_msg, owner_id, users_by_id)
    db.commit()


def _mark_synapse_contact_read(db, owner_id: int, handles):
    from data.direct import DirectMessage

    partner_ids = [_synapse_partner_id(h) for h in handles
                   if h.messenger_name == SYNAPSE_MESSENGER]
    partner_ids = [pid for pid in partner_ids if pid is not None]
    if not partner_ids:
        return
    now = datetime.now()
    changed = False
    incoming = (db.query(DirectMessage)
                .filter(DirectMessage.recipient_id == owner_id,
                        DirectMessage.sender_id.in_(partner_ids),
                        DirectMessage.read_at.is_(None))
                .all())
    for msg in incoming:
        msg.read_at = now
        changed = True
    if changed:
        db.flush()


def _dm_conversations(db, me_id) -> list:
    """Список переписок пользователя: по карточке на каждого собеседника,
    отсортирован по времени последнего сообщения (свежие сверху)."""
    from sqlalchemy import or_

    from data.direct import DirectMessage
    msgs = (db.query(DirectMessage)
            .filter(or_(DirectMessage.sender_id == me_id,
                        DirectMessage.recipient_id == me_id))
            .order_by(DirectMessage.created_at.asc().nullsfirst(),
                      DirectMessage.id.asc())
            .all())
    by_partner = {}
    for m in msgs:
        pid = m.recipient_id if m.sender_id == me_id else m.sender_id
        by_partner.setdefault(pid, []).append(m)
    if not by_partner:
        return []
    users = {u.id: u for u in db.query(User)
             .filter(User.id.in_(list(by_partner.keys()))).all()}
    convs = []
    for pid, plist in by_partner.items():
        user = users.get(pid)
        if user is None:
            continue
        last = plist[-1]
        unread = sum(1 for m in plist
                     if m.recipient_id == me_id and m.read_at is None)
        card = _dm_user_card(user)
        card['last_preview'] = last.text or '📎 Вложение'
        card['last_time'] = (last.created_at.strftime('%H:%M')
                             if last.created_at else '')
        card['unread_count'] = unread
        card['_sort'] = last.created_at or datetime.min
        convs.append(card)
    convs.sort(key=lambda c: c['_sort'], reverse=True)
    for c in convs:
        c.pop('_sort', None)
    return convs


def _dm_load_conversation(db, me_id, partner, mark_read=False):
    """Сообщения переписки между me_id и partner. При mark_read помечает
    входящие непрочитанные как прочитанные."""
    from sqlalchemy import and_, or_

    from data.direct import DirectMessage
    msgs = (db.query(DirectMessage)
            .filter(or_(
                and_(DirectMessage.sender_id == me_id,
                     DirectMessage.recipient_id == partner.id),
                and_(DirectMessage.sender_id == partner.id,
                     DirectMessage.recipient_id == me_id)))
            .order_by(DirectMessage.created_at.asc().nullsfirst(),
                      DirectMessage.id.asc())
            .all())
    if mark_read:
        changed = False
        for m in msgs:
            if m.recipient_id == me_id and m.read_at is None:
                m.read_at = datetime.now()
                changed = True
        if changed:
            db.commit()
    atts = _dm_attachments(db, msgs)
    return [_dm_message_dict(m, me_id, atts) for m in msgs]


def register_routes(app: Flask) -> None:
    presence_write_guard = threading.Lock()
    presence_written_at = {}

    @app.before_request
    def _record_web_presence():
        """Пишем активность не чаще раза в 20 секунд на пользователя."""
        user_id = session.get('user_id')
        if not user_id or request.endpoint == 'static':
            return None
        # Одновременно обновляем старые непостоянные сессии, созданные до
        # включения «запомнить вход». Достаточно одного запроса пользователя.
        if not session.permanent:
            session.permanent = True
        if session.get('can_manage_users') is None:
            user = get_db().query(User).filter(User.id == user_id).first()
            session['can_manage_users'] = _user_can_manage_users(user)
        now_mono = time.monotonic()
        with presence_write_guard:
            previous = presence_written_at.get(user_id)
            if (previous is not None
                    and now_mono - previous < _PRESENCE_WRITE_INTERVAL_SECONDS):
                return None
            # Бронируем интервал до обращения к SQLite. Параллельные запросы
            # одного пользователя больше не создают очередь из UPDATE-lock.
            presence_written_at[user_id] = now_mono
        now = datetime.now()
        db = get_db()
        try:
            changed = (db.query(User)
                       .filter(User.id == user_id)
                       .update({User.last_seen_at: now},
                               synchronize_session=False))
            if changed:
                db.commit()
        except Exception:
            db.rollback()
            with presence_write_guard:
                presence_written_at.pop(user_id, None)
            logger.exception('Не удалось обновить web presence пользователя %s',
                             user_id)
        return None

    @app.route('/presence/ping', methods=['POST'])
    def presence_ping():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        db = get_db()
        user = db.query(User).filter(User.id == session['user_id']).first()
        if user is None:
            return jsonify({'error': 'not_found'}), 404
        return jsonify({'ok': True, 'presence': _user_presence(user)})

    @app.route('/')
    def main_menu():
        if session.get('user_id'):
            return redirect('/home')
        return render_template('main_menu.html')

    @app.route('/about')
    def about_page():
        if session.get('user_id'):
            db = get_db()
            user = db.query(User).filter(User.id == session['user_id']).first()
            if user is not None:
                user.about_seen_at = datetime.now()
                db.commit()
        return render_template('about.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect('/')

    @app.route('/home')
    def index():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact
        from data.devices import Device
        db = get_db()
        _seed_legacy_creator_flags(db)
        user = db.query(User).filter(User.id == session['user_id']).first()
        contacts_count = db.query(Contact).filter(Contact.user_id == user.id).count()
        messages_count = db.query(Messages).filter(Messages.user_id == user.id).count()
        device_connected = db.query(Device.id).filter(Device.user_id == user.id).first() is not None
        return render_template(
            'index.html',
            user=_mark_profile_badges(user),
            device_connected=device_connected,
            connect_code=user.connect_code,
            contacts_count=contacts_count,
            messages_count=messages_count,
            has_avatar=os.path.exists(_avatar_file(user.id)),
            username=user.username or '',
            preferred_lang=user.preferred_lang or 'ru',
        )

    @app.route('/home/username', methods=['POST'])
    def change_username():
        if not session.get('user_id'):
            return redirect('/login')
        db = get_db()
        me = db.query(User).filter(User.id == session['user_id']).first()
        is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        new = _normalize_username(request.form.get('username'))
        error = _validate_username(new)
        was_creator = _is_creator_user(me) or _is_legacy_creator_user(me)
        if (not error and _username_reserved_for_creator(new)
                and not was_creator):
            error = "Этот User ID зарезервирован"
        if not error and db.query(User).filter(
                User.username == new, User.id != me.id).first():
            error = "Этот User ID уже занят"
        if error:
            return (jsonify({'error': error}), 400) if is_xhr \
                else redirect('/home')
        if was_creator:
            me.is_creator = True
        me.username = new
        db.commit()
        if is_xhr:
            return jsonify({'ok': True, 'username': new})
        return redirect('/home')

    @app.route('/profile')
    def profile_page():
        """Страница профиля: личные данные + форма смены пароля."""
        if not session.get('user_id'):
            return redirect('/login')
        db = get_db()
        _seed_legacy_creator_flags(db)
        me = db.query(User).filter(User.id == session['user_id']).first()
        return render_template('profile.html', user=_mark_profile_badges(me))

    @app.route('/profile/password', methods=['POST'])
    def profile_password():
        """Смена пароля: требуется подтверждение текущего."""
        if not session.get('user_id'):
            return redirect('/login')
        db = get_db()
        _seed_legacy_creator_flags(db)
        me = db.query(User).filter(User.id == session['user_id']).first()
        old = request.form.get('old_password') or ''
        new1 = request.form.get('new_password') or ''
        new2 = request.form.get('new_password2') or ''
        error = None
        if not check_password_hash(me.hashed_password, old):
            error = "Текущий пароль введён неверно"
        elif len(new1) < 4:
            error = "Новый пароль слишком короткий (минимум 4 символа)"
        elif new1 != new2:
            error = "Новые пароли не совпадают"
        elif new1 == old:
            error = "Новый пароль совпадает со старым"
        if error:
            return render_template('profile.html',
                                   user=_mark_profile_badges(me),
                                   pw_error=error)
        me.hashed_password = generate_password_hash(new1)
        db.commit()
        return render_template('profile.html', user=_mark_profile_badges(me),
                               pw_success="Пароль обновлён")

    @app.route('/profile/update', methods=['POST'])
    def profile_update():
        """Смена личных данных: имя, фамилия, email, User ID. Email
        и username уникальны — конфликт даёт ошибку, профиль остаётся
        с прежними значениями."""
        if not session.get('user_id'):
            return redirect('/login')
        import re as _re_email
        db = get_db()
        me = db.query(User).filter(User.id == session['user_id']).first()
        name = (request.form.get('name') or '').strip()
        surname = (request.form.get('surname') or '').strip()
        email = (request.form.get('email') or '').strip()
        username = _normalize_username(request.form.get('username'))
        was_creator = _is_creator_user(me) or _is_legacy_creator_user(me)
        error = None
        if not name:
            error = "Имя не может быть пустым"
        elif not email or '@' not in email:
            error = "Введите корректный email"
        elif (db.query(User)
              .filter(User.email == email, User.id != me.id).first()):
            error = "Этот email уже занят другим аккаунтом"
        else:
            uname_err = _validate_username(username)
            if uname_err:
                error = uname_err
            elif (_username_reserved_for_creator(username)
                  and not was_creator):
                error = "Этот User ID зарезервирован"
            elif (db.query(User)
                  .filter(User.username == username,
                          User.id != me.id).first()):
                error = "Этот User ID уже занят"
        if error:
            return render_template('profile.html',
                                   user=_mark_profile_badges(me),
                                   info_error=error)
        me.name = name
        me.surname = surname or None
        me.email = email
        if was_creator:
            me.is_creator = True
        me.username = username
        db.commit()
        return render_template('profile.html', user=_mark_profile_badges(me),
                               info_success="Данные сохранены")

    @app.route('/home/lang', methods=['POST'])
    def change_lang():
        """Сменить язык, на который Ollama переводит чужие сообщения.
        Принимает ISO-код, валидирует против whitelist (любая отсебятина
        отвалится — мы не хотим, чтобы LLM получала мусор в target_lang)."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        allowed = {'ru', 'en', 'es', 'de', 'fr', 'it', 'pt',
                   'uk', 'tr', 'zh', 'ja', 'ko', 'ar'}
        new = (request.form.get('lang') or '').strip().lower()
        if new not in allowed:
            return jsonify({'error': 'bad_lang'}), 400
        db = get_db()
        me = db.query(User).filter(User.id == session['user_id']).first()
        me.preferred_lang = new
        db.commit()
        return jsonify({'ok': True, 'lang': new})

    @app.route('/messenger')
    def messenger_index():
        if not session.get('user_id'):
            return redirect('/login')
        return contacts_index()

    @app.route('/messenger/legacy')
    def messenger_legacy_index():
        if not session.get('user_id'):
            return redirect('/login')
        db = get_db()
        return render_template('messenger.html',
                               conversations=_dm_conversations(db, session['user_id']),
                               selected_id=None)

    @app.route('/messenger/<int:user_id>')
    def messenger_conversation(user_id):
        if not session.get('user_id'):
            return redirect('/login')
        db = get_db()
        me_id = session['user_id']
        if user_id == me_id:
            return redirect('/contacts')
        partner = db.query(User).filter(User.id == user_id).first()
        if partner is None:
            return 'Not Found', 404
        handle = _ensure_synapse_handle(db, me_id, partner)
        db.commit()
        return contact_detail(handle.contact_id)

    @app.route('/messenger/legacy/<int:user_id>')
    def messenger_legacy_conversation(user_id):
        if not session.get('user_id'):
            return redirect('/login')
        db = get_db()
        me_id = session['user_id']
        if user_id == me_id:
            return redirect('/messenger/legacy')
        partner = db.query(User).filter(User.id == user_id).first()
        if partner is None:
            return 'Not Found', 404
        return render_template('messenger.html',
                               conversations=_dm_conversations(db, me_id),
                               selected_id=user_id)

    @app.route('/messenger/conversations.json')
    def messenger_conversations_json():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        db = get_db()
        return jsonify({'conversations':
                        _dm_conversations(db, session['user_id'])})

    @app.route('/messenger/users/search.json')
    def messenger_users_search():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        q = (request.args.get('q') or '').strip().lower()
        if not q:
            return jsonify({'users': []})
        db = get_db()
        _seed_legacy_creator_flags(db)
        me_id = session['user_id']
        users = db.query(User).filter(User.id != me_id).order_by(User.id.asc()).all()
        matched = []
        for user in users:
            uname = (user.username or '').lower()
            full = ((user.name or '') + ' ' + (user.surname or '')).strip().lower()
            if q in uname or (full and q in full):
                matched.append(_dm_user_card(user))
            if len(matched) >= 20:
                break
        return jsonify({'users': matched})

    @app.route('/messenger/<int:user_id>/messages.json')
    def messenger_messages_json(user_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        db = get_db()
        me_id = session['user_id']
        if user_id == me_id:
            return jsonify({'error': 'self'}), 400
        partner = db.query(User).filter(User.id == user_id).first()
        if partner is None:
            return jsonify({'error': 'not_found'}), 404
        msgs = _dm_load_conversation(db, me_id, partner, mark_read=True)
        return jsonify({'partner': _dm_user_card(partner), 'messages': msgs})

    @app.route('/messenger/<int:user_id>/send', methods=['POST'])
    def messenger_send(user_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.direct import DirectMessage
        db = get_db()
        me_id = session['user_id']
        if user_id == me_id:
            return jsonify({'error': 'self'}), 400
        partner = db.query(User).filter(User.id == user_id).first()
        if partner is None:
            return jsonify({'error': 'not_found'}), 404

        text = (request.form.get('text') or '').strip()
        upload = request.files.get('file')
        has_file = upload is not None and bool(upload.filename)
        if not text and not has_file:
            return jsonify({'error': 'empty'}), 400
        voice_upload = (request.form.get('voice') or '').lower() in (
            '1', 'true', 'on')

        now = datetime.now()
        msg = DirectMessage(sender_id=me_id, recipient_id=user_id,
                            text=text or None, created_at=now)
        db.add(msg)
        db.flush()

        attachments = []
        if has_file:
            att, _data = _save_direct_upload(
                db, me_id, msg.id, upload, voice_upload=voice_upload)
            if att is None:
                db.rollback()
                return jsonify({'error': 'empty'}), 400
            if not msg.text:
                msg.text = _DM_PLACEHOLDER.get(att.kind, '📎 Файл')
            db.flush()
            attachments = [{'id': att.id, 'kind': att.kind,
                            'mime': att.mime,
                            'name': att.original_name}]

        sender = db.query(User).filter(User.id == me_id).first()
        users_by_id = {me_id: sender, user_id: partner}
        _mirror_direct_message_for_owner(
            db, msg, me_id, users_by_id)
        recipient_msg = _mirror_direct_message_for_owner(
            db, msg, user_id, users_by_id)

        db.commit()
        if recipient_msg is not None:
            _notify_webpush_message(recipient_msg.id)
        return jsonify({'ok': True, 'id': msg.id, 'text': msg.text or '',
                        'time': now.strftime('%H:%M'), 'outgoing': True,
                        'attachments': attachments})

    @app.route('/messenger/avatar/<int:user_id>')
    def messenger_avatar(user_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        result = _read_avatar(user_id)
        if result is None:
            return 'Not Found', 404
        raw, mime = result
        return Response(raw, mimetype=mime)

    @app.route('/dm/attachments/<int:attachment_id>')
    def dm_attachment_get(attachment_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.crypto import decrypt_bytes
        from data.direct import DirectAttachment, DirectMessage
        db = get_db()
        me_id = session['user_id']
        att = db.query(DirectAttachment).filter(
            DirectAttachment.id == attachment_id).first()
        if att is None:
            return 'Not Found', 404
        msg = db.query(DirectMessage).filter(
            DirectMessage.id == att.message_id).first()
        if msg is None or me_id not in (msg.sender_id, msg.recipient_id):
            return 'Not Found', 404
        full = os.path.join(_media_root(), att.stored_path)
        if not os.path.exists(full):
            return 'Not Found', 404
        with open(full, 'rb') as f:
            raw = decrypt_bytes(f.read())
        return Response(raw, mimetype=att.mime or 'application/octet-stream')

    @app.route('/home/avatar', methods=['GET', 'POST'])
    def avatar():
        if not session.get('user_id'):
            return redirect('/login') if request.method == 'POST' \
                else ('Unauthorized', 401)
        user_id = session['user_id']

        if request.method == 'POST':
            upload = request.files.get('avatar')
            if upload is None or not upload.filename:
                return redirect('/home')
            data = upload.read()
            mime = (upload.mimetype or '').lower()
            if not data or len(data) > 5 * 1024 * 1024 \
                    or not mime.startswith('image/'):
                return redirect('/home')
            _write_encrypted_media_path(_avatar_file(user_id), data)
            with open(_avatar_mime_file(user_id), 'w', encoding='utf-8') as f:
                f.write(mime)
            return redirect('/home')

        result = _read_avatar(user_id)
        if result is None:
            return 'Not Found', 404
        raw, mime = result
        return Response(raw, mimetype=mime)

    @app.route('/users')
    def users_list():
        if not session.get('user_id'):
            return redirect('/login')
        if not _is_admin():
            abort(403)
        db = get_db()
        users = db.query(User).order_by(User.id.asc()).all()
        summaries = [_admin_user_card_summary(db, u) for u in users]
        return render_template('users.html', users=summaries)

    @app.route('/users/presence.json')
    def users_presence_json():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        if not _is_admin():
            abort(403)
        db = get_db()
        users = db.query(User).order_by(User.id.asc()).all()
        return jsonify({'users': [
            {'id': user.id, 'presence': _user_presence(user)}
            for user in users
        ]})

    @app.route('/users/<int:user_id>/diagnostics.json')
    def user_diagnostics(user_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        if not _is_admin():
            abort(403)
        db = get_db()
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            abort(404)
        return jsonify(_admin_user_summary(db, user))

    @app.route('/users/<int:user_id>/reset-code', methods=['POST'])
    def admin_reset_connect_code(user_id):
        if not session.get('user_id'):
            return redirect('/login')
        if not _is_admin():
            abort(403)
        db = get_db()
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            abort(404)
        old_code = user.connect_code
        user.connect_code = _generate_unique_code(db, exclude=old_code)
        user.modified_date = datetime.now()
        db.commit()
        return redirect('/users')

    @app.route('/users/devices/<int:device_id>/delete', methods=['POST'])
    def admin_device_delete(device_id):
        if not session.get('user_id'):
            return redirect('/login')
        if not _is_admin():
            abort(403)
        from data.devices import Device
        db = get_db()
        device = db.query(Device).filter(Device.id == device_id).first()
        if not device:
            abort(404)
        db.delete(device)
        db.commit()
        return redirect('/users')

    @app.route('/users/webpush/<int:subscription_id>/delete', methods=['POST'])
    def admin_webpush_delete(subscription_id):
        if not session.get('user_id'):
            return redirect('/login')
        if not _is_admin():
            abort(403)
        from data.webpush_subscriptions import WebPushSubscription
        db = get_db()
        sub = (db.query(WebPushSubscription)
               .filter(WebPushSubscription.id == subscription_id)
               .first())
        if not sub:
            abort(404)
        db.delete(sub)
        db.commit()
        return redirect('/users')

    @app.route('/users/<int:user_id>/avatar')
    def user_avatar(user_id):
        if not _is_admin():
            return 'Forbidden', 403
        result = _read_avatar(user_id)
        if result is None:
            return 'Not Found', 404
        raw, mime = result
        return Response(raw, mimetype=mime)


    @app.route('/devices')
    def devices_list():
        if not session.get('user_id'):
            return redirect('/login')
        from data.devices import Device
        db = get_db()
        user_id = session['user_id']
        devices = (db.query(Device)
                   .filter(Device.user_id == user_id)
                   .order_by(Device.last_seen_at.desc().nullslast(),
                             Device.created_at.desc())
                   .all())
        return render_template('devices.html', devices=devices)

    @app.route('/devices/<int:device_id>/delete', methods=['POST'])
    def device_delete(device_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.devices import Device
        db = get_db()
        user_id = session['user_id']
        device = db.query(Device).filter(
            Device.id == device_id, Device.user_id == user_id).first()
        if not device:
            return 'Not Found', 404
        db.delete(device)
        db.commit()
        return redirect('/devices')

    @app.route('/telegram')
    def telegram_page():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        user_id = session['user_id']
        return render_template('telegram.html',
                               tg=telegram_bridge.status(user_id))

    @app.route('/telegram/connect', methods=['POST'])
    def telegram_connect():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        user_id = session['user_id']
        phone = (request.form.get('phone') or '').strip()
        if phone:
            try:
                telegram_bridge.request_code(phone, user_id=user_id)
            except telegram_bridge.TelegramAuthError as exc:
                return render_template('telegram.html',
                                       tg=telegram_bridge.status(
                                           user_id, refresh=False),
                                       error=str(exc))
            except Exception:  # noqa: BLE001
                app.logger.exception('Unexpected Telegram connect error')
                return render_template(
                    'telegram.html',
                    tg=telegram_bridge.status(user_id, refresh=False),
                    error='Не удалось связаться с Telegram. Попробуйте позже.')
        return redirect('/telegram')

    @app.route('/telegram/resend', methods=['POST'])
    def telegram_resend():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        user_id = session['user_id']
        try:
            telegram_bridge.resend_code(user_id=user_id)
        except telegram_bridge.TelegramAuthError as exc:
            return render_template(
                'telegram.html',
                tg=telegram_bridge.status(user_id, refresh=False),
                error=str(exc))
        except Exception:  # noqa: BLE001
            app.logger.exception('Unexpected Telegram resend error')
            return render_template(
                'telegram.html',
                tg=telegram_bridge.status(user_id, refresh=False),
                error='Не удалось запросить новый способ доставки. '
                      'Попробуйте позже.')
        return redirect('/telegram')

    @app.route('/telegram/reset', methods=['POST'])
    def telegram_reset():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        user_id = session['user_id']
        try:
            telegram_bridge.reset_login(user_id=user_id)
        except telegram_bridge.TelegramAuthError as exc:
            return render_template(
                'telegram.html',
                tg=telegram_bridge.status(user_id, refresh=False),
                error=str(exc))
        except Exception:  # noqa: BLE001
            app.logger.exception('Unexpected Telegram reset error')
            return render_template(
                'telegram.html',
                tg=telegram_bridge.status(user_id, refresh=False),
                error='Не удалось сбросить попытку входа. Попробуйте позже.')
        return redirect('/telegram')

    @app.route('/telegram/code', methods=['POST'])
    def telegram_code():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        user_id = session['user_id']
        code = (request.form.get('code') or '').strip()
        if code:
            try:
                telegram_bridge.submit_code(code, user_id=user_id)
            except telegram_bridge.TelegramAuthError as exc:
                return render_template('telegram.html',
                                       tg=telegram_bridge.status(
                                           user_id, refresh=False),
                                       error=str(exc))
            except Exception:  # noqa: BLE001
                app.logger.exception('Unexpected Telegram code error')
                return render_template(
                    'telegram.html',
                    tg=telegram_bridge.status(user_id, refresh=False),
                    error='Не удалось проверить код. Попробуйте позже.')
        return redirect('/telegram')

    @app.route('/telegram/password', methods=['POST'])
    def telegram_password():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        user_id = session['user_id']
        password = request.form.get('password') or ''
        if password:
            try:
                telegram_bridge.submit_password(password, user_id=user_id)
            except telegram_bridge.TelegramAuthError as exc:
                return render_template('telegram.html',
                                       tg=telegram_bridge.status(
                                           user_id, refresh=False),
                                       error=str(exc))
            except Exception:  # noqa: BLE001
                app.logger.exception('Unexpected Telegram password error')
                return render_template(
                    'telegram.html',
                    tg=telegram_bridge.status(user_id, refresh=False),
                    error='Не удалось проверить пароль. Попробуйте позже.')
        return redirect('/telegram')

    @app.route('/telegram/logout', methods=['POST'])
    def telegram_logout():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        telegram_bridge.logout(user_id=session['user_id'])
        return redirect('/telegram')

    @app.route('/telegram/settings', methods=['POST'])
    def telegram_settings():
        if not session.get('user_id'):
            return redirect('/login')
        from data import telegram_bridge
        user_id = session['user_id']
        # У нас 3 отдельные мини-формы (по одной на чекбокс). `field`
        # говорит, какое поле меняется — иначе HTML «отсутствующий»
        # чекбокс затёр бы значения других тоглов в False.
        field = request.form.get('field')
        if field == 'skip_muted':
            telegram_bridge.update_filters(
                skip_muted=request.form.get('skip_muted') == 'on',
                user_id=user_id)
        elif field == 'skip_archived':
            telegram_bridge.update_filters(
                skip_archived=request.form.get('skip_archived') == 'on',
                user_id=user_id)
        elif field == 'ghost_mode':
            telegram_bridge.set_ghost_mode(
                request.form.get('ghost_mode') == 'on',
                user_id=user_id)
        else:
            # Старый формат (одна форма со всеми чекбоксами) — для
            # обратной совместимости со внешними скриптами/тестами.
            telegram_bridge.update_filters(
                skip_muted=request.form.get('skip_muted') == 'on',
                skip_archived=request.form.get('skip_archived') == 'on',
                user_id=user_id,
            )
        return redirect('/telegram')

    @app.route('/register', methods=['GET', 'POST'])
    def register():
        if request.method == 'POST':
            db = get_db()
            name = request.form.get('name')
            surname = request.form.get('surname')
            email = request.form.get('email')
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')
            sex = request.form.get('sex')
            username = _normalize_username(request.form.get('username'))
            # ISO-код языка для перевода чужих сообщений через Ollama.
            # Поддерживаем закрытый список — иначе пользователь введёт
            # «русский», и потом LLM получит мусор в `target_lang`.
            allowed_langs = {'ru', 'en', 'es', 'de', 'fr', 'it', 'pt',
                             'uk', 'tr', 'zh', 'ja', 'ko', 'ar'}
            preferred_lang = (request.form.get('preferred_lang') or '').strip().lower()
            if preferred_lang not in allowed_langs:
                preferred_lang = 'ru'

            def fail(msg):
                return render_template('register.html', message=msg,
                                       values=request.form)

            if password != confirm_password:
                return fail("Пароли не совпадают")

            username_error = _validate_username(username)
            if username_error:
                return fail(username_error)
            if _username_reserved_for_creator(username):
                return fail("Этот User ID зарезервирован")

            if db.query(User).filter(User.email == email).first():
                return fail("Такой пользователь уже есть")

            if db.query(User).filter(User.username == username).first():
                return fail("Этот User ID уже занят — придумайте другой")

            user = User(
                name=name,
                surname=surname,
                email=email,
                sex=sex,
                username=username,
                preferred_lang=preferred_lang,
                hashed_password=generate_password_hash(password),
            )
            user.connect_code = _generate_unique_code(db)
            db.add(user)
            db.commit()
            session['user_id'] = user.id
            session['can_manage_users'] = _user_can_manage_users(user)
            session.permanent = True
            return redirect('/home')

        return render_template('register.html')

    @app.route('/code')
    def code():
        if not session.get('user_id'):
            return redirect('/login')
        from data.devices import Device
        db = get_db()
        user = db.query(User).filter(User.id == session['user_id']).first()
        has_device = db.query(Device.id).filter(Device.user_id == user.id).first() is not None
        if has_device:
            return redirect('/home')
        return render_template('code.html', code=user.connect_code)

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if session.get('user_id'):
            return redirect('/home')
        if request.method == 'POST':
            db = get_db()
            email = request.form.get('email')
            password = request.form.get('password')
            user = db.query(User).filter(User.email == email).first()
            if user and check_password_hash(user.hashed_password, password):
                session['user_id'] = user.id
                session['can_manage_users'] = _user_can_manage_users(user)
                session.permanent = True
                return redirect('/home')
            return render_template('login.html', message="Неверный email или пароль")
        return render_template('login.html')

    @app.route('/messages')
    def messages():
        return redirect('/contacts')

    @app.route('/contacts')
    def contacts_index():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, consolidate_android_group_contacts
        db = get_db()
        user_id = session['user_id']
        archive_mode = _archive_mode_from_request()
        creator_mode = request.args.get('creators') == '1'
        _sync_direct_messages_to_contacts(db, user_id)
        consolidate_android_group_contacts(db, user_id)
        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        contacts = _filter_discussion_contacts(db, contacts)
        contacts = _filter_archived_contacts(contacts, archive_mode)
        _enrich_with_last_message(db, contacts)
        return render_template('contacts.html', contacts=contacts,
                               selected=None, selected_handles=[],
                               messages=None, archive_mode=archive_mode,
                               creator_mode=creator_mode,
                               creator_cards=(
                                   _creator_cards(db, user_id)
                                   if creator_mode else []))

    @app.route('/contacts.json')
    def contacts_index_json():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, consolidate_android_group_contacts
        db = get_db()
        user_id = session['user_id']
        archive_mode = _archive_mode_from_request()
        _kick_telegram_recent_sync(user_id)
        consolidate_android_group_contacts(db, user_id)
        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        contacts = _filter_discussion_contacts(db, contacts)
        contacts = _filter_archived_contacts(contacts, archive_mode)
        _enrich_with_last_message(db, contacts)
        return jsonify({'contacts': [
            {
                'id': c.id,
                'display_name': c.display_name,
                'initial': c.initial,
                'avatar_color': c.avatar_color,
                'avatar_url': c.avatar_url,
                'messengers': c.messengers,
                'last_preview': c.last_preview,
                'last_time': c.last_time,
                'last_outgoing': bool(getattr(c, 'last_outgoing', False)),
                'last_tg_read': getattr(c, 'last_tg_read', None),
                'unread_count': c.unread_count or 0,
                'pinned': bool(c.pinned_at),
                'muted': bool(c.muted),
                'archived': bool(c.archived),
                'is_creator': bool(getattr(c, 'is_creator', False)),
                'creator_title': getattr(c, 'creator_title', ''),
                'presence': getattr(c, 'presence', None),
            }
            for c in contacts
        ]})

    @app.route('/contacts/search.json')
    def contacts_search_json():
        """Полнотекстовый поиск по всем чатам пользователя.

        Отдаёт:
          - `contact_ids` — id контактов, у которых имя или хоть одно сообщение
            содержит запрос (используется для фильтрации левого списка).
          - `matches` — до 30 конкретных сообщений со сниппетами для
            выпадающего списка результатов. Каждый элемент: contact_id,
            contact_name, messenger, message_id, snippet, time.
        """
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        q = (request.args.get('q') or '').strip().lower()
        if not q:
            return jsonify({'contact_ids': [], 'matches': []})
        db = get_db()
        user_id = session['user_id']
        archive_mode = _archive_mode_from_request()
        _sync_direct_messages_to_contacts(db, user_id)

        contacts = db.query(Contact).filter(Contact.user_id == user_id).all()
        contacts = _filter_discussion_contacts(db, contacts)
        contacts = _filter_archived_contacts(contacts, archive_mode)
        contact_by_id = {c.id: c for c in contacts}
        matched_by_name = {c.id for c in contacts
                           if q in (c.display_name or '').lower()}

        visible_contact_ids = set(contact_by_id)
        if visible_contact_ids:
            handle_rows = (db.query(MessengerHandle)
                           .filter(MessengerHandle.user_id == user_id,
                                   MessengerHandle.contact_id.in_(
                                       visible_contact_ids))
                           .all())
        else:
            handle_rows = []
        handle_to_meta = {
            h.id: (h.contact_id, h.messenger_name)
            for h in handle_rows
        }
        matches = []
        matched_in_msg = set()
        if handle_to_meta:
            msgs = (db.query(Messages)
                    .filter(Messages.handle_id.in_(list(handle_to_meta.keys())))
                    .order_by(Messages.created_at.desc().nullslast(),
                              Messages.id.desc())
                    .all())
            for m in msgs:
                if len(matches) >= 30:
                    break
                meta = handle_to_meta.get(m.handle_id)
                if meta is None:
                    continue
                cid, messenger = meta
                low = (m.text or '').lower()
                idx = low.find(q)
                if idx < 0:
                    continue
                matched_in_msg.add(cid)
                start = max(0, idx - 50)
                end = min(len(m.text), idx + len(q) + 80)
                snippet = m.text[start:end]
                if start > 0:
                    snippet = '…' + snippet
                if end < len(m.text):
                    snippet = snippet + '…'
                c = contact_by_id.get(cid)
                matches.append({
                    'contact_id': cid,
                    'contact_name': c.display_name if c else '',
                    'messenger': messenger,
                    'message_id': m.id,
                    'snippet': snippet,
                    'time': m.time,
                })
        return jsonify({
            'contact_ids': sorted(matched_by_name | matched_in_msg),
            'matches': matches,
        })

    @app.route('/contacts/synapse/users/search.json')
    def contacts_synapse_users_search():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        q = (request.args.get('q') or '').strip().lower()
        if not q:
            return jsonify({'users': []})
        db = get_db()
        _seed_legacy_creator_flags(db)
        me_id = session['user_id']
        users = db.query(User).filter(User.id != me_id).order_by(User.id.asc()).all()
        matched = []
        for user in users:
            uname = (user.username or '').lower()
            full = ((user.name or '') + ' ' + (user.surname or '')).strip().lower()
            if q in uname or (full and q in full):
                matched.append(_dm_user_card(user))
            if len(matched) >= 20:
                break
        return jsonify({'users': matched})

    @app.route('/contacts/synapse/start/<int:user_id>', methods=['POST'])
    def contacts_synapse_start(user_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        db = get_db()
        me_id = session['user_id']
        if user_id == me_id:
            return jsonify({'error': 'self'}), 400
        partner = db.query(User).filter(User.id == user_id).first()
        if partner is None:
            return jsonify({'error': 'not_found'}), 404
        handle = _ensure_synapse_handle(db, me_id, partner)
        db.commit()
        return jsonify({'ok': True, 'contact_id': handle.contact_id,
                        'messenger': SYNAPSE_MESSENGER})

    @app.route('/contacts/telegram/users/search.json')
    def contacts_telegram_users_search():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        q = (request.args.get('q') or '').strip()
        username = q[1:] if q.startswith('@') else q
        if not username:
            return jsonify({'users': []})
        if not telegram_bridge.is_configured():
            return jsonify({'error': 'telegram_not_configured'}), 502
        try:
            info = telegram_bridge.resolve_username_info(
                username, user_id=session['user_id'])
        except Exception as exc:  # noqa: BLE001
            return jsonify({'users': [], 'detail': str(exc)})
        avatar_url = None
        try:
            raw_avatar = telegram_bridge.download_profile_photo(
                username, user_id=session['user_id'])
            if raw_avatar:
                avatar_url = (
                    "data:image/jpeg;base64,"
                    + base64.b64encode(raw_avatar).decode("ascii")
                )
        except Exception:  # noqa: BLE001
            avatar_url = None
        return jsonify({'users': [{
            'chat_id': int(info.get('chat_id') or 0),
            'title': info.get('title') or username,
            'username': info.get('username') or username,
            'kind': info.get('kind') or 'private',
            'avatar_url': avatar_url,
        }]})

    @app.route('/contacts/<int:contact_id>')
    def contact_detail(contact_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MessengerHandle
        from data.matching import display_author
        db = get_db()
        user_id = session['user_id']
        archive_mode = _archive_mode_from_request()
        _sync_direct_messages_to_contacts(db, user_id)
        contact = (
            db.query(Contact)
            .filter(Contact.id == contact_id, Contact.user_id == user_id)
            .first()
        )
        if not contact:
            return 'Not Found', 404

        contact.last_read_at = datetime.now()
        _commit_best_effort(db, 'contact_detail.last_read_at')

        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        contacts = _filter_discussion_contacts(db, contacts)
        contacts = _filter_archived_contacts(contacts, archive_mode)
        _enrich_with_last_message(db, contacts)
        _avatar_for(contact)

        handles = db.query(MessengerHandle).filter(MessengerHandle.contact_id == contact.id).all()
        _mark_contact_creator_from_handles(contact, handles, _creator_user_ids(db))
        _mark_synapse_contact_read(db, user_id, handles)
        # Контакт — «папка»: чат на каждый мессенджер. Показываем один.
        available = []
        for h in handles:
            if h.messenger_name not in available:
                available.append(h.messenger_name)
        current_m = _pick_messenger(available, request.args.get('m'))
        m_handles = [h for h in handles if h.messenger_name == current_m]
        selected_presence = _presence_for_handles(
            db, user_id, m_handles, refresh_telegram=True)
        handle_ids = [h.id for h in m_handles]
        selected_handles = [
            {'messenger': h.messenger_name, 'sender': h.sender_raw} for h in m_handles
        ]
        tg_chat_handle = next((h for h in m_handles
                               if h.messenger_name == 'Telegram'
                               and h.tg_chat_id is not None), None)
        synapse_handle = next((h for h in m_handles
                               if h.messenger_name == SYNAPSE_MESSENGER), None)
        _tg_handle, _notif_handle = _reply_channel(m_handles)
        can_reply = (synapse_handle is not None or _tg_handle is not None
                     or _notif_handle is not None)
        reply_via = ('telegram' if _tg_handle is not None
                     else ('synapse' if synapse_handle is not None
                           else ('notif' if _notif_handle is not None else None)))
        is_group = any(_is_group_handle(h) for h in m_handles)
        # Тип чата нужен фронту, чтобы под канальными постами появлялась
        # кнопка «💬 Комментарии» (linked discussion group).
        chat_type = (tg_chat_handle.tg_chat_type
                     if tg_chat_handle is not None else None)
        msgs = (
            db.query(Messages)
            .filter(Messages.handle_id.in_(handle_ids),
                    or_(Messages.delivery_status.is_(None),
                        Messages.delivery_status != 'scheduled'))
            .order_by(Messages.created_at.desc().nullslast(), Messages.id.desc())
            .limit(80)
            .all()
        )
        msgs = list(reversed(msgs))
        for m in msgs:
            m.display_author = display_author(m.sender, contact.display_name)
            m.date_label = _message_date_label(m.created_at)
            m.author_avatar_url = _message_author_avatar_url(m)
            m.author_profile_url = _message_author_profile_url(m)
        _attach_media(db, msgs)
        _attach_replies(db, msgs, contact)
        _attach_reactions(db, msgs)
        _attach_forwards(db, msgs, user_id)
        _attach_edits(db, msgs)
        return render_template('contacts.html', contacts=contacts, selected=contact,
                               selected_handles=selected_handles, messages=msgs,
                               can_reply=can_reply, reply_via=reply_via,
                               is_group=is_group, chat_type=chat_type,
                               notifications_muted=bool(contact.muted),
                               messengers=available, current_messenger=current_m,
                               selected_presence=selected_presence,
                               archive_mode=archive_mode,
                               creator_mode=False, creator_cards=[])

    @app.route('/contacts/<int:contact_id>/messages.json')
    def contact_messages_json(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data.matching import display_author
        _recover_interrupted_media_deliveries()
        db = get_db()
        user_id = session['user_id']
        _kick_telegram_recent_sync(user_id)
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id, Contact.user_id == user_id).first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()
        _mark_contact_creator_from_handles(contact, handles, _creator_user_ids(db))
        _mark_synapse_contact_read(db, user_id, handles)
        available = []
        for h in handles:
            if h.messenger_name not in available:
                available.append(h.messenger_name)
        current_m = _pick_messenger(available, request.args.get('m'))
        m_handles = [h for h in handles if h.messenger_name == current_m]
        selected_presence = _presence_for_handles(
            db, user_id, m_handles, refresh_telegram=True)
        handle_ids = [h.id for h in m_handles]
        selected_handles = [
            {'messenger': h.messenger_name, 'sender': h.sender_raw} for h in m_handles
        ]
        tg_chat_handle = next((h for h in m_handles
                               if h.messenger_name == 'Telegram'
                               and h.tg_chat_id is not None), None)
        synapse_handle = next((h for h in m_handles
                               if h.messenger_name == SYNAPSE_MESSENGER), None)
        _tg_handle, _notif_handle = _reply_channel(m_handles)
        can_reply = (synapse_handle is not None or _tg_handle is not None
                     or _notif_handle is not None)
        reply_via = ('telegram' if _tg_handle is not None
                     else ('synapse' if synapse_handle is not None
                           else ('notif' if _notif_handle is not None else None)))
        is_group = any(_is_group_handle(h) for h in m_handles)
        is_forum = bool(tg_chat_handle is not None
                        and tg_chat_handle.tg_is_forum)
        # `/messages.json` вызывается при каждом открытии и polling чата.
        # Внешний MTProto-запрос определения форума здесь блокировал ответ
        # вплоть до таймаута. Метаданные форума обновляет Telegram bridge;
        # отдельный `/forum-topics.json` остаётся для явного открытия тем.
        _avatar_for(contact)
        # Фильтр по теме (для форум-чатов): если ?topic_id=N — отдаём
        # только сообщения из этой темы. Если не задан — все сообщения
        # (как раньше; форумы UI должен открывать сразу с topic_id).
        topic_id_q = request.args.get('topic_id')
        try:
            topic_id_int = int(topic_id_q) if topic_id_q else None
        except ValueError:
            topic_id_int = None
        page_limit = 80
        try:
            before_id = int(request.args.get('before_id') or 0)
        except ValueError:
            before_id = 0
        msgs_q = db.query(Messages).filter(
            Messages.handle_id.in_(handle_ids),
            or_(Messages.delivery_status.is_(None),
                Messages.delivery_status != 'scheduled'))
        if topic_id_int is not None:
            msgs_q = msgs_q.filter(Messages.tg_topic_id == topic_id_int)
        if before_id:
            msgs_q = msgs_q.filter(Messages.id < before_id)
        msgs_desc = (msgs_q
                     .order_by(Messages.created_at.desc().nullslast(),
                               Messages.id.desc())
                     .limit(page_limit + 1)
                     .all())
        has_older = len(msgs_desc) > page_limit
        msgs = list(reversed(msgs_desc[:page_limit]))
        # Для того чтобы не обновлять страницу каждый раз как пришло уведомление
        if is_forum and topic_id_int is not None and tg_chat_handle is not None:
            # У форум-чата у каждой темы свой last_read — иначе открытие
            # одной темы тушит «непрочитанное» во всех остальных.
            from data.topic_reads import mark_topic_read
            mark_topic_read(db, tg_chat_handle.id, topic_id_int)
        else:
            contact.last_read_at = datetime.now()
        _commit_best_effort(db, 'messages_json.last_read_at')
        _attach_media(db, msgs)
        _attach_replies(db, msgs, contact)
        _attach_reactions(db, msgs)
        _attach_forwards(db, msgs, user_id)
        _attach_edits(db, msgs)
        # Подгружаем сохранённые «темы чата» (если уже анализировались):
        # отдадим клиенту, чтобы он сразу мог показать пин-бар сверху без
        # отдельного запроса.
        from data.chat_topics import get_topics as _get_topics
        # Для форум-чата отдаём темы LLM ТОЛЬКО открытой темы форума:
        # соседние темы форума имеют свои закрепы. Для обычного чата —
        # как раньше (topic_id=None).
        saved_topics = [] if before_id else _topics_with_time(
            db, _get_topics(db, contact_id, topic_id_int), handle_ids)
        return jsonify({
            'contact': {
                'id': contact.id,
                'display_name': contact.display_name,
                'initial': contact.initial,
                'avatar_color': contact.avatar_color,
                'avatar_url': contact.avatar_url,
                'handles': selected_handles,
                'can_reply': can_reply,
                'reply_via': reply_via,
                'is_group': is_group,
                'show_authors': bool(is_group or len(selected_handles) > 1),
                'is_forum': is_forum,
                'topic_id': topic_id_int,
                'messengers': available,
                'messenger': current_m,
                'chat_type': (tg_chat_handle.tg_chat_type
                              if tg_chat_handle is not None else None),
                'notifications_muted': bool(contact.muted),
                'archived': bool(contact.archived),
                'is_creator': bool(getattr(contact, 'is_creator', False)),
                'creator_title': getattr(contact, 'creator_title', ''),
                'presence': selected_presence,
            },
            'topics': saved_topics,
            'has_older': has_older,
            'older_before_id': msgs[0].id if msgs else None,
            'messages': [
                {'id': m.id, 'sender': m.sender, 'text': m.visible_text,
                 'text_html': m.visible_text_html,
                 'messenger_name': m.messenger_name,
                 'client_send_key': getattr(
                     m, 'notification_dedup_key', None),
                 'time': m.time,
                 'date_label': _message_date_label(m.created_at),
                 'outgoing': bool(m.outgoing),
                 'tg_read': bool(m.tg_read_at) if m.tg_message_id else None,
                 'delivery_status': getattr(m, 'delivery_status', None),
                 'delivery_error': getattr(m, 'delivery_error', None),
                 'deleted': bool(m.deleted_at),
                 'pinned': bool(m.pinned_at),
                 'ttl_seconds': m.tg_ttl_seconds,
                 'display_author': display_author(m.sender, contact.display_name),
                 'author_avatar_url': _message_author_avatar_url(m),
                 'author_profile_url': _message_author_profile_url(m),
                 'reply_to': m.reply_quote,
                 'fwd_from': m.fwd_quote,
                 'edits': getattr(m, 'edit_history', []),
                 'reactions': getattr(m, 'reactions', []),
                 'media_restore_kind': getattr(
                     m, 'media_restore_kind', None),
                 'media_restore_url': getattr(
                     m, 'media_restore_url', None),
                 'attachments': [{'id': a.id, 'kind': a.kind,
                                  'mime': a.mime,
                                  'name': a.original_name,
                                  'availability': a.availability,
                                  'url': a.url,
                                  'restore_url': a.restore_url,
                                  'has_sticker_pack': (
                                      bool(a.sticker_pack_key)
                                      or m.messenger_name == 'Telegram')}
                                 for a in m.media]}
                for m in msgs
            ],
            # Закреплённые сообщения чата — UI рисует «📌»-плашку сверху
            # с кратким текстом и кнопкой «перейти». Список — самые свежие
            # закрепы сверху.
            'pinned_messages': [
                {'id': m.id, 'text': (m.visible_text or '')[:160],
                 'author': display_author(m.sender, contact.display_name)}
                for m in sorted(
                    [mm for mm in msgs if mm.pinned_at is not None],
                    key=lambda mm: mm.pinned_at, reverse=True)
            ],
        })

    @app.route('/contacts/<int:contact_id>/send', methods=['POST'])
    def contact_send(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        from data.pending_replies import (PendingReply, STATUS_PENDING,
                                          STATUS_PICKED)
        db = get_db()
        user_id = session['user_id']
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id, Contact.user_id == user_id).first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        text = (request.form.get('text') or '').strip()
        upload = request.files.get('file')
        client_send_key = _client_send_key(request.form.get('client_send_key'))
        forward_raw = (request.form.get('forward_message_id') or '').strip()
        forward_source = None
        forward_meta = None
        if forward_raw:
            try:
                forward_id = int(forward_raw)
            except (TypeError, ValueError):
                forward_id = None
            if forward_id:
                forward_source = db.query(Messages).filter(
                    Messages.id == forward_id,
                    Messages.user_id == user_id).first()
            if forward_source is None:
                return jsonify({'error': 'forward_not_found'}), 404
            forward_meta = _forward_source_meta(db, user_id, forward_source)
        if not text and upload is None and forward_source is None:
            return jsonify({'error': 'empty'}), 400
        if client_send_key:
            existing = (db.query(Messages)
                        .filter(Messages.user_id == user_id,
                                Messages.notification_dedup_key
                                == client_send_key)
                        .order_by(Messages.id.desc())
                        .first())
            if existing is not None:
                return jsonify(_manual_send_duplicate_payload(
                    db, existing, user_id))
        voice_upload = False
        if upload is not None:
            voice_upload = (
                (request.form.get('voice') or '').lower()
                in ('1', 'true', 'on')
            )

        # Распознаём markdown: если текст содержит **жирный**, ||спойлер||,
        # `моноширный`, [текст](url) — шлём с parse_mode='md', а у себя
        # сохраняем plain без меток + готовый text_html (через
        # telethon-утилиты) для подсветки в bubble сразу же.
        md_parse_mode = None
        md_html = None
        md_plain = text
        if text and _has_markdown(text):
            try:
                from telethon.extensions import (markdown as _tg_md,
                                                  html as _tg_html)
                parsed_text, entities = _tg_md.parse(text)
                md_parse_mode = 'md'
                md_plain = parsed_text
                md_html = _tg_html.unparse(parsed_text, entities)
            except Exception:  # noqa: BLE001
                md_parse_mode = None
                md_html = None
                md_plain = text

        # Опции отправки: silent (без уведомления) и schedule_at (ISO
        # datetime — отложенная отправка). Доступны только через Telegram.
        silent = (request.form.get('silent') or '') in ('1', 'true', 'on')
        schedule_at = None
        schedule_raw = (request.form.get('schedule_at') or '').strip()
        if schedule_raw:
            try:
                # input type="datetime-local" даёт "YYYY-MM-DDTHH:MM" —
                # это локальное время браузера, парсим без таймзоны.
                schedule_at = datetime.strptime(
                    schedule_raw[:16], '%Y-%m-%dT%H:%M')
            except ValueError:
                try:
                    schedule_at = datetime.fromisoformat(schedule_raw)
                except ValueError:
                    schedule_at = None
            # В прошлом? Telegram отвергнет — отдадим понятную ошибку сразу.
            if schedule_at is not None and schedule_at <= datetime.now():
                return jsonify({
                    'error': 'bad_schedule',
                    'detail': 'Время отправки должно быть в будущем',
                }), 400

        # Учитываем «какой мессенджер открыт у пользователя» (?m=… в URL ленты).
        # Если поле есть — пробуем отправить через этого мессенджера; иначе
        # глобально предпочитаем Telegram.
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()
        requested_messenger = (request.form.get('messenger') or '').strip() or None
        m_handles = ([h for h in handles if h.messenger_name == requested_messenger]
                     if requested_messenger else handles)
        synapse_handle = next((h for h in m_handles
                               if h.messenger_name == SYNAPSE_MESSENGER), None)
        if synapse_handle is not None and (
                requested_messenger == SYNAPSE_MESSENGER
                or not any(h.messenger_name != SYNAPSE_MESSENGER
                           for h in m_handles)):
            partner_id = _synapse_partner_id(synapse_handle)
            if partner_id is None or partner_id == user_id:
                return jsonify({'error': 'not_found'}), 404

            from data.direct import DirectMessage
            now = datetime.now()
            direct_text = text
            if forward_source is not None:
                direct_text = _forward_text_for_target(
                    db, user_id, forward_source, SYNAPSE_MESSENGER)
                if text:
                    direct_text = direct_text + '\n\n' + text
            if not direct_text and upload is None:
                return jsonify({'error': 'empty'}), 400

            direct_msg = DirectMessage(
                sender_id=user_id,
                recipient_id=partner_id,
                text=direct_text or None,
                created_at=now,
            )
            db.add(direct_msg)
            db.flush()

            if upload is not None:
                direct_att, _data = _save_direct_upload(
                    db, user_id, direct_msg.id, upload,
                    voice_upload=voice_upload)
                if direct_att is None:
                    db.rollback()
                    return jsonify({'error': 'empty'}), 400
                if not direct_msg.text:
                    direct_msg.text = _DM_PLACEHOLDER.get(
                        direct_att.kind, '📎 Файл')
            shared_forward_paths = []
            if forward_source is not None:
                copied = _copy_message_attachments_to_direct(
                    db, forward_source, direct_msg.id)
                shared_forward_paths = [row.stored_path for row in copied]
                if copied and not direct_msg.text:
                    direct_msg.text = _DM_PLACEHOLDER.get(
                        copied[0].kind, '📎 Файл')
            db.flush()

            me = db.query(User).filter(User.id == user_id).first()
            partner = db.query(User).filter(User.id == partner_id).first()
            msg = None
            recipient_msg = None
            if me is not None and partner is not None:
                users_by_id = {user_id: me, partner_id: partner}
                msg = _mirror_direct_message_for_owner(
                    db, direct_msg, user_id, users_by_id)
                recipient_msg = _mirror_direct_message_for_owner(
                    db, direct_msg, partner_id, users_by_id)
            if forward_meta is not None:
                _apply_forward_meta(msg, forward_meta)
                _apply_forward_meta(recipient_msg, forward_meta)
            if msg is not None and client_send_key:
                msg.notification_dedup_key = client_send_key
            if msg is None:
                msg = Messages(
                    sender='Вы',
                    text=direct_msg.text,
                    messenger_name=SYNAPSE_MESSENGER,
                    time=now.strftime('%H:%M'),
                    user_id=user_id,
                    handle_id=synapse_handle.id,
                    created_at=now,
                    outgoing=True,
                    notification_dedup_key=client_send_key,
                )
                db.add(msg)
                db.flush()
                _apply_forward_meta(msg, forward_meta)
                _mirror_direct_attachments_to_message(
                    db, direct_msg, user_id, msg)

            reply_quote = None
            reply_raw = request.form.get('reply_to')
            if reply_raw and msg is not None:
                try:
                    reply_id = int(reply_raw)
                except (TypeError, ValueError):
                    reply_id = None
                if reply_id:
                    target = db.query(Messages).filter(
                        Messages.id == reply_id,
                        Messages.user_id == user_id,
                        Messages.handle_id == synapse_handle.id).first()
                    if target is not None:
                        msg.reply_to_message_id = target.id
                        from data.matching import display_author
                        rt_text = target.text or ''
                        reply_quote = {
                            'id': target.id,
                            'author': ('Вы' if target.outgoing
                                       else display_author(target.sender,
                                                            contact.display_name)),
                            'text': rt_text[:120] + ('...' if len(rt_text) > 120 else ''),
                        }
            if shared_forward_paths:
                # Между подготовкой DirectAttachment и commit очистка могла
                # успеть вытеснить source blob. Под общим lock проверяем его
                # ещё раз и одним атомарным commit публикуем все зеркала.
                with telegram_bridge.media_reference_guard():
                    if not all(_media_rel_path_exists(path)
                               for path in shared_forward_paths):
                        db.rollback()
                        return jsonify({
                            'error': 'media_unavailable',
                            'detail': ('Файл уже выгружен из кэша. '
                                       'Сначала загрузите его в чате.'),
                        }), 409
                    db.commit()
            else:
                db.commit()
            if recipient_msg is not None:
                _notify_webpush_message(recipient_msg.id)
            local_atts = _message_attachment_rows(db, msg.id)
            msg.media = local_atts
            msg.visible_text = msg.text or ''
            msg.visible_text_html = None
            _attach_forwards(db, [msg], user_id)
            return jsonify({'ok': True, 'id': msg.id, 'time': msg.time,
                            'text': msg.visible_text, 'text_html': None,
                            'date_label': _message_date_label(msg.created_at),
                            'messenger_name': SYNAPSE_MESSENGER,
                            'reply_to': reply_quote,
                            'fwd_from': msg.fwd_quote,
                            'forwarded': forward_source is not None,
                            'attachments': [
                                {'id': a.id, 'kind': a.kind, 'mime': a.mime,
                                 'name': a.original_name,
                                 'has_sticker_pack': bool(a.sticker_pack_key)}
                                for a in local_atts
                            ]})
        tg_handle, notif_handle = _reply_channel(m_handles)
        if tg_handle is None and notif_handle is None and requested_messenger:
            # Запрошен мессенджер, в котором ответ невозможен — пробуем глобально.
            tg_handle, notif_handle = _reply_channel(handles)
        if tg_handle is None and notif_handle is None:
            return jsonify({'error': 'no_reply_channel'}), 400
        if forward_source is not None and tg_handle is None:
            return jsonify({'error': 'target_not_telegram'}), 400

        # Reply-to: id нашей Messages, на которую отвечаем. Подходит, если в
        # том же чате (handle совпадает с тем, через который шлём).
        reply_target = None
        reply_kw_tg = {}
        target_handle_id = (tg_handle.id if tg_handle is not None
                            else notif_handle.id)
        reply_raw = request.form.get('reply_to')
        if reply_raw:
            try:
                reply_id = int(reply_raw)
            except (TypeError, ValueError):
                reply_id = None
            if reply_id:
                target = db.query(Messages).filter(
                    Messages.id == reply_id,
                    Messages.user_id == user_id).first()
                if target is not None and target.handle_id == target_handle_id:
                    reply_target = target
                    if (tg_handle is not None
                            and target.tg_message_id is not None):
                        reply_kw_tg['reply_to'] = target.tg_message_id

        # --- Telegram (Telethon) — текст или медиа, с reply_to ---
        if tg_handle is not None:
            forwarded_tg_id = None
            if forward_source is not None:
                source_chat_id = _msg_tg_chat_id(db, forward_source)
                if forward_source.tg_message_id is not None and source_chat_id is not None:
                    try:
                        forwarded_tg_id = telegram_bridge.forward_message(
                            source_chat_id, forward_source.tg_message_id,
                            tg_handle.tg_chat_id, user_id=user_id)
                    except Exception as exc:  # noqa: BLE001
                        return jsonify({'error': 'send_failed',
                                        'detail': str(exc)}), 502
                    if forwarded_tg_id is not None:
                        forwarded_local = db.query(Messages).filter(
                            Messages.user_id == user_id,
                            Messages.handle_id == tg_handle.id,
                            Messages.tg_message_id == forwarded_tg_id).first()
                        if forwarded_local is not None:
                            reply_target = forwarded_local
                        reply_kw_tg['reply_to'] = forwarded_tg_id
                    if not text and upload is None:
                        return jsonify({'ok': True, 'forwarded': True,
                                        'forward_tg_message_id': forwarded_tg_id})
                else:
                    forward_text = _forward_delivery_text(
                        db, user_id, forward_source)
                    if text:
                        forward_text = forward_text + '\n\n' + text
                    _opts_fw = {}
                    if silent:
                        _opts_fw['silent'] = True
                    if schedule_at is not None:
                        _opts_fw['schedule'] = schedule_at
                    try:
                        local_msg, sent_media = _telegram_send_fallback_copy(
                            db, user_id, tg_handle, forward_source,
                            forward_text, reply_kw_tg, _opts_fw,
                            local_reply_target=reply_target,
                            forward_meta=forward_meta)
                    except Exception as exc:  # noqa: BLE001
                        return jsonify({'error': 'send_failed',
                                        'detail': str(exc)}), 502
                    if local_msg is not None and client_send_key:
                        local_msg.notification_dedup_key = client_send_key
                    db.commit()
                    _attach_media(db, [local_msg])
                    _attach_forwards(db, [local_msg], user_id)
                    return jsonify({
                        'ok': True,
                        'id': local_msg.id,
                        'time': local_msg.time,
                        'date_label': _message_date_label(
                            local_msg.created_at),
                        'text': local_msg.visible_text,
                        'text_html': local_msg.visible_text_html,
                        'fwd_from': local_msg.fwd_quote,
                        'reply_to': None,
                        'forwarded': True,
                        'sent_media': sent_media,
                    })

            if upload is not None:
                data = upload.read()
                if not data:
                    return jsonify({'error': 'empty'}), 400
                upload_name = upload.filename or 'file'
                upload_mime = (upload.mimetype or '').lower()
                if voice_upload:
                    data, upload_name, upload_mime = _normalize_voice_upload(
                        data, upload_name or 'voice.webm', upload_mime)
                # parse_mode передаём только если был найден markdown —
                # см. комментарий ниже у send_message о совместимости со
                # старыми моками в тестах.
                _md_kw_f = ({'parse_mode': md_parse_mode}
                            if md_parse_mode else {})
                _opts_f = {}
                if silent:
                    _opts_f['silent'] = True
                if schedule_at is not None:
                    _opts_f['schedule'] = schedule_at
                # Медиа сначала надёжно сохраняем в локальный outbox и сразу
                # возвращаем браузеру. Медленный upload в Telegram продолжит
                # Telethon-loop — так AlwaysData/Safari не обрывают длинный
                # HTTP-запрос с ложным `Load failed`. Для schedule запись
                # удалится только после подтверждения Telegram.
                mime = upload_mime
                if voice_upload:
                    kind = 'voice'
                elif mime.startswith('image/'):
                    kind = 'image'
                elif mime.startswith('video/'):
                    kind = 'video'
                elif mime.startswith('audio/'):
                    kind = 'audio'
                else:
                    kind = 'file'
                placeholder = {
                    'image': '📷 Фото', 'video': '🎬 Видео',
                    'voice': '🎤 Голосовое сообщение',
                    'audio': '🎵 Аудио', 'file': '📎 Файл',
                }.get(kind, '📎 Вложение')
                now = datetime.now()
                msg = Messages(
                    sender='Вы',
                    text=md_plain or placeholder,
                    text_html=md_html,
                    messenger_name='Telegram',
                    time=now.strftime('%H:%M'),
                    user_id=user_id,
                    handle_id=tg_handle.id,
                    created_at=now,
                    outgoing=True,
                    tg_message_id=None,
                    reply_to_message_id=(
                        reply_target.id if reply_target else None),
                    notification_dedup_key=client_send_key,
                    delivery_status='sending',
                    delivery_error=None,
                    delivery_caption=text or '',
                    delivery_silent=bool(silent),
                    delivery_reply_to_tg_id=reply_kw_tg.get('reply_to'),
                    delivery_schedule_at=schedule_at,
                    delivery_started_at=now,
                )
                db.add(msg)
                db.flush()  # нужно msg.id для Attachment
                stored_path = _store_media_bytes(user_id, data)
                att = _attach_message_file(
                    db, user_id, msg.id, kind, mime, upload_name,
                    stored_path, len(data))
                db.commit()

                message_id = msg.id
                response_payload = {
                    'ok': True,
                    'queued': True,
                    'scheduled': schedule_at is not None,
                    'when': (schedule_at.isoformat()
                             if schedule_at is not None else None),
                    'media': True,
                    'id': msg.id,
                    'time': msg.time,
                    'date_label': _message_date_label(msg.created_at),
                    'text': md_plain or '',
                    'text_html': md_html,
                    'messenger_name': 'Telegram',
                    'delivery_status': 'sending',
                    'delivery_error': None,
                    'forwarded': forward_source is not None,
                    'attachments': [{
                        'id': att.id,
                        'kind': att.kind,
                        'mime': att.mime,
                        'name': att.original_name,
                        'has_sticker_pack': False,
                    }],
                }

                def _delivery_done(sent_id, error):
                    if schedule_at is not None:
                        _finish_scheduled_media_delivery(
                            message_id, sent_id, error)
                    else:
                        _finish_telegram_media_delivery(
                            message_id, sent_id, error)

                def _load_delivery_file(path=stored_path):
                    payload = _read_media_bytes(path)
                    if payload is None:
                        raise RuntimeError(
                            'Локальный файл отправки недоступен')
                    return payload

                try:
                    telegram_bridge.queue_file(
                        tg_handle.tg_chat_id, _load_delivery_file,
                        upload_name, text,
                        callback=_delivery_done,
                        **_md_kw_f, **reply_kw_tg, **_opts_f,
                        user_id=user_id, voice_note=voice_upload)
                except Exception as exc:  # noqa: BLE001
                    _finish_telegram_media_delivery(message_id, None, exc)
                    response_payload['delivery_status'] = 'failed'
                    response_payload['delivery_error'] = str(exc)[:1000]

                return jsonify(response_payload), 202

            # parse_mode передаём ТОЛЬКО когда нашли markdown — иначе
            # ломаются унаследованные моки в тестах, у которых сигнатура
            # старого API (text-only без kwarg). Поведение для plain-текста
            # не меняется.
            _md_kw = {'parse_mode': md_parse_mode} if md_parse_mode else {}
            _opts = {}
            if silent:
                _opts['silent'] = True
            if schedule_at is not None:
                _opts['schedule'] = schedule_at
            try:
                sent_id = telegram_bridge.send_message(
                    tg_handle.tg_chat_id, text,
                    **_md_kw, **reply_kw_tg, **_opts,
                    user_id=user_id)
            except Exception as exc:  # noqa: BLE001
                return jsonify({'error': 'send_failed', 'detail': str(exc)}), 502
            # Scheduled — echo придёт только в момент реальной отправки.
            if schedule_at is not None:
                return jsonify({'ok': True, 'scheduled': True,
                                'when': schedule_at.isoformat()})
            now = datetime.now()
            msg = Messages(
                sender='Вы',
                text=md_plain,
                text_html=md_html,
                messenger_name='Telegram',
                time=now.strftime('%H:%M'),
                user_id=user_id,
                handle_id=tg_handle.id,
                created_at=now,
                outgoing=True,
                tg_message_id=sent_id,
                reply_to_message_id=reply_target.id if reply_target else None,
                notification_dedup_key=client_send_key,
            )
            db.add(msg)
            db.commit()
            reply_quote = None
            if reply_target is not None:
                from data.matching import display_author
                rt_text = reply_target.text or ''
                reply_quote = {
                    'id': reply_target.id,
                    'author': ('Вы' if reply_target.outgoing
                               else display_author(reply_target.sender,
                                                    contact.display_name)),
                    'text': rt_text[:120] + ('…' if len(rt_text) > 120 else ''),
                }
            # Возвращаем text (plain без markdown-меток) и text_html для
            # оптимистичного рендера на фронте — иначе пользователь видит
            # звёздочки «**жирный**» 2-5 секунд, пока поллинг не подтянет
            # уже отформатированную версию.
            return jsonify({'ok': True, 'id': msg.id, 'time': msg.time,
                            'date_label': _message_date_label(msg.created_at),
                            'text': md_plain, 'text_html': md_html,
                            'reply_to': reply_quote,
                            'forwarded': forward_source is not None})

        # --- Notification reply через Android (только текст) ---
        if upload is not None:
            return jsonify({'error': 'media_not_supported'}), 400
        if not text:
            return jsonify({'error': 'empty'}), 400
        if client_send_key:
            existing_pr = (db.query(PendingReply)
                           .filter(PendingReply.user_id == user_id,
                                   PendingReply.client_send_key
                                   == client_send_key,
                                   PendingReply.status.in_(
                                       [STATUS_PENDING, STATUS_PICKED]))
                           .order_by(PendingReply.id.desc())
                           .first())
            if existing_pr is not None:
                return jsonify({
                    'ok': True,
                    'duplicate': True,
                    'queued': True,
                    'pending_id': existing_pr.id,
                    'via': 'notif',
                })
        pr = PendingReply(
            user_id=user_id,
            handle_id=notif_handle.id,
            text=text,
            package_name=_package_for_handle(notif_handle),
            sender_label=notif_handle.sender_raw,
            status=STATUS_PENDING,
            reply_to_message_id=reply_target.id if reply_target else None,
            client_send_key=client_send_key,
        )
        db.add(pr)
        db.commit()
        return jsonify({'ok': True, 'queued': True, 'pending_id': pr.id,
                        'via': 'notif'})

    @app.route('/contacts/<int:contact_id>/send-sticker', methods=['POST'])
    def contact_send_sticker(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data.stickers import SavedSticker
        from data import telegram_bridge

        db = get_db()
        user_id = session['user_id']
        try:
            sticker_id = int(request.form.get('sticker_id') or 0)
        except ValueError:
            sticker_id = 0
        sticker = (db.query(SavedSticker)
                   .filter(SavedSticker.id == sticker_id,
                           SavedSticker.user_id == user_id)
                   .first())
        if sticker is None:
            return jsonify({'error': 'sticker_not_found'}), 404
        raw = _read_media_bytes(sticker.stored_path)
        if raw is None:
            return jsonify({'error': 'sticker_file_missing'}), 404

        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id,
                           Contact.user_id == user_id)
                   .first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()
        requested_messenger = (
            request.form.get('messenger') or '').strip() or None
        m_handles = ([h for h in handles
                      if h.messenger_name == requested_messenger]
                     if requested_messenger else handles)
        synapse_handle = next((h for h in m_handles
                               if h.messenger_name == SYNAPSE_MESSENGER), None)
        if synapse_handle is not None and (
                requested_messenger == SYNAPSE_MESSENGER
                or not any(h.messenger_name != SYNAPSE_MESSENGER
                           for h in m_handles)):
            partner_id = _synapse_partner_id(synapse_handle)
            if partner_id is None or partner_id == user_id:
                return jsonify({'error': 'not_found'}), 404
            from data.direct import DirectMessage

            now = datetime.now()
            direct_msg = DirectMessage(
                sender_id=user_id, recipient_id=partner_id,
                text=_DM_PLACEHOLDER['sticker'], created_at=now)
            db.add(direct_msg)
            db.flush()
            _add_direct_attachment_ref(
                db, direct_msg.id, 'sticker', sticker.mime,
                sticker.original_name, sticker.stored_path, sticker.size,
                sticker_pack_key=sticker.pack_key,
                sticker_pack_title=sticker.pack_title,
                sticker_item_key=sticker.item_key)
            me = db.get(User, user_id)
            partner = db.get(User, partner_id)
            users_by_id = {user_id: me, partner_id: partner}
            msg = _mirror_direct_message_for_owner(
                db, direct_msg, user_id, users_by_id)
            recipient_msg = _mirror_direct_message_for_owner(
                db, direct_msg, partner_id, users_by_id)
            sticker.last_used_at = now
            db.commit()
            if recipient_msg is not None:
                _notify_webpush_message(recipient_msg.id)
            local_atts = _message_attachment_rows(db, msg.id)
            return jsonify({
                'ok': True,
                'id': msg.id,
                'time': msg.time,
                'date_label': _message_date_label(msg.created_at),
                'text': '',
                'text_html': None,
                'messenger_name': SYNAPSE_MESSENGER,
                'attachments': [
                    {'id': a.id, 'kind': a.kind, 'mime': a.mime,
                     'name': a.original_name,
                     'has_sticker_pack': bool(a.sticker_pack_key)}
                    for a in local_atts
                ],
            })

        tg_handle, notif_handle = _reply_channel(m_handles)
        if tg_handle is None and notif_handle is None and requested_messenger:
            tg_handle, notif_handle = _reply_channel(handles)
        if tg_handle is None:
            return jsonify({'error': 'target_not_telegram'}), 400
        try:
            sent_id = telegram_bridge.send_file(
                tg_handle.tg_chat_id, raw,
                sticker.original_name or 'sticker.webp', '',
                user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'send_failed', 'detail': str(exc)}), 502
        now = datetime.now()
        msg = Messages(
            sender='Вы',
            text=_DM_PLACEHOLDER['sticker'],
            messenger_name='Telegram',
            time=now.strftime('%H:%M'),
            user_id=user_id,
            handle_id=tg_handle.id,
            created_at=now,
            outgoing=True,
            tg_message_id=sent_id,
        )
        db.add(msg)
        db.flush()
        att = _attach_message_file(
            db, user_id, msg.id, 'sticker', sticker.mime,
            sticker.original_name, sticker.stored_path, sticker.size,
            sticker_pack_key=sticker.pack_key,
            sticker_pack_title=sticker.pack_title,
            sticker_item_key=sticker.item_key)
        sticker.last_used_at = now
        db.commit()
        return jsonify({
            'ok': True,
            'id': msg.id,
            'time': msg.time,
            'date_label': _message_date_label(msg.created_at),
            'text': '',
            'text_html': None,
            'messenger_name': 'Telegram',
            'attachments': [{'id': att.id, 'kind': att.kind,
                             'mime': att.mime, 'name': att.original_name,
                             'has_sticker_pack': bool(att.sticker_pack_key)}],
        })

    @app.route('/contacts/<int:contact_id>/avatars.json')
    def contact_avatars(contact_id):
        """Список фотографий профиля контакта (для просмотра всех аватарок
        в правой панели). Тянем id через Telethon iter_profile_photos —
        сами бинарники грузятся лениво через /contacts/<id>/avatar/<pid>."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handle = (db.query(MessengerHandle)
                  .filter(MessengerHandle.contact_id == contact.id,
                          MessengerHandle.tg_chat_id.isnot(None))
                  .first())
        # Без TG-handle или с выключенным мостом — отдаём хотя бы локальную
        # сохранённую аватарку, если она вообще была подгружена раньше.
        if handle is None or not telegram_bridge.is_configured():
            items = ([{'photo_id': 'local',
                       'url': f'/contacts/{contact.id}/photo'}]
                     if _media_rel_path_exists(contact.avatar_path) else [])
            return jsonify({'ok': True, 'items': items})
        try:
            photos = telegram_bridge.fetch_profile_photos(handle.tg_chat_id,
                                                          user_id=user_id)
        except Exception:  # noqa: BLE001
            items = ([{'photo_id': 'local',
                       'url': f'/contacts/{contact.id}/photo'}]
                     if _media_rel_path_exists(contact.avatar_path) else [])
            return jsonify({'ok': True, 'items': items})
        items = [{'photo_id': p['id'],
                  'url': f'/contacts/{contact.id}/avatar/{p["id"]}'}
                 for p in photos if p.get('id')]
        # Фолбэк: если TG ничего не отдал (например, фото скрыты), но
        # локально у нас фото есть — покажем хотя бы его.
        if not items and _media_rel_path_exists(contact.avatar_path):
            items = [{'photo_id': 'local',
                      'url': f'/contacts/{contact.id}/photo'}]
        return jsonify({'ok': True, 'items': items})

    @app.route('/contacts/<int:contact_id>/avatar/<int:photo_id>')
    def contact_avatar_one(contact_id, photo_id):
        """Отдаёт бинарник конкретной фотографии профиля. Кэшируется
        зашифрованной в _media_root()/<user>/tg_avatars/<contact_id>/."""
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.contacts import Contact, MessengerHandle
        from data.crypto import decrypt_bytes
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return 'Not Found', 404
        rel_path = f"{user_id}/tg_avatars/{contact.id}/{photo_id}.enc"
        cache_full = os.path.join(_media_root(), rel_path)
        if not os.path.exists(cache_full):
            handle = (db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id == contact.id,
                              MessengerHandle.tg_chat_id.isnot(None))
                      .first())
            if handle is None or not telegram_bridge.is_configured():
                return 'Not Found', 404
            try:
                data = telegram_bridge.download_profile_photo_by_id(
                    handle.tg_chat_id, photo_id, user_id=user_id)
            except Exception:  # noqa: BLE001
                return 'Not Found', 404
            if not data:
                return 'Not Found', 404
            _write_encrypted_media_path(cache_full, data)
        with open(cache_full, 'rb') as f:
            raw = decrypt_bytes(f.read())
        return Response(raw, mimetype='image/jpeg')

    @app.route('/contacts/<int:contact_id>/photo')
    def contact_photo(contact_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.contacts import Contact
        from data.crypto import decrypt_bytes
        db = get_db()
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id,
                           Contact.user_id == session['user_id']).first())
        if contact is None or not contact.avatar_path:
            return 'Not Found', 404
        full = os.path.join(_media_root(), contact.avatar_path)
        if not os.path.exists(full):
            contact.avatar_path = None
            _commit_best_effort(db, 'clear missing contact avatar')
            return 'Not Found', 404
        with open(full, 'rb') as f:
            raw = decrypt_bytes(f.read())
        rel = contact.avatar_path.replace('\\', '/')
        mime = ('image/png' if '/notification_avatars/' in f'/{rel}'
                else 'image/jpeg')
        return Response(raw, mimetype=mime)

    @app.route('/messages/<int:message_id>/author-photo')
    def message_author_photo(message_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.crypto import decrypt_bytes
        db = get_db()
        msg = (db.query(Messages)
               .filter(Messages.id == message_id,
                       Messages.user_id == session['user_id']).first())
        if msg is None or not getattr(msg, 'author_avatar_path', None):
            return 'Not Found', 404
        full = os.path.join(_media_root(), msg.author_avatar_path)
        if not os.path.exists(full):
            msg.author_avatar_path = None
            _commit_best_effort(db, 'clear missing author avatar')
            return 'Not Found', 404
        with open(full, 'rb') as f:
            raw = decrypt_bytes(f.read())
        return Response(raw, mimetype='image/png')

    @app.route('/contacts/<int:contact_id>/topics.json')
    def contact_topics(contact_id):
        """Извлечение главных тем чата через локальную Ollama-LLM.
        Возвращает {topics: [{title, start_id, message_ids, time, date}],
        status: 'ok'} или {status: 'no_ollama'|'no_messages'|'llm_error'}.

        Темы сохраняются в БД (`chat_topics`) — переживают перезапуск
        процесса и переход в другой чат. Перезапуск анализа происходит
        либо когда в чате появились новые сообщения (поменялся max_id),
        либо когда пользователь принудительно нажал «обновить»
        (force=1)."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import ollama as _ollama
        from data.chat_topics import (get_topics as _get_topics,
                                       replace_topics as _replace_topics,
                                       fingerprint as _topics_fp)
        from data.contacts import Contact, MessengerHandle
        db = get_db()
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id,
                           Contact.user_id == session['user_id']).first())
        if not contact:
            return jsonify({'status': 'not_found'}), 404

        # Параметры: force (принудительный полный пересчёт), cached_only
        # (тихая автозагрузка — не дёргать LLM), topic_id (для форум-тем).
        # limit больше не нужен — анализируем весь чат / только дельту.
        force = request.args.get('force') == '1'
        cached_only = request.args.get('cached_only') == '1'
        raw_topic_id = request.args.get('topic_id')
        try:
            topic_id = (int(raw_topic_id)
                        if raw_topic_id not in (None, '', 'null') else None)
        except ValueError:
            topic_id = None

        # Защитный потолок: на чате >5000 сообщений chunked-анализ может
        # уйти на 10+ минут. Берём только последние 5000 — старые темы
        # уже не релевантны, а пользователь хочет что-то получить за
        # разумное время.
        MAX_FULL_MSGS = 5000

        handle_ids = [h.id for h in db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()]
        if not handle_ids:
            return jsonify({'status': 'no_messages', 'topics': []})
        from sqlalchemy import func
        topic_filter_args = [Messages.handle_id.in_(handle_ids)]
        if topic_id is not None:
            topic_filter_args.append(Messages.tg_topic_id == topic_id)
        max_id = (db.query(func.max(Messages.id))
                  .filter(*topic_filter_args).scalar() or 0)

        # Кэш: если max_id не менялся — отдаём что есть.
        saved_fp, _ = _topics_fp(db, contact_id, topic_id)
        cache_valid = (saved_fp is not None and saved_fp == max_id)
        if cached_only or (cache_valid and not force):
            if saved_fp is None:
                return jsonify({'status': 'no_cached', 'topics': []})
            saved = _get_topics(db, contact_id, topic_id)
            return jsonify({
                'status': 'ok',
                'topics': _topics_with_time(db, saved, handle_ids),
                'cached': True,
                'stale': not cache_valid,
            })

        if not _ollama.is_available():
            return jsonify({
                'status': 'no_ollama',
                'detail': 'Установите Ollama (ollama.com) и выполните '
                          '"ollama pull qwen2.5:3b" в PowerShell.',
            })
        models = _ollama.installed_models()
        if models and _ollama.DEFAULT_MODEL not in models:
            return jsonify({
                'status': 'no_model',
                'detail': f'Модель {_ollama.DEFAULT_MODEL} не скачана. '
                          f'Выполни: ollama pull {_ollama.DEFAULT_MODEL}',
                'available_models': models,
            })

        # Режим: ПОЛНЫЙ (нет кэша или force=1) vs ИНКРЕМЕНТАЛЬНЫЙ
        # (кэш есть, max_id вырос — обрабатываем только дельту).
        incremental = (saved_fp is not None and not force
                       and saved_fp < max_id)

        if incremental:
            analyzed = _topics_incremental(
                db, contact_id, topic_id, handle_ids,
                topic_filter_args, saved_fp, max_id)
        else:
            analyzed = _topics_full(
                db, contact_id, topic_id, handle_ids,
                topic_filter_args, max_id, MAX_FULL_MSGS)

        if analyzed.get('error'):
            return jsonify(analyzed['error'])
        db.commit()
        saved = _get_topics(db, contact_id, topic_id)
        return jsonify({
            'status': 'ok',
            'topics': _topics_with_time(db, saved, handle_ids),
            'analyzed': analyzed.get('count', 0),
            'cached': False,
            'incremental': incremental,
        })

    @app.route('/contacts/<int:contact_id>/forum-topics.json')
    def contact_forum_topics(contact_id):
        """Список тем Telegram-форума с превью и счётчиком непрочитанных.

        Каждая тема ведёт себя как отдельный чат: есть last-message
        preview, время последнего сообщения и unread-бэйдж. Состояние
        прочтения хранится в `topic_read_state` по паре (handle, topic).
        """
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from sqlalchemy import or_
        from data.contacts import Contact, MessengerHandle
        from data.topic_reads import get_read_map
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id,
                           Contact.user_id == user_id).first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        tg_handle = next((h for h in db.query(MessengerHandle)
                          .filter(MessengerHandle.contact_id == contact.id).all()
                          if h.messenger_name == 'Telegram'
                          and h.tg_chat_id is not None), None)
        if tg_handle is None:
            return jsonify({'status': 'not_telegram', 'topics': []})

        # Live-список с сервера (через кэш в bridge на 60 сек) — даёт
        # точные названия и порядок. Параллельно мы всё равно дочитываем
        # из БД unread/preview.
        try:
            live = telegram_bridge.fetch_forum_topics(tg_handle.tg_chat_id,
                                                      user_id=user_id)
        except Exception:  # noqa: BLE001
            live = []
        if live and not tg_handle.tg_is_forum:
            tg_handle.tg_is_forum = True
            db.commit()

        handle_ids = [h.id for h in db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id == contact.id).all()]
        if not handle_ids:
            return jsonify({'status': 'ok', 'topics': []})

        # Карта state прочтения per topic (для unread).
        read_map = get_read_map(db, handle_ids)

        # Если live из MTProto пустой — собираем topic_ids из БД.
        if live:
            topic_ids = [t['id'] for t in live]
            titles_by_id = {t['id']: t['title'] for t in live}
        else:
            topic_ids = [tid for (tid,) in (
                db.query(Messages.tg_topic_id)
                .filter(Messages.handle_id.in_(handle_ids),
                        Messages.tg_topic_id.isnot(None))
                .distinct().all()) if tid]
            heads = {m.tg_message_id: m.tg_topic_title for m in (
                db.query(Messages)
                .filter(Messages.handle_id.in_(handle_ids),
                        Messages.tg_message_id.in_(topic_ids),
                        Messages.tg_topic_title.isnot(None)).all())}
            titles_by_id = {tid: (heads.get(tid) or f'Тема #{tid}')
                            for tid in topic_ids}
        if not topic_ids:
            return jsonify({'status': 'ok',
                            'is_forum': bool(tg_handle.tg_is_forum),
                            'topics': []})

        # Last-message per topic. SQLite-friendly: одним запросом тянем
        # все сообщения с этим topic_id и в Python группируем — обычно
        # форум-каналы небольшие (десятки-сотни сообщений на тему).
        msgs_by_topic = {}
        for m in (db.query(Messages)
                  .filter(Messages.handle_id.in_(handle_ids),
                          Messages.tg_topic_id.in_(topic_ids))
                  .order_by(Messages.created_at.asc().nullsfirst(),
                            Messages.id.asc())
                  .all()):
            msgs_by_topic.setdefault(m.tg_topic_id, []).append(m)

        topics_out = []
        for tid in topic_ids:
            items = msgs_by_topic.get(tid, [])
            last = items[-1] if items else None
            last_read = read_map.get((tg_handle.id, tid))
            unread = 0
            if items:
                for m in items:
                    if m.outgoing:
                        continue
                    if last_read is None or (m.created_at
                                              and m.created_at > last_read):
                        unread += 1
            preview = ''
            if last is not None and not last.deleted_at and last.text:
                preview = last.text[:80]
            topics_out.append({
                'id': tid,
                'title': titles_by_id.get(tid, f'Тема #{tid}'),
                'top_message_id': tid,
                'unread': unread,
                'last_preview': preview,
                'last_time': last.time if last else '',
                'last_at': (last.created_at.isoformat() if last
                            and last.created_at else None),
                'message_count': len(items),
            })

        # Сортируем по свежести последнего сообщения (новые темы сверху).
        topics_out.sort(
            key=lambda t: t['last_at'] or '', reverse=True)
        return jsonify({'status': 'ok',
                        'is_forum': bool(tg_handle.tg_is_forum),
                        'topics': topics_out})

    @app.route('/contacts/<int:contact_id>/typing.json')
    def contact_typing(contact_id):
        if not session.get('user_id'):
            return jsonify({'typing': False})
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id,
                           Contact.user_id == session['user_id']).first())
        if not contact:
            return jsonify({'typing': False})
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()
        available = []
        for handle in handles:
            if handle.messenger_name not in available:
                available.append(handle.messenger_name)
        current_m = _pick_messenger(available, request.args.get('m'))
        active_handles = [handle for handle in handles
                          if handle.messenger_name == current_m]
        tg_handle = _telegram_reply_handle(active_handles)
        presence = _presence_for_handles(
            db, session['user_id'], active_handles,
            refresh_telegram=True)
        typing = False
        authors = []
        text = ''
        if tg_handle is not None:
            try:
                status = telegram_bridge.typing_status(tg_handle.tg_chat_id)
                typing = bool(status.get('typing'))
                authors = [a for a in status.get('authors', []) if a]
            except Exception:  # noqa: BLE001
                typing = False
                authors = []
        is_group = (tg_handle is not None and tg_handle.tg_chat_type == 'group')
        if typing:
            if is_group and authors:
                if len(authors) == 1:
                    text = f'{authors[0]} печатает…'
                elif len(authors) == 2:
                    text = f'{authors[0]} и {authors[1]} печатают…'
                else:
                    text = f'{authors[0]} и ещё {len(authors) - 1} печатают…'
            else:
                text = 'печатает…'
        return jsonify({'typing': bool(typing), 'authors': authors,
                        'text': text, 'presence': presence})

    @app.route('/contacts/<int:contact_id>/members.json')
    def contact_members(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id,
                           Contact.user_id == user_id).first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()
        tg_handle = _telegram_reply_handle(handles)
        if tg_handle is None or tg_handle.tg_chat_type != 'group':
            return jsonify({'is_group': any(_is_group_handle(h)
                                            for h in handles),
                            'members': []})
        try:
            members = telegram_bridge.get_participants(tg_handle.tg_chat_id,
                                                       user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'unavailable', 'detail': str(exc)}), 502
        return jsonify({'is_group': True, 'members': members})

    @app.route('/contacts/manage')
    def contacts_manage():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MessengerHandle
        from sqlalchemy import func
        db = get_db()
        user_id = session['user_id']
        all_contacts = (db.query(Contact).filter(Contact.user_id == user_id)
                        .order_by(Contact.display_name.asc()).all())
        for c in all_contacts:
            _avatar_for(c)
        handles = (db.query(MessengerHandle).filter(MessengerHandle.user_id == user_id)
                   .order_by(MessengerHandle.messenger_name.asc()).all())
        contact_handles = {c.id: [] for c in all_contacts}
        for h in handles:
            contact_handles.setdefault(h.contact_id, []).append(h)

        # meta для каждого контакта: счётчик сообщений, последнее, набор мессенджеров.
        # Нужно, чтобы перед merge/delete видеть, какой контакт «толще».
        for c in all_contacts:
            hids = [h.id for h in contact_handles.get(c.id, [])]
            if not hids:
                c.msg_count = 0
                c.last_at = None
                c.messengers = []
                continue
            c.msg_count = (db.query(func.count(Messages.id))
                           .filter(Messages.handle_id.in_(hids)).scalar() or 0)
            last = (db.query(Messages.created_at)
                    .filter(Messages.handle_id.in_(hids))
                    .order_by(Messages.created_at.desc().nullslast(),
                              Messages.id.desc())
                    .first())
            c.last_at = last[0] if last else None
            seen = []
            for h in contact_handles[c.id]:
                if h.messenger_name not in seen:
                    seen.append(h.messenger_name)
            c.messengers = seen

        return render_template('contacts_manage.html',
                               all_contacts=all_contacts,
                               contact_handles=contact_handles)

    @app.route('/contacts/<int:contact_id>/pin', methods=['POST'])
    def contact_pin(contact_id):
        """Закрепить или открепить контакт в списке слева."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact
        db = get_db()
        contact = db.query(Contact).filter(
            Contact.id == contact_id,
            Contact.user_id == session['user_id']).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        if contact.pinned_at is None:
            contact.pinned_at = datetime.now()
        else:
            contact.pinned_at = None
        db.commit()
        return jsonify({'ok': True, 'pinned': bool(contact.pinned_at)})

    @app.route('/contacts/<int:contact_id>/block', methods=['POST'])
    def contact_block(contact_id):
        """Блокировка: ставим Contact.blocked_at (новые входящие молча
        выкидываются в record_message), и параллельно пробуем заблокировать
        пользователя в самом Telegram через BlockRequest — чтобы новые
        сообщения не приходили и в TG-клиент тоже. Toggle: повторный
        запрос снимает блок и тут, и в TG. Если TG-блок не удался (например,
        мост не настроен) — локальное состояние всё равно меняется."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        contact = db.query(Contact).filter(
            Contact.id == contact_id,
            Contact.user_id == session['user_id']).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        new_block = contact.blocked_at is None
        # Найдём личный TG-handle и попробуем синхронизировать состояние
        # блокировки в самом Telegram. Это «best effort» — если упадёт
        # (нет авторизации, не настроен мост, пользователь — group),
        # просто пометим tg_synced=False в ответе.
        tg_handle = (db.query(MessengerHandle)
                     .filter(MessengerHandle.contact_id == contact.id,
                             MessengerHandle.tg_chat_id.isnot(None),
                             MessengerHandle.tg_chat_type == 'private')
                     .first())
        tg_synced = None
        if tg_handle is not None and telegram_bridge.is_configured():
            try:
                telegram_bridge.set_block(tg_handle.tg_chat_id, new_block,
                                          user_id=user_id)
                tg_synced = True
            except Exception:  # noqa: BLE001
                tg_synced = False
        # Локальное состояние меняем независимо от успеха TG-вызова,
        # чтобы хотя бы у нас работало.
        contact.blocked_at = datetime.now() if new_block else None
        db.commit()
        return jsonify({'ok': True,
                        'blocked': contact.blocked_at is not None,
                        'tg_synced': tg_synced})

    @app.route('/contacts/<int:contact_id>/mute', methods=['POST'])
    def contact_mute(contact_id):
        """Беззвучный режим локально и, если это Telegram, в самом TG."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id,
            Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        new_muted = not bool(contact.muted)
        tg_handles = (db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id == contact.id,
                              MessengerHandle.user_id == user_id,
                              MessengerHandle.messenger_name == 'Telegram',
                              MessengerHandle.tg_chat_id.isnot(None))
                      .all())
        tg_synced = None
        if tg_handles and telegram_bridge.is_configured():
            tg_synced = True
            seen = set()
            for handle in tg_handles:
                if handle.tg_chat_id in seen:
                    continue
                seen.add(handle.tg_chat_id)
                try:
                    telegram_bridge.set_mute(handle.tg_chat_id, new_muted,
                                             user_id=user_id)
                except Exception:  # noqa: BLE001
                    tg_synced = False
        contact.muted = new_muted
        db.commit()
        return jsonify({'ok': True,
                        'muted': bool(contact.muted),
                        'tg_synced': tg_synced})

    @app.route('/contacts/<int:contact_id>/archive', methods=['POST'])
    def contact_archive(contact_id):
        """Переместить контакт в архив или вернуть его в общий список."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id,
            Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        raw = request.form.get('archived')
        if raw is None:
            new_archived = not bool(contact.archived)
        else:
            new_archived = _form_bool(raw)
        tg_handles = (db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id == contact.id,
                              MessengerHandle.user_id == user_id,
                              MessengerHandle.messenger_name == 'Telegram',
                              MessengerHandle.tg_chat_id.isnot(None))
                      .all())
        tg_synced = None
        if tg_handles and telegram_bridge.is_configured():
            tg_synced = True
            seen = set()
            for handle in tg_handles:
                if handle.tg_chat_id in seen:
                    continue
                seen.add(handle.tg_chat_id)
                try:
                    telegram_bridge.set_archive(handle.tg_chat_id,
                                                new_archived,
                                                user_id=user_id)
                except Exception:  # noqa: BLE001
                    tg_synced = False
        contact.archived = new_archived
        db.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True,
                            'archived': bool(contact.archived),
                            'tg_synced': tg_synced})
        return redirect('/contacts?archived=1' if contact.archived else '/contacts')

    @app.route('/contacts/<int:contact_id>/rename', methods=['POST'])
    def contact_rename(contact_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return 'Not Found', 404
        new_name = request.form.get('display_name', '').strip()
        if new_name:
            contact.display_name = new_name
            db.commit()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'ok': True, 'display_name': contact.display_name})
        return redirect('/contacts/manage')

    @app.route('/contacts/<int:contact_id>/delete', methods=['POST'])
    def contact_delete(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handle_ids = [h.id for h in db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id == contact.id).all()]
        if handle_ids:
            db.query(Messages).filter(Messages.handle_id.in_(handle_ids)).delete(
                synchronize_session=False)
        db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).delete(
            synchronize_session=False)
        db.delete(contact)
        db.commit()
        return jsonify({'ok': True})

    # --- Профиль контакта (правая выезжающая панель в стиле Telegram) ---

    # Маппинг внутреннего Attachment.kind в «корзины», которые видит UI.
    # Стикеры показываем рядом с фото — это самое близкое по смыслу.
    # 'link' — псевдо-корзина: ссылки достаются парсингом текста сообщений,
    # отдельной таблицы под них нет.
    _MEDIA_BUCKETS = {
        'photo': ('image', 'sticker'),
        'video': ('video', 'video_note'),
        'voice': ('voice',),
        'audio': ('audio',),
        'file':  ('file',),
    }

    # Регэксп для извлечения URL из текста сообщений. Сознательно простой —
    # ловит http(s)-схемы, режет на пробелах/кавычках/угловых скобках.
    import re as _re_url
    _URL_RE = _re_url.compile(r"https?://[^\s<>'\"]+", _re_url.IGNORECASE)
    # Чтобы не разогнаться на больших чатах, ссылки ищем только в последних
    # _LINK_SCAN_LIMIT сообщениях. Подсчёт получается приблизительный —
    # этого достаточно для UI «сколько ссылок было в чате».
    _LINK_SCAN_LIMIT = 500

    def _aggregate_media_counts(db, user_id, contact_id):
        """Возвращает dict bucket→count для всех вложений контакта,
        плюс ключ 'link' с приближённым числом ссылок (см.
        _count_message_links). Подсчёт ссылок ограничен последними
        _LINK_SCAN_LIMIT сообщениями, чтобы не разогнаться на больших
        чатах — для UI важно «много / мало / нет», а не точное число."""
        from sqlalchemy import func
        from data.attachments import Attachment
        from data.contacts import MessengerHandle
        rows = (db.query(Attachment.kind, func.count(Attachment.id))
                .join(Messages, Attachment.message_id == Messages.id)
                .join(MessengerHandle,
                      Messages.handle_id == MessengerHandle.id)
                .filter(MessengerHandle.contact_id == contact_id,
                        Attachment.user_id == user_id)
                .group_by(Attachment.kind).all())
        out = {b: 0 for b in _MEDIA_BUCKETS}
        for kind, cnt in rows:
            for bucket, kinds in _MEDIA_BUCKETS.items():
                if kind in kinds:
                    out[bucket] += int(cnt)
                    break
        out['link'] = _count_message_links(db, user_id, contact_id)
        return out

    def _count_message_links(db, user_id, contact_id):
        """Сколько ссылок в последних _LINK_SCAN_LIMIT сообщениях чата.
        Текст сообщений зашифрован TypeDecorator'ом — фильтр в SQL по
        '%http%' не сработает, поэтому загружаем и парсим regex'ом."""
        from data.contacts import MessengerHandle
        msgs = (db.query(Messages.text)
                .join(MessengerHandle,
                      Messages.handle_id == MessengerHandle.id)
                .filter(MessengerHandle.contact_id == contact_id,
                        Messages.user_id == user_id,
                        Messages.text.isnot(None))
                .order_by(Messages.id.desc())
                .limit(_LINK_SCAN_LIMIT).all())
        total = 0
        for (txt,) in msgs:
            if not txt:
                continue
            total += len(_URL_RE.findall(txt))
        return total

    def _collect_message_links(db, user_id, contact_id, limit, offset):
        """Возвращает список найденных ссылок (URL + превью текста + id
        исходного сообщения), отсортированный от свежих к старым.
        Аналогично _count_message_links — пробегаем по последним
        _LINK_SCAN_LIMIT сообщениям. limit/offset режут уже найденные."""
        from data.contacts import MessengerHandle
        msgs = (db.query(Messages)
                .join(MessengerHandle,
                      Messages.handle_id == MessengerHandle.id)
                .filter(MessengerHandle.contact_id == contact_id,
                        Messages.user_id == user_id,
                        Messages.text.isnot(None))
                .order_by(Messages.id.desc())
                .limit(_LINK_SCAN_LIMIT).all())
        items = []
        for m in msgs:
            urls = _URL_RE.findall(m.text or '')
            for url in urls:
                items.append({
                    'url': url,
                    'text_preview': (m.text or '')[:200],
                    'message_id': m.id,
                    'created_at': (m.created_at.isoformat()
                                   if m.created_at else None),
                })
        return items[offset:offset + limit]

    @app.route('/contacts/<int:contact_id>/profile.json')
    def contact_profile(contact_id):
        """Сводка для правой панели: имя, аватар, mute/pin, мессенджеры,
        счётчики медиа по типам. Сами медиа отдаются отдельно через
        /contacts/<id>/media.json — здесь только числа для секций."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()
        _avatar_for(contact)
        _mark_contact_creator_from_handles(contact, handles,
                                           _creator_user_ids(db))
        msgs_count = (db.query(Messages)
                      .filter(Messages.handle_id.in_([h.id for h in handles]
                                                     or [0]))
                      .count()) if handles else 0
        is_group = any(_is_group_or_channel_handle(h) for h in handles)
        return jsonify({
            'ok': True,
            'contact': {
                'id': contact.id,
                'display_name': contact.display_name,
                'avatar_url': (f'/contacts/{contact.id}/photo'
                               if contact.avatar_path else None),
                'pinned': contact.pinned_at is not None,
                'muted': bool(contact.muted),
                'archived': bool(contact.archived),
                'blocked': contact.blocked_at is not None,
                'messages_count': int(msgs_count),
                'is_creator': bool(getattr(contact, 'is_creator', False)),
                'creator_title': getattr(contact, 'creator_title', ''),
            },
            'messengers': [{
                'name': h.messenger_name,
                'sender_raw': h.sender_raw,
                'tg_chat_id': h.tg_chat_id,
                'tg_chat_type': getattr(h, 'tg_chat_type', None),
            } for h in handles],
            'is_group': is_group,
            'media_counts': _aggregate_media_counts(
                db, user_id, contact.id),
        })

    @app.route('/contacts/<int:contact_id>/media.json')
    def contact_media(contact_id):
        """Список вложений выбранного типа для галереи. ?kind=photo|video|
        voice|audio|file, плюс limit/offset для подгрузки. Возвращаем
        самые свежие сверху."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.attachments import Attachment
        from data.contacts import Contact, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        bucket = (request.args.get('kind') or 'photo').lower()
        try:
            limit = max(1, min(int(request.args.get('limit') or 60), 200))
            offset = max(0, int(request.args.get('offset') or 0))
        except ValueError:
            limit, offset = 60, 0
        # «Ссылки» — отдельный путь: достаются парсингом текста сообщений,
        # а не из таблицы attachments.
        if bucket == 'link':
            items = _collect_message_links(db, user_id, contact.id,
                                            limit, offset)
            return jsonify({'ok': True, 'kind': 'link', 'items': items})
        kinds = _MEDIA_BUCKETS.get(bucket)
        if not kinds:
            return jsonify({'error': 'bad_kind'}), 400
        rows = (db.query(Attachment, Messages)
                .join(Messages, Attachment.message_id == Messages.id)
                .join(MessengerHandle,
                      Messages.handle_id == MessengerHandle.id)
                .filter(MessengerHandle.contact_id == contact.id,
                        Attachment.user_id == user_id,
                        Attachment.kind.in_(kinds))
                .order_by(Attachment.id.desc())
                .limit(limit).offset(offset).all())
        items = []
        for attachment, message in rows:
            availability = _attachment_availability(
                attachment, message)
            items.append({
                'id': attachment.id,
                'kind': attachment.kind,
                'mime': attachment.mime,
                'name': attachment.original_name,
                'size': attachment.size,
                'created_at': (attachment.created_at.isoformat()
                               if attachment.created_at else None),
                'message_id': attachment.message_id,
                'availability': availability,
                'url': (f'/attachments/{attachment.id}'
                        if availability == 'local' else None),
                'restore_url': (f'/attachments/{attachment.id}/restore'
                                if availability == 'remote' else None),
            })
        return jsonify({
            'ok': True, 'kind': bucket,
            'items': items,
        })

    @app.route('/contacts/by-tg.json')
    def contact_by_tg():
        """Найти существующий Contact по Telegram-id (без создания).
        Параметр ?tg_chat_id=… (старое имя ?tg_user_id= тоже поддерживаем
        для обратной совместимости). Возвращает contact_id или null."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import MessengerHandle
        try:
            tg_chat_id = int(request.args.get('tg_chat_id')
                              or request.args.get('tg_user_id')
                              or 0)
        except ValueError:
            tg_chat_id = 0
        if not tg_chat_id:
            return jsonify({'error': 'bad_request'}), 400
        db = get_db()
        handle = (db.query(MessengerHandle)
                  .filter(MessengerHandle.user_id == session['user_id'],
                          MessengerHandle.tg_chat_id == tg_chat_id)
                  .first())
        return jsonify({
            'ok': True,
            'contact_id': handle.contact_id if handle else None,
        })

    @app.route('/contacts/telegram-author/<int:tg_chat_id>/photo')
    def telegram_author_photo(tg_chat_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data import telegram_bridge
        if not telegram_bridge.is_configured():
            return 'Not Found', 404
        try:
            raw = telegram_bridge.download_profile_photo(
                tg_chat_id, user_id=session['user_id'])
        except Exception:  # noqa: BLE001
            raw = None
        if not raw:
            return 'Not Found', 404
        return Response(raw, mimetype='image/jpeg')

    @app.route('/contacts/from-tg/<int:tg_chat_id>')
    def contact_from_tg_redirect(tg_chat_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import MessengerHandle, find_or_create_handle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        handle = (db.query(MessengerHandle)
                  .filter(MessengerHandle.user_id == user_id,
                          MessengerHandle.tg_chat_id == tg_chat_id)
                  .first())
        if handle is not None:
            return redirect(f'/contacts/{handle.contact_id}?m=Telegram')
        if not telegram_bridge.is_configured():
            return redirect('/contacts')
        try:
            info = telegram_bridge.resolve_entity_info(tg_chat_id,
                                                       user_id=user_id)
        except Exception:  # noqa: BLE001
            return redirect('/contacts')
        name = (info.get('display_name') or info.get('username')
                or str(tg_chat_id))
        kind = info.get('kind') or 'private'
        norm_chat_id = int(info.get('chat_id') or tg_chat_id)
        handle = find_or_create_handle(db, user_id, 'Telegram', name,
                                       tg_chat_id=norm_chat_id,
                                       tg_chat_type=kind)
        db.commit()
        return redirect(f'/contacts/{handle.contact_id}?m=Telegram')

    @app.route('/contacts/from-tg.json', methods=['POST'])
    def contact_from_tg():
        """Найти или создать локальный Contact для произвольной
        Telegram-сущности (пользователь, группа, канал) по peer-id.
        Нужно для клика по участнику группы / общей группе в правой
        панели — у нас может ещё не быть с ними переписки, а пользователь
        хочет открыть чат внутри Synapse. Резолвим имя через Telethon
        get_entity, создаём Contact + MessengerHandle.
        Body: tg_chat_id (int) или tg_username (@username)."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import (Contact, MessengerHandle,
                                    find_or_create_handle)
        from data import telegram_bridge
        tg_username = (request.form.get('tg_username') or '').strip()
        if tg_username.startswith('@'):
            tg_username = tg_username[1:]
        try:
            tg_chat_id = int(request.form.get('tg_chat_id') or 0)
        except ValueError:
            tg_chat_id = 0
        if not tg_chat_id and not tg_username:
            return jsonify({'error': 'bad_request'}), 400
        db = get_db()
        user_id = session['user_id']
        # 1. Уже есть handle с этим id?
        handle = (db.query(MessengerHandle)
                  .filter(MessengerHandle.user_id == user_id,
                          MessengerHandle.tg_chat_id == tg_chat_id)
                  .first())
        if handle is not None:
            return jsonify({'ok': True, 'contact_id': handle.contact_id,
                            'created': False})
        # 2. Резолвим через Telethon, чтобы получить имя и тип.
        if not telegram_bridge.is_configured():
            return jsonify({'error': 'telegram_not_configured'}), 502
        try:
            if tg_username:
                info = telegram_bridge.resolve_username_info(
                    tg_username, user_id=user_id)
            else:
                info = telegram_bridge.resolve_entity_info(tg_chat_id,
                                                           user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'resolve_failed',
                            'detail': str(exc)}), 502
        # 3. У сущности может быть «нормализованный» id (например, если
        # передали peer-id с -100… префиксом). Если так — повторим поиск
        # уже с ним, чтобы не плодить дубликат.
        norm_chat_id = int(info.get('chat_id') or tg_chat_id)
        if norm_chat_id != tg_chat_id:
            handle = (db.query(MessengerHandle)
                      .filter(MessengerHandle.user_id == user_id,
                              MessengerHandle.tg_chat_id == norm_chat_id)
                      .first())
            if handle is not None:
                return jsonify({'ok': True, 'contact_id': handle.contact_id,
                                'created': False})
        # 4. Создаём через find_or_create_handle — он же поймает дубль
        # по (user_id, 'Telegram', title), если такой Contact уже есть
        # без проставленного tg_chat_id.
        title = info.get('title') or 'Без имени'
        kind = info.get('kind') or 'private'
        handle = find_or_create_handle(
            db, user_id, 'Telegram', title,
            tg_chat_id=norm_chat_id, tg_chat_type=kind)
        db.commit()
        return jsonify({'ok': True, 'contact_id': handle.contact_id,
                        'created': True})

    @app.route('/contacts/<int:contact_id>/tg_extra.json')
    def contact_tg_extra(contact_id):
        """Расширенные данные TG-контакта: bio / телефон / @username +
        список общих групп. Эти запросы ходят в Telegram через Telethon,
        поэтому отдаём их отдельным эндпоинтом — UI рисует базовый профиль
        мгновенно, а потом подтягивает эти данные. Если у контакта нет
        личного TG-handle (только Android-уведомления или группа) —
        возвращаем available=False."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        contact = db.query(Contact).filter(
            Contact.id == contact_id, Contact.user_id == user_id).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        # Берём первый личный TG-handle (tg_chat_type='private' и tg_chat_id есть).
        handle = (db.query(MessengerHandle)
                  .filter(MessengerHandle.contact_id == contact.id,
                          MessengerHandle.tg_chat_id.isnot(None),
                          MessengerHandle.tg_chat_type == 'private')
                  .first())
        if handle is None:
            return jsonify({'ok': True, 'available': False,
                            'reason': 'no_tg_user_handle'})
        if not telegram_bridge.is_configured():
            return jsonify({'ok': True, 'available': False,
                            'reason': 'telegram_not_configured'})

        chat_id = handle.tg_chat_id
        result = {
            'ok': True, 'available': True,
            'user_info': None, 'common_chats': [], 'errors': [],
        }
        try:
            result['user_info'] = telegram_bridge.get_user_info(
                chat_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            result['errors'].append({'where': 'user_info',
                                     'detail': str(exc)})
        try:
            result['common_chats'] = telegram_bridge.get_common_chats(
                chat_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            result['errors'].append({'where': 'common_chats',
                                     'detail': str(exc)})
        return jsonify(result)

    def _msg_tg_chat_id(db, msg):
        """tg_chat_id чата, которому принадлежит сообщение, либо None."""
        return _message_tg_chat_id(db, msg)

    @app.route('/messages/<int:message_id>/forward', methods=['POST'])
    def message_forward(message_id):
        """Переслать Telegram-сообщение в другой Telegram-чат через Telethon.
        Пересылка только Telegram → Telegram: для MAX/VK через шторку нет
        нативного forward, и эмуляция «скопируй текст руками» сюда не пишем."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        if msg.tg_message_id is None:
            return jsonify({'error': 'not_telegram'}), 400
        source_chat_id = _msg_tg_chat_id(db, msg)
        if source_chat_id is None:
            return jsonify({'error': 'no_source_chat'}), 400

        try:
            target_cid = int(request.form.get('target_contact_id') or 0)
        except ValueError:
            target_cid = 0
        if not target_cid:
            return jsonify({'error': 'bad_target'}), 400
        target = db.query(Contact).filter(
            Contact.id == target_cid, Contact.user_id == user_id).first()
        if not target:
            return jsonify({'error': 'target_not_found'}), 404
        target_handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == target.id).all()
        tg_target = _telegram_reply_handle(target_handles)
        if tg_target is None:
            return jsonify({'error': 'target_not_telegram'}), 400

        try:
            telegram_bridge.forward_message(
                source_chat_id, msg.tg_message_id, tg_target.tg_chat_id,
                user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'send_failed',
                            'detail': str(exc)}), 502
        # Сама запись в нашу БД для target прилетит обычным NewMessage-эхом
        # из Telethon как наше исходящее.
        return jsonify({'ok': True})

    @app.route('/messages/forward-bulk', methods=['POST'])
    def messages_forward_bulk():
        """Массовая пересылка нескольких Telegram-сообщений в один чат.
        Body: ids=<csv message_ids>, target_contact_id=<id>. Все сообщения
        должны быть из ОДНОГО source-чата (Telegram forward_messages
        требует одного peer-источника)."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        raw_ids = (request.form.get('ids') or '').strip()
        if not raw_ids:
            return jsonify({'error': 'no_ids'}), 400
        try:
            msg_ids = [int(x) for x in raw_ids.split(',') if x.strip()]
        except ValueError:
            return jsonify({'error': 'bad_ids'}), 400
        if not msg_ids:
            return jsonify({'error': 'no_ids'}), 400
        msgs = (db.query(Messages)
                .filter(Messages.id.in_(msg_ids),
                        Messages.user_id == user_id)
                .all())
        if len(msgs) != len(msg_ids):
            return jsonify({'error': 'some_not_found'}), 404
        # Все должны быть из одного TG-чата и иметь tg_message_id.
        sources = set()
        tg_ids = []
        for m in msgs:
            if m.tg_message_id is None:
                return jsonify({'error': 'not_telegram',
                                'id': m.id}), 400
            src = _msg_tg_chat_id(db, m)
            if src is None:
                return jsonify({'error': 'no_source_chat',
                                'id': m.id}), 400
            sources.add(src)
            tg_ids.append(m.tg_message_id)
        if len(sources) != 1:
            return jsonify({'error': 'mixed_sources'}), 400
        source_chat_id = sources.pop()
        try:
            target_cid = int(request.form.get('target_contact_id') or 0)
        except ValueError:
            target_cid = 0
        if not target_cid:
            return jsonify({'error': 'bad_target'}), 400
        target = (db.query(Contact)
                  .filter(Contact.id == target_cid,
                          Contact.user_id == user_id).first())
        if not target:
            return jsonify({'error': 'target_not_found'}), 404
        target_handles = (db.query(MessengerHandle)
                          .filter(MessengerHandle.contact_id == target.id)
                          .all())
        tg_target = _telegram_reply_handle(target_handles)
        if tg_target is None:
            return jsonify({'error': 'target_not_telegram'}), 400
        try:
            sent_ids = telegram_bridge.forward_messages_bulk(
                source_chat_id, tg_ids, tg_target.tg_chat_id,
                user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'send_failed',
                            'detail': str(exc)}), 502
        return jsonify({'ok': True, 'count': len(sent_ids),
                        'sent_ids': sent_ids})

    @app.route('/contacts/telegram.json')
    def contacts_telegram_json():
        """Только Telegram-контакты (с tg_chat_id) — для модалки пересылки."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        contacts = db.query(Contact).filter(Contact.user_id == user_id).all()
        contacts = _filter_discussion_contacts(db, contacts)
        ids = [c.id for c in contacts]
        handles_by_contact = {}
        if ids:
            for h in (db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id.in_(ids),
                              MessengerHandle.messenger_name == 'Telegram',
                              MessengerHandle.tg_chat_id.isnot(None)).all()):
                handles_by_contact.setdefault(h.contact_id, []).append(h)
        out = []
        for c in contacts:
            if _telegram_reply_handle(handles_by_contact.get(c.id, [])) is None:
                continue
            _avatar_for(c)
            out.append({
                'id': c.id,
                'display_name': c.display_name,
                'initial': c.initial,
                'avatar_color': c.avatar_color,
                'avatar_url': c.avatar_url,
            })
        out.sort(key=lambda x: (x['display_name'] or '').lower())
        return jsonify({'contacts': out})

    @app.route('/contacts/forward-targets.json')
    def contacts_forward_targets_json():
        """Контакты, куда одиночную пересылку можно доставить из composer.

        Telegram-цели используют нативный forward, когда источник тоже
        Telegram, или текстовый fallback для MAX/Synapse. Synapse-цели
        принимают текст и сохранённые вложения напрямую.
        """
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle

        db = get_db()
        user_id = session['user_id']
        contacts = db.query(Contact).filter(Contact.user_id == user_id).all()
        contacts = _filter_discussion_contacts(db, contacts)
        ids = [c.id for c in contacts]
        handles_by_contact = {}
        if ids:
            for h in (db.query(MessengerHandle)
                      .filter(MessengerHandle.contact_id.in_(ids)).all()):
                handles_by_contact.setdefault(h.contact_id, []).append(h)
        out = []
        for c in contacts:
            handles = handles_by_contact.get(c.id, [])
            tg = _telegram_reply_handle(handles)
            synapse = next((h for h in handles
                            if h.messenger_name == SYNAPSE_MESSENGER), None)
            if tg is None and synapse is None:
                continue
            messenger = 'Telegram' if tg is not None else SYNAPSE_MESSENGER
            _avatar_for(c)
            out.append({
                'id': c.id,
                'display_name': c.display_name,
                'initial': c.initial,
                'avatar_color': c.avatar_color,
                'avatar_url': c.avatar_url,
                'messenger': messenger,
            })
        out.sort(key=lambda x: (x['display_name'] or '').lower())
        return jsonify({'contacts': out})

    @app.route('/messages/<int:message_id>/react', methods=['POST'])
    def message_react(message_id):
        """Toggle реакции на Telegram-сообщении. Параметр `emoji` — какая.
        Если такая реакция от меня уже стоит — снимаем (отправляем пустой
        набор Telegram-у). Telethon-bridge получит UpdateMessageReactions
        и перепишет БД, но мы заодно делаем оптимистичный апдейт для UI."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        from data.reactions import MessageReaction, replace_reactions
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        if msg.tg_message_id is None:
            return jsonify({'error': 'not_telegram'}), 400
        chat_id = _msg_tg_chat_id(db, msg)
        if chat_id is None:
            return jsonify({'error': 'no_chat'}), 400
        emoji = (request.form.get('emoji') or '').strip()
        if not emoji:
            return jsonify({'error': 'bad_emoji'}), 400

        existing = {r.emoji: r for r in db.query(MessageReaction).filter(
            MessageReaction.message_id == msg.id).all()}
        mine_now = bool(existing.get(emoji) and existing[emoji].mine)
        target_emoji = None if mine_now else emoji  # toggle

        try:
            telegram_bridge.send_reaction(chat_id, msg.tg_message_id,
                                          target_emoji, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'send_failed', 'detail': str(exc)}), 502

        # Оптимистичный snapshot: в обычном (не-Premium) TG у меня может
        # быть только одна реакция на сообщение. Поэтому при постановке
        # новой обязательно снимаем `mine` со ВСЕХ старых моих реакций и
        # уменьшаем их count на 1 (т.к. моя там была учтена).
        # Точную картину UpdateMessageReactions потом всё равно перепишет.
        snapshot = []
        for e, r in existing.items():
            count = r.count
            mine = r.mine
            if e == emoji:
                # Кликнул по существующей: toggle.
                if mine_now:
                    count = max(0, count - 1); mine = False
                else:
                    if not mine:
                        count += 1
                    mine = True
            else:
                # Другая реакция: если она была моей и теперь ставлю новую —
                # снять и уменьшить count.
                if mine and not mine_now:
                    count = max(0, count - 1)
                    mine = False
            if count > 0 or mine:
                snapshot.append({'emoji': e, 'count': count, 'mine': mine})
        if not mine_now and emoji not in existing:
            snapshot.append({'emoji': emoji, 'count': 1, 'mine': True})
        replace_reactions(db, msg.id, snapshot)
        db.commit()
        return jsonify({'ok': True, 'reactions': snapshot})

    @app.route('/messages/delete-bulk', methods=['POST'])
    def messages_delete_bulk():
        """Удаляет выбранные сообщения одним действием.

        scope=self удаляет Telegram-сообщения только у владельца и чистит
        локальные записи. scope=all разрешён только когда все выбранные
        сообщения исходящие и принадлежат одному Telegram-чату.
        """
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge

        raw_ids = (request.form.get('ids')
                   or (request.get_json(silent=True) or {}).get('ids')
                   or '')
        if isinstance(raw_ids, list):
            raw_parts = raw_ids
        else:
            raw_parts = str(raw_ids).split(',')
        try:
            requested_ids = []
            seen = set()
            for raw in raw_parts:
                value = int(str(raw).strip())
                if value not in seen:
                    requested_ids.append(value)
                    seen.add(value)
        except (TypeError, ValueError):
            return jsonify({'error': 'bad_ids'}), 400
        if not requested_ids:
            return jsonify({'error': 'no_ids'}), 400
        if len(requested_ids) > 200:
            return jsonify({'error': 'too_many_ids', 'limit': 200}), 400

        db = get_db()
        user_id = session['user_id']
        rows = (db.query(Messages)
                .filter(Messages.user_id == user_id,
                        Messages.id.in_(requested_ids)).all())
        by_id = {row.id: row for row in rows}
        if len(by_id) != len(requested_ids):
            return jsonify({'error': 'some_not_found'}), 404
        messages = [by_id[value] for value in requested_ids]
        scope = (request.form.get('scope')
                 or (request.get_json(silent=True) or {}).get('scope')
                 or 'self')
        for_all = scope == 'all'
        if for_all and any(not bool(msg.outgoing) for msg in messages):
            return jsonify({'error': 'cannot_delete_for_all'}), 400

        telegram_groups = {}
        for msg in messages:
            if msg.tg_message_id is None:
                if for_all:
                    return jsonify({'error': 'not_telegram'}), 400
                continue
            chat_id = _msg_tg_chat_id(db, msg)
            if chat_id is None:
                if for_all:
                    return jsonify({'error': 'no_chat'}), 400
                continue
            telegram_groups.setdefault(chat_id, []).append(msg.tg_message_id)
        if for_all and len(telegram_groups) != 1:
            return jsonify({'error': 'mixed_sources'}), 400

        tg_deleted = True if telegram_groups else None
        for chat_id, tg_ids in telegram_groups.items():
            try:
                telegram_bridge.delete_messages(
                    chat_id, tg_ids, revoke=for_all, user_id=user_id)
            except Exception:  # noqa: BLE001
                tg_deleted = False
        if for_all and tg_deleted is False:
            db.rollback()
            return jsonify({'error': 'telegram_delete_failed'}), 502
        stored_paths = _purge_message_dependencies(db, messages)
        db.commit()
        _remove_media_paths(stored_paths)
        return jsonify({
            'ok': True,
            'deleted_ids': requested_ids,
            'tg_deleted': tg_deleted,
            'scope': 'all' if for_all else 'self',
        })

    @app.route('/messages/<int:message_id>/retry-send', methods=['POST'])
    def message_retry_media_send(message_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge

        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id,
            Messages.user_id == user_id,
            Messages.outgoing.is_(True)).first()
        if msg is None:
            return jsonify({'error': 'not_found'}), 404
        if msg.tg_message_id is not None:
            return jsonify({'ok': True, 'already_sent': True}), 200
        if getattr(msg, 'delivery_status', None) != 'failed':
            return jsonify({'error': 'not_retryable'}), 400
        chat_id = _msg_tg_chat_id(db, msg)
        attachment = next(iter(_message_attachment_rows(db, msg.id)), None)
        if chat_id is None or attachment is None:
            return jsonify({'error': 'media_missing'}), 400
        data = _read_media_bytes(attachment.stored_path)
        if data is None:
            return jsonify({'error': 'media_missing'}), 404
        del data

        caption = getattr(msg, 'delivery_caption', None) or ''
        silent = bool(getattr(msg, 'delivery_silent', False))
        reply_to_tg_id = getattr(msg, 'delivery_reply_to_tg_id', None)
        stored_schedule_at = getattr(msg, 'delivery_schedule_at', None)
        retry_schedule_at = (stored_schedule_at
                             if stored_schedule_at is not None
                             and stored_schedule_at > datetime.now()
                             else None)
        claimed = (db.query(Messages)
                   .filter(Messages.id == message_id,
                           Messages.user_id == user_id,
                           Messages.tg_message_id.is_(None),
                           Messages.delivery_status == 'failed')
                   .update({Messages.delivery_status: 'sending',
                            Messages.delivery_error: None,
                            Messages.delivery_started_at: datetime.now()},
                           synchronize_session=False))
        db.commit()
        if claimed != 1:
            return jsonify({'error': 'already_retrying'}), 409

        def _delivery_done(sent_id, error):
            if retry_schedule_at is not None:
                _finish_scheduled_media_delivery(message_id, sent_id, error)
            else:
                _finish_telegram_media_delivery(message_id, sent_id, error)

        stored_path = attachment.stored_path

        def _load_delivery_file(path=stored_path):
            payload = _read_media_bytes(path)
            if payload is None:
                raise RuntimeError('Локальный файл отправки недоступен')
            return payload

        response_status = 'sending'
        response_error = None
        try:
            telegram_bridge.queue_file(
                chat_id, _load_delivery_file,
                attachment.original_name or 'file',
                caption,
                callback=_delivery_done, user_id=user_id,
                reply_to=reply_to_tg_id,
                parse_mode='md' if _has_markdown(caption) else None,
                silent=silent,
                schedule=retry_schedule_at,
                voice_note=attachment.kind == 'voice')
        except Exception as exc:  # noqa: BLE001
            _finish_telegram_media_delivery(message_id, None, exc)
            response_status = 'failed'
            response_error = str(exc)[:1000]
        return jsonify({'ok': True, 'queued': True,
                        'delivery_status': response_status,
                        'delivery_error': response_error}), 202

    @app.route('/messages/<int:message_id>/delete', methods=['POST'])
    def message_delete(message_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        # scope='self' — удалить только у меня (в TG останется у собеседника),
        # scope='all' (или не указан, для обратной совместимости) — удалить
        # у всех через Telegram revoke. Для не-Telegram сообщений scope
        # ни на что не влияет: всегда просто чистим локальную копию.
        scope = (request.form.get('scope')
                 or (request.get_json(silent=True) or {}).get('scope')
                 or 'all')
        for_all = scope != 'self'
        tg_deleted = None
        chat_id = _msg_tg_chat_id(db, msg)
        if msg.tg_message_id is not None and chat_id is not None:
            try:
                if for_all:
                    telegram_bridge.delete_message(chat_id, msg.tg_message_id,
                                                   user_id=user_id)
                else:
                    telegram_bridge.delete_message(chat_id, msg.tg_message_id,
                                                   revoke=False,
                                                   user_id=user_id)
                tg_deleted = True
            except Exception:  # noqa: BLE001
                tg_deleted = False
        stored_paths = _purge_message_dependencies(db, [msg])
        db.commit()
        _remove_media_paths(stored_paths)
        return jsonify({'ok': True, 'tg_deleted': tg_deleted,
                        'scope': 'all' if for_all else 'self'})

    @app.route('/messages/<int:message_id>/comments.json')
    def message_comments(message_id):
        """Список комментариев к посту канала через linked discussion
        group. Если сообщение не из канала / нет discussion group —
        возвращаем available=False, UI покажет «недоступно»."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg or msg.tg_message_id is None:
            return jsonify({'available': False, 'reason': 'not_telegram',
                            'items': []})
        chat_id = _msg_tg_chat_id(db, msg)
        if not chat_id:
            return jsonify({'available': False, 'reason': 'no_chat_id',
                            'items': []})
        if not telegram_bridge.is_configured():
            return jsonify({'available': False,
                            'reason': 'telegram_not_configured',
                            'items': []})
        try:
            data = telegram_bridge.get_comments(chat_id, msg.tg_message_id,
                                                user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'available': False, 'reason': 'error',
                            'detail': str(exc), 'items': []})
        return jsonify({'ok': True, **data})

    @app.route('/comments/media/<int:disc_chat_id>/<int:msg_id>')
    def comment_media(disc_chat_id, msg_id):
        """Отдаёт бинарник медиа конкретного комментария. Через Telethon
        download_media. Без кэша на диске — комментариев потенциально
        много и они меняются; экономим место. Если пользователь часто
        пересматривает один и тот же тред, браузер закэширует на свой
        стороне."""
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data import telegram_bridge
        if not telegram_bridge.is_configured():
            return 'Not Found', 404
        try:
            data, mime = telegram_bridge.download_comment_media(
                disc_chat_id, msg_id, user_id=session['user_id'])
        except Exception:  # noqa: BLE001
            return 'Not Found', 404
        if not data:
            return 'Not Found', 404
        return Response(data, mimetype=mime or 'application/octet-stream')

    @app.route('/messages/<int:message_id>/comments', methods=['POST'])
    def message_comments_send(message_id):
        """Отправить новый комментарий к посту канала."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg or msg.tg_message_id is None:
            return jsonify({'error': 'not_telegram'}), 400
        chat_id = _msg_tg_chat_id(db, msg)
        if not chat_id or not telegram_bridge.is_configured():
            return jsonify({'error': 'unavailable'}), 502
        text = (request.form.get('text') or '').strip()
        if not text:
            return jsonify({'error': 'empty'}), 400
        try:
            info = telegram_bridge.get_comments(
                chat_id, msg.tg_message_id, 1, user_id=user_id)
            if not info.get('available'):
                return jsonify({'error': 'no_discussion'}), 400
            result = telegram_bridge.send_comment(
                info['discussion_chat_id'],
                info['top_msg_id'], text, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'send_failed',
                            'detail': str(exc)}), 502
        return jsonify({'ok': True, 'id': result.get('id')})

    @app.route('/messages/<int:message_id>/pin', methods=['POST'])
    def message_pin(message_id):
        """Закрепить сообщение: в Telegram + локально (Messages.pinned_at).
        Для Android-сообщений Telegram-API недоступен — закрепим только
        локально (UI всё равно покажет плашку в верхушке чата)."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        chat_id = _msg_tg_chat_id(db, msg)
        # Если это Telegram-сообщение — закрепим и в самом мессенджере.
        if msg.tg_message_id is not None and chat_id is not None:
            try:
                telegram_bridge.pin_message(chat_id, msg.tg_message_id,
                                             notify=False, user_id=user_id)
            except Exception as exc:  # noqa: BLE001
                return jsonify({'error': 'pin_failed',
                                'detail': str(exc)}), 502
        msg.pinned_at = datetime.now()
        db.commit()
        return jsonify({'ok': True, 'pinned_at': msg.pinned_at.isoformat()})

    @app.route('/messages/<int:message_id>/unpin', methods=['POST'])
    def message_unpin(message_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        chat_id = _msg_tg_chat_id(db, msg)
        if msg.tg_message_id is not None and chat_id is not None:
            try:
                telegram_bridge.unpin_message(chat_id, msg.tg_message_id,
                                              user_id=user_id)
            except Exception as exc:  # noqa: BLE001
                return jsonify({'error': 'unpin_failed',
                                'detail': str(exc)}), 502
        msg.pinned_at = None
        db.commit()
        return jsonify({'ok': True})

    @app.route('/messages/<int:message_id>/translate', methods=['POST'])
    def message_translate(message_id):
        """Перевести текст сообщения через локальную Ollama на язык
        пользователя (`User.preferred_lang`). НЕ кэшируем результат —
        пользователь может сменить целевой язык, и переводить заново
        дешевле, чем городить инвалидацию."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import ollama as _ollama
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        text = (msg.text or '').strip()
        if not text:
            return jsonify({'error': 'empty'}), 400
        user = db.query(User).filter(User.id == user_id).first()
        target = (user.preferred_lang if user else None) or 'ru'
        if not _ollama.is_available():
            return jsonify({
                'status': 'no_ollama',
                'detail': 'Перевод требует локальной Ollama. '
                          'Запустите её и попробуйте снова.',
            }), 503
        try:
            translated = _ollama.translate(text, target)
        except RuntimeError as exc:
            return jsonify({'status': 'llm_error',
                            'detail': str(exc)}), 502
        return jsonify({'ok': True,
                        'text': translated,
                        'lang': target})

    @app.route('/messages/<int:message_id>/edit', methods=['POST'])
    def message_edit(message_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import telegram_bridge
        db = get_db()
        user_id = session['user_id']
        msg = db.query(Messages).filter(
            Messages.id == message_id, Messages.user_id == user_id).first()
        if not msg:
            return jsonify({'error': 'not_found'}), 404
        new_text = (request.form.get('text') or '').strip()
        if not new_text:
            return jsonify({'error': 'empty'}), 400
        # Редактировать в Telegram можно только свои сообщения.
        if not msg.outgoing:
            return jsonify({'error': 'not_own'}), 400
        chat_id = _msg_tg_chat_id(db, msg)
        if msg.tg_message_id is None or chat_id is None:
            return jsonify({'error': 'no_telegram'}), 400
        try:
            telegram_bridge.edit_message(chat_id, msg.tg_message_id, new_text,
                                         user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'edit_failed', 'detail': str(exc)}), 502
        # Сохраняем прошлую версию в историю до подмены — иначе она
        # потеряется (эхо MessageEdited от Telethon придёт уже на новый
        # текст, и `_handle_edited` решит, что изменений нет).
        from data.edits import push_old_version
        if (msg.text or '') != new_text:
            push_old_version(db, msg.id, msg.text or '')
        msg.text = new_text
        db.commit()
        return jsonify({'ok': True, 'text': new_text})

    @app.route('/contacts/merge', methods=['POST'])
    def contacts_merge():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import merge_contacts
        db = get_db()
        is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        try:
            source_id = int(request.form['source_id'])
            target_id = int(request.form['target_id'])
        except (KeyError, ValueError):
            if is_xhr:
                return jsonify({'error': 'bad_request'}), 400
            return 'Bad Request', 400
        try:
            merge_contacts(db, session['user_id'], source_id, target_id)
        except ValueError:
            if is_xhr:
                return jsonify({'error': 'same_contact'}), 400
            return 'Bad Request', 400
        except LookupError:
            if is_xhr:
                return jsonify({'error': 'not_found'}), 404
            return 'Not Found', 404
        if is_xhr:
            return jsonify({'ok': True,
                            'source_id': source_id,
                            'target_id': target_id})
        return redirect('/contacts/manage')

    @app.route('/contacts/handles/<int:handle_id>/extract', methods=['POST'])
    def handle_extract(handle_id):
        """Вынести handle из контакта-«папки» в свой собственный отдельный
        контакт. Имя нового контакта = sender_raw хэндла (как при первичном
        создании). Если в исходном контакте остаётся только этот handle —
        делать нечего, возвращаем already_alone."""
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        handle = db.query(MessengerHandle).filter(
            MessengerHandle.id == handle_id,
            MessengerHandle.user_id == user_id).first()
        if not handle:
            if is_xhr:
                return jsonify({'error': 'not_found'}), 404
            return 'Not Found', 404
        siblings = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == handle.contact_id).count()
        if siblings <= 1:
            if is_xhr:
                return jsonify({'error': 'already_alone'}), 400
            return redirect('/contacts/manage')
        new_contact = Contact(user_id=user_id, display_name=handle.sender_raw)
        db.add(new_contact)
        db.flush()
        old_contact_id = handle.contact_id
        handle.contact_id = new_contact.id
        db.commit()
        if is_xhr:
            return jsonify({'ok': True,
                            'new_contact_id': new_contact.id,
                            'old_contact_id': old_contact_id})
        return redirect('/contacts/manage')

    @app.route('/contacts/handles/<int:handle_id>/move', methods=['POST'])
    def handle_move(handle_id):
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact, MessengerHandle
        db = get_db()
        user_id = session['user_id']
        is_xhr = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        handle = db.query(MessengerHandle).filter(
            MessengerHandle.id == handle_id, MessengerHandle.user_id == user_id).first()
        if not handle:
            if is_xhr:
                return jsonify({'error': 'not_found'}), 404
            return 'Not Found', 404
        try:
            target_id = int(request.form['target_contact_id'])
        except (KeyError, ValueError):
            if is_xhr:
                return jsonify({'error': 'bad_request'}), 400
            return 'Bad Request', 400
        target = db.query(Contact).filter(
            Contact.id == target_id, Contact.user_id == user_id).first()
        if not target:
            if is_xhr:
                return jsonify({'error': 'not_found'}), 404
            return 'Not Found', 404
        old_contact_id = handle.contact_id
        if old_contact_id == target_id:
            if is_xhr:
                return jsonify({'ok': True, 'source_deleted': False,
                                'old_contact_id': old_contact_id,
                                'target_id': target_id})
            return redirect('/contacts/manage')
        handle.contact_id = target_id
        db.flush()
        remaining = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == old_contact_id).count()
        source_deleted = False
        if remaining == 0:
            db.query(Contact).filter(Contact.id == old_contact_id).delete(
                synchronize_session=False)
            source_deleted = True
        db.commit()
        if is_xhr:
            return jsonify({'ok': True,
                            'source_deleted': source_deleted,
                            'old_contact_id': old_contact_id,
                            'target_id': target_id})
        return redirect('/contacts/manage')

    def _device_from_bearer(db):
        from data.devices import Device, hash_token
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        token = auth[len('Bearer '):].strip()
        if not token:
            return None
        return db.query(Device).filter(Device.token_hash == hash_token(token)).first()

    def _expire_stale_pending_replies(db, user_id):
        """Отменяет только задания, которые Android ещё не забрал.

        ``picked`` уже могло уйти через RemoteInput, даже если подтверждение
        задержалось. Сервер не умеет отменить такую задачу и не должен ложно
        помечать её неотправленной — иначе ручной повтор создаст дубль.
        """
        from data.pending_replies import (PendingReply, STATUS_EXPIRED,
                                          STATUS_PENDING)
        now = datetime.now()
        queue_cutoff = now - timedelta(
            seconds=_NOTIFICATION_REPLY_MAX_AGE_SECONDS)
        changed = 0
        changed += (db.query(PendingReply)
                    .filter(PendingReply.user_id == user_id,
                            PendingReply.status == STATUS_PENDING,
                            PendingReply.created_at < queue_cutoff)
                    .update({PendingReply.status: STATUS_EXPIRED,
                             PendingReply.error: 'device_timeout'},
                            synchronize_session=False))
        if changed:
            db.commit()
        return changed

    @app.route('/api/me', methods=['GET'])
    def api_me():
        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return jsonify({'error': 'unauthorized'}), 401
        user = db.query(User).filter(User.id == device.user_id).first()
        return jsonify({
            'user': {'id': user.id, 'name': user.name},
            'device': {'id': device.id, 'name': device.name},
        })

    @app.route('/api/pending_replies', methods=['GET'])
    def api_pending_replies():
        """Android-клиент забирает отложенные ответы для своего пользователя.
        Атомарно помечает их `picked`, чтобы повторный поллинг не возвращал
        одно и то же. Дальше клиент пытается отправить ответ через `RemoteInput`
        и отчитывается в `/api/replies/<id>/done`. Ответ всегда мгновенный:
        блокирующее ожидание здесь заняло бы дефицитный WSGI-поток и замедлило
        бы весь веб-интерфейс на малом тарифе AlwaysData.
        """
        from data.pending_replies import (PendingReply, STATUS_PENDING,
                                          STATUS_PICKED)
        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return jsonify({'error': 'unauthorized'}), 401
        device.last_seen_ip = request.remote_addr
        device.last_seen_at = datetime.now()
        _expire_stale_pending_replies(db, device.user_id)

        items = (db.query(PendingReply)
                 .filter(PendingReply.user_id == device.user_id,
                         PendingReply.status == STATUS_PENDING,
                         or_(PendingReply.picked_up_at.is_(None),
                             PendingReply.picked_up_at <= datetime.now()))
                 .order_by(PendingReply.id.asc()).all())
        now = datetime.now()
        out = []
        for it in items:
            # UPDATE ... WHERE status=pending делает claim безопасным даже
            # если два привязанных устройства опросили очередь одновременно.
            claimed = (db.query(PendingReply)
                       .filter(PendingReply.id == it.id,
                               PendingReply.status == STATUS_PENDING)
                       .update({PendingReply.status: STATUS_PICKED,
                                PendingReply.picked_up_at: now,
                                PendingReply.device_id: device.id},
                               synchronize_session=False))
            if claimed != 1:
                continue
            out.append({
                'id': it.id,
                'package_name': it.package_name,
                'sender_label': it.sender_label,
                'text': it.text,
            })
        db.commit()
        return jsonify({'replies': out})

    @app.route('/api/replies/<int:reply_id>/done', methods=['POST'])
    def api_reply_done(reply_id):
        """Android-клиент отчитывается об отправке. На успехе создаём
        запись `Messages` (исходящее), и она проявляется в ленте веб-панели."""
        from data.contacts import MessengerHandle
        from data.pending_replies import (PendingReply, STATUS_EXPIRED,
                                          STATUS_FAILED, STATUS_PENDING,
                                          STATUS_PICKED, STATUS_SENT)
        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return jsonify({'error': 'unauthorized'}), 401

        pr = db.query(PendingReply).filter(
            PendingReply.id == reply_id,
            PendingReply.user_id == device.user_id).first()
        if pr is None:
            return jsonify({'error': 'not_found'}), 404

        if pr.status == STATUS_SENT:
            return jsonify({'ok': True, 'duplicate': True})
        if pr.status in (STATUS_FAILED, STATUS_EXPIRED):
            return jsonify({'error': 'reply_already_finished',
                            'status': pr.status}), 409
        if pr.status != STATUS_PICKED:
            return jsonify({'error': 'reply_not_picked'}), 409

        body = request.get_json(silent=True) or {}
        ok = bool(body.get('ok'))
        error = str(body.get('error') or '')[:200] or None
        now = datetime.now()

        if ok:
            # Единственный победитель атомарно переводит picked -> sent.
            # Повторный/параллельный callback не сможет создать второй
            # Messages даже если оба запроса успели прочитать старый статус.
            claimed = (db.query(PendingReply)
                       .filter(PendingReply.id == pr.id,
                               PendingReply.user_id == device.user_id,
                               PendingReply.status == STATUS_PICKED)
                       .update({PendingReply.status: STATUS_SENT,
                                PendingReply.sent_at: now,
                                PendingReply.error: None},
                               synchronize_session=False))
            if claimed != 1:
                db.rollback()
                current = db.query(PendingReply).filter(
                    PendingReply.id == reply_id,
                    PendingReply.user_id == device.user_id).first()
                if current is not None and current.status == STATUS_SENT:
                    return jsonify({'ok': True, 'duplicate': True})
                return jsonify({'error': 'reply_already_finished',
                                'status': (current.status
                                           if current is not None
                                           else 'missing')}), 409
            handle = db.query(MessengerHandle).filter(
                MessengerHandle.id == pr.handle_id).first()
            existing_msg = None
            if pr.client_send_key:
                existing_msg = db.query(Messages).filter(
                    Messages.user_id == pr.user_id,
                    Messages.notification_dedup_key
                    == pr.client_send_key).first()
            if existing_msg is None:
                msg = Messages(
                    sender='Вы',
                    text=pr.text,
                    messenger_name=handle.messenger_name if handle else '',
                    time=now.strftime('%H:%M'),
                    user_id=pr.user_id,
                    handle_id=pr.handle_id,
                    created_at=now,
                    outgoing=True,
                    reply_to_message_id=pr.reply_to_message_id,
                    notification_dedup_key=pr.client_send_key,
                )
                db.add(msg)
        else:
            # Потеря notification action часто временна: Android мог только
            # что проснуться, переподключить NotificationListener или ещё не
            # успеть прогреть кэш активных уведомлений. Не объявляем отправку
            # проваленной с первой попытки. Возвращаем задачу в очередь с
            # коротким backoff; общий TTL по created_at по-прежнему не даёт
            # сообщению внезапно уйти спустя много минут.
            retry_deadline = (
                pr.created_at
                + timedelta(seconds=_NOTIFICATION_REPLY_MAX_AGE_SECONDS)
            )
            if (error in _RETRYABLE_NOTIFICATION_REPLY_ERRORS
                    and now < retry_deadline):
                claimed = (db.query(PendingReply)
                           .filter(PendingReply.id == pr.id,
                                   PendingReply.user_id == device.user_id,
                                   PendingReply.status == STATUS_PICKED)
                           .update({
                               PendingReply.status: STATUS_PENDING,
                               PendingReply.error: error,
                               # Для pending это поле служит также временем
                               # следующей разрешённой попытки.
                               PendingReply.picked_up_at: (
                                   now + timedelta(
                                       seconds=_NOTIFICATION_REPLY_RETRY_SECONDS)
                               ),
                               PendingReply.device_id: None,
                           }, synchronize_session=False))
                if claimed != 1:
                    db.rollback()
                    return jsonify({'error': 'reply_already_finished'}), 409
                db.commit()
                return jsonify({
                    'ok': True,
                    'retrying': True,
                    'retry_after_seconds': (
                        _NOTIFICATION_REPLY_RETRY_SECONDS),
                })
            claimed = (db.query(PendingReply)
                       .filter(PendingReply.id == pr.id,
                               PendingReply.user_id == device.user_id,
                               PendingReply.status == STATUS_PICKED)
                       .update({PendingReply.status: STATUS_FAILED,
                                PendingReply.error: error},
                               synchronize_session=False))
            if claimed != 1:
                db.rollback()
                return jsonify({'error': 'reply_already_finished'}), 409

        db.commit()
        return jsonify({'ok': True})

    @app.route('/api/pending_replies/<int:reply_id>/status', methods=['GET'])
    def api_pending_reply_status(reply_id):
        """Веб-панель опрашивает, чем кончилась попытка отправки."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.pending_replies import PendingReply
        db = get_db()
        _expire_stale_pending_replies(db, session['user_id'])
        pr = db.query(PendingReply).filter(
            PendingReply.id == reply_id,
            PendingReply.user_id == session['user_id']).first()
        if pr is None:
            return jsonify({'error': 'not_found'}), 404
        return jsonify({
            'id': pr.id,
            'status': pr.status,
            'error': pr.error,
        })

    @app.route('/download')
    def download_index():
        connect_code = None
        uid = session.get('user_id')
        if uid:
            user = get_db().query(User).filter(User.id == uid).first()
            connect_code = user.connect_code if user else None
        return render_template('download.html', connect_code=connect_code)

    @app.route('/download/skillwood.apk')
    def download_apk():
        candidates = [
            os.path.join(os.getcwd(), 'dist'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist'),
        ]
        for d in candidates:
            if os.path.exists(os.path.join(d, 'skillwood.apk')):
                response = send_from_directory(
                    d, 'skillwood.apk',
                    as_attachment=True,
                    mimetype='application/vnd.android.package-archive',
                )
                # Имя APK неизменно, поэтому мобильный браузер или внешний
                # прокси не должен вернуть предыдущую сборку из кэша.
                response.headers['Cache-Control'] = (
                    'no-store, no-cache, must-revalidate, max-age=0')
                response.headers['Pragma'] = 'no-cache'
                response.headers['Expires'] = '0'
                return response
        abort(404)

    @app.route('/sw.js')
    def service_worker():
        resp = send_from_directory(
            os.path.join(app.root_path, 'static'),
            'synapse-sw.js',
            mimetype='application/javascript',
        )
        resp.headers['Service-Worker-Allowed'] = '/'
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @app.route('/manifest.webmanifest')
    def web_manifest():
        resp = jsonify({
            "id": "/",
            "name": "Synapse",
            "short_name": "Synapse",
            "description": "Единая лента сообщений Synapse",
            "start_url": "/contacts",
            "scope": "/",
            "display": "standalone",
            "background_color": "#0f1115",
            "theme_color": "#2563eb",
            "icons": [
                {
                    "src": "/static/synapse-icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any maskable",
                },
            ],
        })
        resp.mimetype = 'application/manifest+json'
        resp.headers['Cache-Control'] = 'no-cache'
        return resp

    @app.route('/api/webpush/vapid-public-key')
    def webpush_vapid_public_key():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import webpush
        return jsonify({'public_key': webpush.vapid_public_key()})

    @app.route('/api/webpush/status')
    def webpush_status():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import webpush
        return jsonify(webpush.subscription_status(get_db(), session['user_id']))

    @app.route('/api/webpush/test', methods=['POST'])
    def webpush_test():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import webpush
        db = get_db()
        payload = request.get_json(silent=True) or {}
        try:
            delay_seconds = int(payload.get('delay_seconds') or 0)
        except (TypeError, ValueError):
            delay_seconds = 0
        delay_seconds = max(0, min(delay_seconds, 15))
        if delay_seconds:
            time.sleep(delay_seconds)
        result = webpush.notify_test(db, session['user_id'])
        status = webpush.subscription_status(db, session['user_id'])
        return jsonify({
            'ok': result.get('sent', 0) > 0,
            'delay_seconds': delay_seconds,
            **result,
            'status': status,
        })

    @app.route('/api/webpush/subscribe', methods=['POST'])
    def webpush_subscribe():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import webpush
        db = get_db()
        payload = request.get_json(silent=True) or {}
        try:
            sub = webpush.save_subscription(
                db, session['user_id'], payload,
                user_agent=request.headers.get('User-Agent'))
        except ValueError:
            return jsonify({'error': 'bad_subscription'}), 400
        return jsonify({'ok': True, 'id': sub.id})

    @app.route('/api/webpush/unsubscribe', methods=['POST'])
    def webpush_unsubscribe():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data import webpush
        db = get_db()
        payload = request.get_json(silent=True) or {}
        endpoint = (payload.get('endpoint') or '').strip()
        if not endpoint:
            return jsonify({'error': 'bad_subscription'}), 400
        removed = webpush.disable_subscription(
            db, session['user_id'], endpoint)
        return jsonify({'ok': True, 'removed': removed})

    @app.route('/add', methods=['POST'])
    def add_message():
        from data.contacts import record_message

        sender = request.form.get('sender')
        text_value = request.form.get('text')
        messenger_name = request.form.get('messenger_name')
        package_name = (request.form.get('package_name') or '').strip() or None
        author = (request.form.get('author') or '').strip() or None
        is_group = _form_bool(request.form.get('is_group'))
        dedup_key = (request.form.get('dedup_key') or '').strip() or None

        if not sender or not text_value or not messenger_name:
            return 'Bad Request', 400

        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return 'Unauthorized', 401
        device.last_seen_ip = request.remote_addr
        device.last_seen_at = datetime.now()
        db.commit()

        chat_avatar_path = _save_notification_avatar(
            device.user_id, request.form.get('chat_avatar'))
        author_avatar_path = _save_notification_avatar(
            device.user_id, request.form.get('author_avatar'))
        message_author = author if is_group and author else None
        msg = record_message(db, device.user_id, messenger_name, sender,
                             text_value, author=message_author,
                             package_name=package_name, is_group=is_group,
                             contact_avatar_path=chat_avatar_path,
                             author_avatar_path=author_avatar_path,
                             notification_dedup_key=dedup_key)
        if msg is not None:
            _notify_webpush_message(msg.id)
        return 'OK', 200

    @app.route('/add_media', methods=['POST'])
    def add_media():
        # Приём медиа от Android-клиента
        from sqlalchemy.exc import IntegrityError

        from data.attachments import Attachment
        from data.contacts import record_message

        sender = request.form.get('sender')
        messenger_name = request.form.get('messenger_name')
        kind = request.form.get('kind') or 'image'
        dedup_key = (request.form.get('dedup_key') or '').strip() or None
        message_dedup_key = (
            request.form.get('message_dedup_key') or '').strip() or dedup_key
        caption = request.form.get('text') or ''
        package_name = (request.form.get('package_name') or '').strip() or None
        author = (request.form.get('author') or '').strip() or None
        is_group = _form_bool(request.form.get('is_group'))
        upload = request.files.get('file')

        if not sender or not messenger_name or upload is None:
            return 'Bad Request', 400

        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return 'Unauthorized', 401
        user_id = device.user_id

        device.last_seen_ip = request.remote_addr
        device.last_seen_at = datetime.now()
        db.commit()

        # Если это фото уже было (Max/VK шлёт повторно) - не создаём дубль
        if dedup_key is not None:
            exists = (db.query(Attachment.id)
                      .filter(Attachment.user_id == user_id,
                              Attachment.dedup_key == dedup_key).first())
            if exists is not None:
                return 'OK Duplicate', 200

        data = upload.read()
        if not data:
            return 'Bad Request', 400

        chat_avatar_path = _save_notification_avatar(
            user_id, request.form.get('chat_avatar'))
        author_avatar_path = _save_notification_avatar(
            user_id, request.form.get('author_avatar'))
        placeholder = {'image': '📷 Фото',
                       'sticker': '🩷 Стикер',
                       'video': '🎬 Видео',
                       'voice': '🎙 Голосовое',
                       'audio': '🎵 Аудио',
                       'file': '📎 Файл'}.get(kind, '📎 Вложение')
        message_author = author if is_group and author else None
        msg = record_message(
            db, user_id, messenger_name, sender, caption or placeholder,
            author=message_author, package_name=package_name,
            is_group=is_group, contact_avatar_path=chat_avatar_path,
            author_avatar_path=author_avatar_path,
            notification_dedup_key=message_dedup_key)
        if msg is None:
            return 'OK', 200

        stored_path = _store_media_bytes(user_id, data)

        att = Attachment(
            user_id=user_id,
            message_id=msg.id,
            kind=kind,
            mime=upload.mimetype,
            original_name=upload.filename or None,
            stored_path=stored_path,
            size=len(data),
            dedup_key=dedup_key,
        )
        db.add(att)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return 'OK Duplicate', 200
        _notify_webpush_message(msg.id)
        return 'OK', 200

    @app.route('/attachments/<int:attachment_id>/save-sticker',
               methods=['POST'])
    def attachment_save_local_sticker(attachment_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        return _sticker_collection_disabled_response()

    @app.route('/attachments/<int:attachment_id>/save-sticker-pack',
               methods=['POST'])
    def attachment_save_local_sticker_pack(attachment_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        return _sticker_collection_disabled_response()

    @app.route('/stickers.json')
    def stickers_json():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        return jsonify({
            'ok': True,
            'disabled': True,
            'detail': STICKER_COLLECTION_DISABLED_DETAIL,
            'stickers': [],
        })

    @app.route('/stickers/<int:sticker_id>')
    def sticker_get(sticker_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.stickers import SavedSticker

        db = get_db()
        sticker = (db.query(SavedSticker)
                   .filter(SavedSticker.id == sticker_id,
                           SavedSticker.user_id == session['user_id'])
                   .first())
        if sticker is None:
            return 'Not Found', 404
        raw = _read_media_bytes(sticker.stored_path)
        if raw is None:
            return 'Not Found', 404
        return Response(raw, mimetype=sticker.mime or 'application/octet-stream')

    @app.route('/attachments/<int:attachment_id>')
    def attachment_get(attachment_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.attachments import Attachment
        from data.crypto import decrypt_bytes
        from cryptography.fernet import InvalidToken
        db = get_db()
        att = (db.query(Attachment)
               .filter(Attachment.id == attachment_id,
                       Attachment.user_id == session['user_id']).first())
        if att is None:
            return 'Not Found', 404
        try:
            full = _safe_media_full_path(att.stored_path)
            with open(full, 'rb') as f:
                raw = decrypt_bytes(f.read())
        except (OSError, ValueError, InvalidToken):
            return 'Not Found', 404
        return Response(raw, mimetype=att.mime or 'application/octet-stream')

    @app.route('/attachments/<int:attachment_id>/restore', methods=['POST'])
    def attachment_restore_from_telegram(attachment_id):
        """Явно возвращает вытесненный Telegram-файл в локальный кэш."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.attachments import Attachment

        db = get_db()
        user_id = session['user_id']
        attachment = (db.query(Attachment)
                      .filter(Attachment.id == attachment_id,
                              Attachment.user_id == user_id).first())
        if attachment is None:
            return jsonify({'error': 'not_found'}), 404
        message = db.get(Messages, attachment.message_id)
        handle = _telegram_media_restore_handle(
            db, user_id, message, attachment.kind)
        if handle is None:
            return jsonify({
                'error': 'media_unavailable',
                'detail': 'Этот файл нельзя повторно загрузить из Telegram.',
            }), 409

        if _media_rel_path_exists(attachment.stored_path):
            return jsonify({
                'ok': True,
                'status': 'ready',
                'attachment': {
                    'id': attachment.id,
                    'kind': attachment.kind,
                    'mime': attachment.mime,
                    'name': attachment.original_name,
                    'availability': 'local',
                    'url': f'/attachments/{attachment.id}',
                },
            })
        restore_message_id = int(message.id)
        restore_attachment_id = int(attachment.id)
        db.rollback()  # освобождаем SQLite read-lock до фоновой записи
        key, job = _queue_media_restore(
            user_id, restore_message_id, restore_attachment_id)
        result, status_code = _media_restore_job_response(key, job)
        return jsonify(result), status_code

    @app.route('/messages/<int:message_id>/telegram-media/restore',
               methods=['POST'])
    def message_media_restore_from_telegram(message_id):
        """Чинит старые placeholders, у которых ещё не было Attachment."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.attachments import Attachment

        db = get_db()
        user_id = session['user_id']
        message = (db.query(Messages)
                   .filter(Messages.id == message_id,
                           Messages.user_id == user_id).first())
        kind = _telegram_placeholder_media_kind(
            message.text if message is not None else None)
        handle = _telegram_media_restore_handle(db, user_id, message, kind)
        if handle is None:
            return jsonify({
                'error': 'media_unavailable',
                'detail': 'Это сообщение нельзя восстановить из Telegram.',
            }), 409

        with _media_restore_lock(('message', message.id)):
            db.expire_all()
            message = db.get(Messages, message_id)
            kind = _telegram_placeholder_media_kind(
                message.text if message is not None else None)
            handle = _telegram_media_restore_handle(
                db, user_id, message, kind)
            if handle is None:
                return jsonify({
                    'error': 'media_unavailable',
                    'detail': 'Медиа больше нельзя получить из Telegram.',
                }), 409
            attachment = (db.query(Attachment)
                          .filter(Attachment.user_id == user_id,
                                  Attachment.message_id == message.id)
                          .order_by(Attachment.id.asc()).first())
            if attachment is None:
                attachment = Attachment(
                    user_id=user_id,
                    message_id=message.id,
                    kind=kind,
                    mime=None,
                    original_name=None,
                    stored_path=f'{user_id}/{uuid.uuid4().hex}.enc',
                    size=None,
                )
                db.add(attachment)
                db.commit()
            if _media_rel_path_exists(attachment.stored_path):
                return jsonify({
                    'ok': True,
                    'status': 'ready',
                    'attachment': {
                        'id': attachment.id,
                        'kind': attachment.kind,
                        'mime': attachment.mime,
                        'name': attachment.original_name,
                        'availability': 'local',
                        'url': f'/attachments/{attachment.id}',
                    },
                })
        restore_message_id = int(message.id)
        restore_attachment_id = int(attachment.id)
        db.rollback()  # освобождаем SQLite read-lock до фоновой записи
        key, job = _queue_media_restore(
            user_id, restore_message_id, restore_attachment_id)
        result, status_code = _media_restore_job_response(key, job)
        return jsonify(result), status_code

    @app.route('/attachments/<int:attachment_id>/telegram-sticker',
               methods=['POST'])
    def attachment_save_telegram_sticker(attachment_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.attachments import Attachment
        from data.contacts import MessengerHandle
        from data import telegram_bridge

        mode = (request.form.get('mode') or 'single').strip().lower()
        if mode not in ('single', 'pack'):
            return jsonify({'error': 'bad_mode'}), 400

        db = get_db()
        user_id = session['user_id']
        att = (db.query(Attachment)
               .filter(Attachment.id == attachment_id,
                       Attachment.user_id == user_id).first())
        if att is None:
            return jsonify({'error': 'not_found'}), 404
        if att.kind != 'sticker':
            return jsonify({'error': 'not_sticker'}), 400

        msg = db.get(Messages, att.message_id)
        if msg is None or msg.user_id != user_id or not msg.tg_message_id:
            return jsonify({'error': 'not_telegram_sticker'}), 400
        handle = db.get(MessengerHandle, msg.handle_id)
        if (handle is None or handle.user_id != user_id
                or handle.messenger_name != 'Telegram'
                or handle.tg_chat_id is None):
            return jsonify({'error': 'not_telegram_sticker'}), 400

        try:
            result = telegram_bridge.save_sticker_from_message(
                handle.tg_chat_id, msg.tg_message_id, mode, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'telegram_unavailable',
                            'detail': str(exc)}), 502
        return jsonify(result or {'ok': True, 'mode': mode})

    @app.route('/attachments/<int:attachment_id>/sticker-pack')
    @app.route('/attachments/<int:attachment_id>/telegram-sticker-pack')
    def attachment_sticker_pack(attachment_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.attachments import Attachment
        from data.contacts import MessengerHandle
        from data import telegram_bridge

        db = get_db()
        user_id = session['user_id']
        att = (db.query(Attachment)
               .filter(Attachment.id == attachment_id,
                       Attachment.user_id == user_id).first())
        if att is None:
            return jsonify({'error': 'not_found'}), 404
        if att.kind != 'sticker':
            return jsonify({'error': 'not_sticker'}), 400
        if att.sticker_pack_key:
            payload = _local_sticker_pack_payload(
                db, user_id, att.sticker_pack_key)
            if payload.get('count'):
                return jsonify(payload)

        msg = db.get(Messages, att.message_id)
        if msg is None or msg.user_id != user_id or not msg.tg_message_id:
            return jsonify({'error': 'not_telegram_sticker'}), 400
        handle = db.get(MessengerHandle, msg.handle_id)
        if (handle is None or handle.user_id != user_id
                or handle.messenger_name != 'Telegram'
                or handle.tg_chat_id is None):
            return jsonify({'error': 'not_telegram_sticker'}), 400

        try:
            result = telegram_bridge.sticker_pack_from_message(
                handle.tg_chat_id, msg.tg_message_id, user_id=user_id)
        except Exception as exc:  # noqa: BLE001
            return jsonify({'error': 'telegram_unavailable',
                            'detail': str(exc)}), 502
        return jsonify(result)

    @app.route('/api/ping')
    def api_ping():
        return jsonify({'ok': True, 'service': 'skillwood'})

    @app.route('/api/connect', methods=['POST'])
    def api_connect():
        from data.devices import Device, generate_token, hash_token

        body = request.get_json(silent=True) or {}
        code = (body.get('code') or '').strip()
        device_name = (body.get('device_name') or '').strip()
        if not code or not device_name:
            return jsonify({'error': 'code and device_name required'}), 400

        db = get_db()
        user = db.query(User).filter(User.connect_code == code).first()
        if not user:
            return jsonify({'error': 'unknown code'}), 404

        token = generate_token()
        device = Device(user_id=user.id, name=device_name,
                        token_hash=hash_token(token))
        db.add(device)
        db.commit()
        return jsonify({
            'token': token,
            'user': {'id': user.id, 'name': user.name},
            'device': {'id': device.id, 'name': device.name},
        })

def _generate_code() -> str:
    return ''.join(random.choices(string.digits, k=8))


def _generate_unique_code(db, exclude=None) -> str:
    for _ in range(100):
        code = _generate_code()
        if code == exclude:
            continue
        exists = db.query(User.id).filter(User.connect_code == code).first()
        if not exists:
            return code
    raise RuntimeError('Не удалось сгенерировать уникальный код подключения')


if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
