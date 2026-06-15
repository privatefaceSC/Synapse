import os
import random
import re
import string
import time
import uuid
import base64
from datetime import datetime

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template,
                   request, send_from_directory, session)
from werkzeug.security import check_password_hash, generate_password_hash

from data import db_sessions
from data.users import Messages, User


_AVATAR_PALETTE = [
    "#ef4444", "#f59e0b", "#10b981", "#3b82f6",
    "#8b5cf6", "#ec4899", "#14b8a6", "#f97316",
]
SYNAPSE_MESSENGER = "Synapse"


def _configure_timezone():
    os.environ.setdefault("TZ", "Europe/Moscow")
    if hasattr(time, "tzset"):
        time.tzset()


def _avatar_for(contact):
    name = (contact.display_name or "?").strip()
    contact.initial = name[:1].upper() if name else "?"
    contact.avatar_color = _AVATAR_PALETTE[contact.id % len(_AVATAR_PALETTE)]
    contact.avatar_url = (f'/contacts/{contact.id}/photo'
                          if getattr(contact, 'avatar_path', None) else None)
    return contact


def _enrich_with_last_message(db, contacts):
    from data.contacts import MessengerHandle
    from sqlalchemy import func, or_

    for c in contacts:
        _avatar_for(c)
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == c.id).all()
        handle_ids = [h.id for h in handles]
        # Уникальные мессенджеры контакта (для «папки» с выбором чата).
        msgrs = []
        for h in handles:
            if h.messenger_name not in msgrs:
                msgrs.append(h.messenger_name)
        c.messengers = msgrs
        if not handle_ids:
            c.last_preview = None
            c.last_time = None
            c.last_at = None
            c.unread_count = 0
            continue

        last = (
            db.query(Messages)
            .filter(Messages.handle_id.in_(handle_ids))
            .order_by(Messages.created_at.desc().nullslast(), Messages.id.desc())
            .first()
        )
        if last:
            c.last_preview = last.text
            c.last_time = last.time
            c.last_at = last.created_at
        else:
            c.last_preview = None
            c.last_time = None
            c.last_at = None

        # Свои исходящие в «непрочитанные» не считаем — иначе после отправки
        # сообщения собственный чат подсвечивается красным «1». В Telegram,
        # очевидно, тоже не подсвечивает то, что ты сам только что написал.
        unread_q = (db.query(func.count(Messages.id))
                    .filter(Messages.handle_id.in_(handle_ids))
                    .filter(or_(Messages.outgoing.is_(None),
                                Messages.outgoing.is_(False))))
        if c.last_read_at is not None:
            unread_q = unread_q.filter(Messages.created_at > c.last_read_at)
        c.unread_count = unread_q.scalar() or 0
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
        # Если у сообщения есть вложение, а текст — это технический
        # плейсхолдер вида «📷 Фото», прячем его: само фото и так в bubble.
        # Реальная подпись остаётся как есть.
        if m.media and is_media_placeholder(m.text):
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
    """Проставляет каждому сообщению `fwd_quote` — {name, contact_id} того,
    от кого его переслали (если это форвард из Telegram). `contact_id` —
    наш Contact, у которого есть MessengerHandle с этим tg_chat_id; если
    автора у нас в контактах нет (или он скрыт) — None, ник в UI
    становится некликабельным."""
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
        m.fwd_quote = {
            'name': name,
            'contact_id': cid_by_chat.get(chat_id),
        }
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


def create_app(db_path: str = "db/blogs.db") -> Flask:
    _configure_timezone()
    db_sessions.global_init(db_path)

    app = Flask(__name__)
    app.config['SECRET_KEY'] = 'yandexlyceum_secret_key'
    app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

    @app.teardown_appcontext
    def _close_db(_exc):
        sess = g.pop('db', None)
        if sess is not None:
            sess.close()

    register_routes(app)

    from data import telegram_bridge
    telegram_bridge.start()
    return app


def get_db():
    if 'db' not in g:
        g.db = db_sessions.create_session()
    return g.db


def _media_root() -> str:
    return os.environ.get('SKILLWOOD_MEDIA_ROOT') or os.path.join(os.getcwd(), 'media')


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


def _is_admin() -> bool:
    return session.get('user_id') == 1


# --- Внутренний мессенджер ----------------------------------------------

_USERNAME_RE = re.compile(r'^[a-z0-9_]{3,32}$')

_DM_PLACEHOLDER = {'image': '📷 Фото', 'video': '🎬 Видео',
                   'audio': '🎵 Аудио', 'file': '📎 Файл'}


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


def _dm_user_card(user) -> dict:
    """Краткая карточка пользователя для UI внутреннего мессенджера."""
    full = ((user.name or '') + ' ' + (user.surname or '')).strip()
    label = full or (user.username or '?')
    has_avatar = os.path.exists(_avatar_file(user.id))
    return {
        'id': user.id,
        'username': user.username or '',
        'display_name': label,
        'initial': label[:1].upper() if label else '?',
        'avatar_color': _AVATAR_PALETTE[user.id % len(_AVATAR_PALETTE)],
        'avatar_url': f'/messenger/avatar/{user.id}' if has_avatar else None,
    }


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
                         'name': a.original_name} for a in atts.get(m.id, [])],
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
    return msg


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

    @app.context_processor
    def inject_gear_user():
        """Передаёт текущего пользователя во все шаблоны под именем
        `gear_user` — нужно base.html'у, чтобы показать connect_code
        в выпадающем меню шестерёнки. Если не залогинен — None."""
        uid = session.get('user_id')
        if not uid:
            return {'gear_user': None}
        try:
            db = get_db()
            return {'gear_user': db.query(User)
                    .filter(User.id == uid).first()}
        except Exception:  # noqa: BLE001
            return {'gear_user': None}

    @app.route('/')
    def main_menu():
        if session.get('user_id'):
            return redirect('/home')
        return render_template('main_menu.html')

    @app.route('/logout')
    def logout():
        session.pop('user_id', None)
        return redirect('/')

    @app.route('/home')
    def index():
        if not session.get('user_id'):
            return redirect('/login')
        from data.contacts import Contact
        from data.devices import Device
        db = get_db()
        user = db.query(User).filter(User.id == session['user_id']).first()
        contacts_count = db.query(Contact).filter(Contact.user_id == user.id).count()
        messages_count = db.query(Messages).filter(Messages.user_id == user.id).count()
        device_connected = db.query(Device.id).filter(Device.user_id == user.id).first() is not None
        return render_template(
            'index.html',
            user=user,
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
        if not error and db.query(User).filter(
                User.username == new, User.id != me.id).first():
            error = "Этот User ID уже занят"
        if error:
            return (jsonify({'error': error}), 400) if is_xhr \
                else redirect('/home')
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
        me = db.query(User).filter(User.id == session['user_id']).first()
        return render_template('profile.html', user=me)

    @app.route('/profile/password', methods=['POST'])
    def profile_password():
        """Смена пароля: требуется подтверждение текущего."""
        if not session.get('user_id'):
            return redirect('/login')
        db = get_db()
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
            return render_template('profile.html', user=me, pw_error=error)
        me.hashed_password = generate_password_hash(new1)
        db.commit()
        return render_template('profile.html', user=me,
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
            elif (db.query(User)
                  .filter(User.username == username,
                          User.id != me.id).first()):
                error = "Этот User ID уже занят"
        if error:
            return render_template('profile.html', user=me, info_error=error)
        me.name = name
        me.surname = surname or None
        me.email = email
        me.username = username
        db.commit()
        return render_template('profile.html', user=me,
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
        from data.crypto import encrypt_bytes
        from data.direct import DirectAttachment, DirectMessage
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

        now = datetime.now()
        msg = DirectMessage(sender_id=me_id, recipient_id=user_id,
                            text=text or None, created_at=now)
        db.add(msg)
        db.flush()

        attachments = []
        if has_file:
            data = upload.read()
            if not data:
                db.rollback()
                return jsonify({'error': 'empty'}), 400
            kind = _kind_from_mime(upload.mimetype)
            os.makedirs(os.path.join(_media_root(), 'dm'), exist_ok=True)
            stored_path = 'dm/' + uuid.uuid4().hex + '.enc'
            with open(os.path.join(_media_root(), stored_path), 'wb') as f:
                f.write(encrypt_bytes(data))
            att = DirectAttachment(
                message_id=msg.id, kind=kind, mime=upload.mimetype,
                original_name=upload.filename or None,
                stored_path=stored_path, size=len(data))
            db.add(att)
            if not msg.text:
                msg.text = _DM_PLACEHOLDER.get(kind, '📎 Файл')
            db.flush()
            attachments = [{'id': att.id, 'kind': att.kind,
                            'name': att.original_name}]

        db.commit()
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
            from data.crypto import encrypt_bytes
            upload = request.files.get('avatar')
            if upload is None or not upload.filename:
                return redirect('/home')
            data = upload.read()
            mime = (upload.mimetype or '').lower()
            if not data or len(data) > 5 * 1024 * 1024 \
                    or not mime.startswith('image/'):
                return redirect('/home')
            os.makedirs(os.path.join(_media_root(), str(user_id)), exist_ok=True)
            with open(_avatar_file(user_id), 'wb') as f:
                f.write(encrypt_bytes(data))
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
        for u in users:
            _user_avatar_for(u)
        return render_template('users.html', users=users)

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
        force_sms = request.form.get('force_sms') == '1'
        if phone:
            try:
                telegram_bridge.request_code(phone, user_id=user_id,
                                             force_sms=force_sms)
            except Exception as exc:  # noqa: BLE001
                return render_template('telegram.html',
                                       tg=telegram_bridge.status(user_id),
                                       error=str(exc))
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
            except Exception as exc:  # noqa: BLE001
                return render_template('telegram.html',
                                       tg=telegram_bridge.status(user_id),
                                       error=str(exc))
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
            except Exception as exc:  # noqa: BLE001
                return render_template('telegram.html',
                                       tg=telegram_bridge.status(user_id),
                                       error=str(exc))
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
            user.connect_code = _generate_code()
            db.add(user)
            db.commit()
            session['user_id'] = user.id
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
        if request.method == 'POST':
            db = get_db()
            email = request.form.get('email')
            password = request.form.get('password')
            user = db.query(User).filter(User.email == email).first()
            if user and check_password_hash(user.hashed_password, password):
                session['user_id'] = user.id
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
        from data.contacts import Contact
        db = get_db()
        user_id = session['user_id']
        _sync_direct_messages_to_contacts(db, user_id)
        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        contacts = _filter_discussion_contacts(db, contacts)
        _enrich_with_last_message(db, contacts)
        return render_template('contacts.html', contacts=contacts,
                               selected=None, selected_handles=[], messages=None)

    @app.route('/contacts.json')
    def contacts_index_json():
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact
        db = get_db()
        user_id = session['user_id']
        _sync_direct_messages_to_contacts(db, user_id)
        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        contacts = _filter_discussion_contacts(db, contacts)
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
                'unread_count': c.unread_count or 0,
                'pinned': bool(c.pinned_at),
                'muted': bool(c.muted),
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
        _sync_direct_messages_to_contacts(db, user_id)

        contacts = db.query(Contact).filter(Contact.user_id == user_id).all()
        contacts = _filter_discussion_contacts(db, contacts)
        contact_by_id = {c.id: c for c in contacts}
        matched_by_name = {c.id for c in contacts
                           if q in (c.display_name or '').lower()}

        handle_to_meta = {
            h.id: (h.contact_id, h.messenger_name)
            for h in db.query(MessengerHandle)
            .filter(MessengerHandle.user_id == user_id).all()
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
        _sync_direct_messages_to_contacts(db, user_id)
        contact = (
            db.query(Contact)
            .filter(Contact.id == contact_id, Contact.user_id == user_id)
            .first()
        )
        if not contact:
            return 'Not Found', 404

        contact.last_read_at = datetime.now()
        db.commit()

        contacts = (
            db.query(Contact)
            .filter(Contact.user_id == user_id)
            .all()
        )
        contacts = _filter_discussion_contacts(db, contacts)
        _enrich_with_last_message(db, contacts)
        _avatar_for(contact)

        handles = db.query(MessengerHandle).filter(MessengerHandle.contact_id == contact.id).all()
        _mark_synapse_contact_read(db, user_id, handles)
        # Контакт — «папка»: чат на каждый мессенджер. Показываем один.
        available = []
        for h in handles:
            if h.messenger_name not in available:
                available.append(h.messenger_name)
        current_m = _pick_messenger(available, request.args.get('m'))
        m_handles = [h for h in handles if h.messenger_name == current_m]
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
        is_group = (tg_chat_handle is not None
                    and tg_chat_handle.tg_chat_type == 'group')
        # Тип чата нужен фронту, чтобы под канальными постами появлялась
        # кнопка «💬 Комментарии» (linked discussion group).
        chat_type = (tg_chat_handle.tg_chat_type
                     if tg_chat_handle is not None else None)
        msgs = (
            db.query(Messages)
            .filter(Messages.handle_id.in_(handle_ids))
            .order_by(Messages.created_at.desc().nullslast(), Messages.id.desc())
            .limit(80)
            .all()
        )
        msgs = list(reversed(msgs))
        for m in msgs:
            m.display_author = display_author(m.sender, contact.display_name)
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
                               messengers=available, current_messenger=current_m)

    @app.route('/contacts/<int:contact_id>/messages.json')
    def contact_messages_json(contact_id):
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact, MessengerHandle
        from data.matching import display_author
        db = get_db()
        user_id = session['user_id']
        _sync_direct_messages_to_contacts(db, user_id)
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id, Contact.user_id == user_id).first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        handles = db.query(MessengerHandle).filter(
            MessengerHandle.contact_id == contact.id).all()
        _mark_synapse_contact_read(db, user_id, handles)
        available = []
        for h in handles:
            if h.messenger_name not in available:
                available.append(h.messenger_name)
        current_m = _pick_messenger(available, request.args.get('m'))
        m_handles = [h for h in handles if h.messenger_name == current_m]
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
        is_group = (tg_chat_handle is not None
                    and tg_chat_handle.tg_chat_type == 'group')
        is_forum = bool(tg_chat_handle is not None and tg_chat_handle.tg_is_forum)
        # Lazy-определение форума: для tg-группы/канала, где tg_is_forum
        # ещё не выставлен (handle создан до фичи или это новый чат),
        # один раз дёргаем MTProto. Кэш `_forum_topics_cache` на 60 сек
        # защищает от повторных вызовов — последующие открытия мгновенные.
        if (not is_forum and tg_chat_handle is not None
                and tg_chat_handle.tg_chat_type in ('group', 'channel')):
            from data import telegram_bridge as _tg
            live = _tg.fetch_forum_topics(tg_chat_handle.tg_chat_id)
            if live:
                tg_chat_handle.tg_is_forum = True
                db.commit()
                is_forum = True
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
        msgs_q = db.query(Messages).filter(Messages.handle_id.in_(handle_ids))
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
        db.commit()
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
                'is_forum': is_forum,
                'topic_id': topic_id_int,
                'messengers': available,
                'messenger': current_m,
                'chat_type': (tg_chat_handle.tg_chat_type
                              if tg_chat_handle is not None else None),
                'notifications_muted': bool(contact.muted),
            },
            'topics': saved_topics,
            'has_older': has_older,
            'older_before_id': msgs[0].id if msgs else None,
            'messages': [
                {'id': m.id, 'sender': m.sender, 'text': m.visible_text,
                 'text_html': m.visible_text_html,
                 'messenger_name': m.messenger_name, 'time': m.time,
                 'outgoing': bool(m.outgoing),
                 'tg_read': bool(m.tg_read_at) if m.tg_message_id else None,
                 'deleted': bool(m.deleted_at),
                 'pinned': bool(m.pinned_at),
                 'ttl_seconds': m.tg_ttl_seconds,
                 'display_author': display_author(m.sender, contact.display_name),
                 'reply_to': m.reply_quote,
                 'fwd_from': m.fwd_quote,
                 'edits': getattr(m, 'edit_history', []),
                 'reactions': getattr(m, 'reactions', []),
                 'attachments': [{'id': a.id, 'kind': a.kind,
                                  'name': a.original_name} for a in m.media]}
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
        from data.pending_replies import PendingReply, STATUS_PENDING
        db = get_db()
        user_id = session['user_id']
        contact = (db.query(Contact)
                   .filter(Contact.id == contact_id, Contact.user_id == user_id).first())
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        text = (request.form.get('text') or '').strip()
        upload = request.files.get('file')
        forward_raw = (request.form.get('forward_message_id') or '').strip()
        forward_source = None
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
            if forward_source.tg_message_id is None:
                return jsonify({'error': 'forward_not_telegram'}), 400
        if not text and upload is None and forward_source is None:
            return jsonify({'error': 'empty'}), 400

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
            if upload is not None:
                return jsonify({'error': 'media_not_supported'}), 400
            if forward_source is not None:
                return jsonify({'error': 'target_not_telegram'}), 400
            if not text:
                return jsonify({'error': 'empty'}), 400
            partner_id = _synapse_partner_id(synapse_handle)
            if partner_id is None or partner_id == user_id:
                return jsonify({'error': 'not_found'}), 404

            from data.direct import DirectMessage
            now = datetime.now()
            direct_msg = DirectMessage(
                sender_id=user_id,
                recipient_id=partner_id,
                text=text,
                created_at=now,
            )
            db.add(direct_msg)
            db.flush()
            msg = Messages(
                sender='Вы',
                text=text,
                messenger_name=SYNAPSE_MESSENGER,
                time=now.strftime('%H:%M'),
                user_id=user_id,
                handle_id=synapse_handle.id,
                created_at=now,
                outgoing=True,
            )
            db.add(msg)
            db.flush()
            me = db.query(User).filter(User.id == user_id).first()
            partner = db.query(User).filter(User.id == partner_id).first()
            if me is not None and partner is not None:
                users_by_id = {user_id: me, partner_id: partner}
                _mirror_direct_message_for_owner(
                    db, direct_msg, partner_id, users_by_id)

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
            db.commit()
            return jsonify({'ok': True, 'id': msg.id, 'time': msg.time,
                            'text': msg.text, 'text_html': None,
                            'messenger_name': SYNAPSE_MESSENGER,
                            'reply_to': reply_quote,
                            'forwarded': False})
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
                if source_chat_id is None:
                    return jsonify({'error': 'no_source_chat'}), 400
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

            if upload is not None:
                data = upload.read()
                if not data:
                    return jsonify({'error': 'empty'}), 400
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
                try:
                    sent_id = telegram_bridge.send_file(
                        tg_handle.tg_chat_id, data,
                        upload.filename or 'file', text,
                        **_md_kw_f, **reply_kw_tg, **_opts_f,
                        user_id=user_id)
                except Exception as exc:  # noqa: BLE001
                    return jsonify({'error': 'send_failed',
                                    'detail': str(exc)}), 502
                # Scheduled: Telegram пришлёт echo только когда оно реально
                # отправится, поэтому локально записывать его сейчас не нужно.
                if schedule_at is not None:
                    return jsonify({'ok': True, 'scheduled': True,
                                    'when': schedule_at.isoformat()})
                # СРАЗУ пишем локальную запись Messages + Attachment —
                # echo для своих media от Telethon приходит не всегда
                # (зависит от версии/настроек клиента). Anti-dupe по
                # tg_message_id в `_handle_message` защитит от двойной
                # записи, если echo всё-таки прилетит.
                mime = (upload.mimetype or '').lower()
                if mime.startswith('image/'):
                    kind = 'image'
                elif mime.startswith('video/'):
                    kind = 'video'
                elif mime.startswith('audio/'):
                    kind = 'audio'
                else:
                    kind = 'file'
                placeholder = {
                    'image': '📷 Фото', 'video': '🎬 Видео',
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
                    tg_message_id=sent_id,
                    reply_to_message_id=(
                        reply_target.id if reply_target else None),
                )
                db.add(msg)
                db.flush()  # нужно msg.id для Attachment
                # Шифруем и кладём файл в media/<user_id>/<uuid>.enc —
                # тот же контракт, что у моста (_save_attachment).
                from data.attachments import Attachment as _Attachment
                from data.crypto import encrypt_bytes as _encrypt_bytes
                import uuid as _uuid
                root = (os.environ.get('SKILLWOOD_MEDIA_ROOT')
                        or os.path.join(os.getcwd(), 'media'))
                rel_dir = str(user_id)
                os.makedirs(os.path.join(root, rel_dir), exist_ok=True)
                stored_path = f"{rel_dir}/{_uuid.uuid4().hex}.enc"
                with open(os.path.join(root, stored_path), 'wb') as f:
                    f.write(_encrypt_bytes(data))
                att = _Attachment(
                    user_id=user_id,
                    message_id=msg.id,
                    kind=kind,
                    mime=mime or None,
                    original_name=upload.filename or None,
                    stored_path=stored_path,
                    size=len(data),
                )
                db.add(att)
                db.commit()
                return jsonify({'ok': True, 'media': True, 'id': msg.id,
                                'forwarded': forward_source is not None})

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
                            'text': md_plain, 'text_html': md_html,
                            'reply_to': reply_quote,
                            'forwarded': forward_source is not None})

        # --- Notification reply через Android (только текст) ---
        if upload is not None:
            return jsonify({'error': 'media_not_supported'}), 400
        if not text:
            return jsonify({'error': 'empty'}), 400
        pr = PendingReply(
            user_id=user_id,
            handle_id=notif_handle.id,
            text=text,
            package_name=_package_for_handle(notif_handle),
            sender_label=notif_handle.sender_raw,
            status=STATUS_PENDING,
            reply_to_message_id=reply_target.id if reply_target else None,
        )
        db.add(pr)
        db.commit()
        return jsonify({'ok': True, 'queued': True, 'pending_id': pr.id,
                        'via': 'notif'})

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
                     if contact.avatar_path else [])
            return jsonify({'ok': True, 'items': items})
        try:
            photos = telegram_bridge.fetch_profile_photos(handle.tg_chat_id,
                                                          user_id=user_id)
        except Exception:  # noqa: BLE001
            items = ([{'photo_id': 'local',
                       'url': f'/contacts/{contact.id}/photo'}]
                     if contact.avatar_path else [])
            return jsonify({'ok': True, 'items': items})
        items = [{'photo_id': p['id'],
                  'url': f'/contacts/{contact.id}/avatar/{p["id"]}'}
                 for p in photos if p.get('id')]
        # Фолбэк: если TG ничего не отдал (например, фото скрыты), но
        # локально у нас фото есть — покажем хотя бы его.
        if not items and contact.avatar_path:
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
        from data.crypto import encrypt_bytes, decrypt_bytes
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
            os.makedirs(os.path.dirname(cache_full), exist_ok=True)
            with open(cache_full, 'wb') as f:
                f.write(encrypt_bytes(data))
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
            return 'Not Found', 404
        with open(full, 'rb') as f:
            raw = decrypt_bytes(f.read())
        return Response(raw, mimetype='image/jpeg')

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
        tg_handle = _telegram_reply_handle(handles)
        typing = False
        if tg_handle is not None:
            try:
                typing = telegram_bridge.is_typing(tg_handle.tg_chat_id)
            except Exception:  # noqa: BLE001
                typing = False
        return jsonify({'typing': bool(typing)})

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
            return jsonify({'is_group': False, 'members': []})
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
        """Беззвучный режим: контакт остаётся в списке (и бэйджи считаются),
        но браузерные пуши и звуковой сигнал отключаются."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.contacts import Contact
        db = get_db()
        contact = db.query(Contact).filter(
            Contact.id == contact_id,
            Contact.user_id == session['user_id']).first()
        if not contact:
            return jsonify({'error': 'not_found'}), 404
        contact.muted = not bool(contact.muted)
        db.commit()
        return jsonify({'ok': True, 'muted': bool(contact.muted)})

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
        msgs_count = (db.query(Messages)
                      .filter(Messages.handle_id.in_([h.id for h in handles]
                                                     or [0]))
                      .count()) if handles else 0
        is_group = any(getattr(h, 'tg_chat_type', None) == 'group'
                       or getattr(h, 'tg_chat_type', None) == 'channel'
                       for h in handles)
        return jsonify({
            'ok': True,
            'contact': {
                'id': contact.id,
                'display_name': contact.display_name,
                'avatar_url': (f'/contacts/{contact.id}/photo'
                               if contact.avatar_path else None),
                'pinned': contact.pinned_at is not None,
                'muted': bool(contact.muted),
                'blocked': contact.blocked_at is not None,
                'messages_count': int(msgs_count),
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
        rows = (db.query(Attachment)
                .join(Messages, Attachment.message_id == Messages.id)
                .join(MessengerHandle,
                      Messages.handle_id == MessengerHandle.id)
                .filter(MessengerHandle.contact_id == contact.id,
                        Attachment.user_id == user_id,
                        Attachment.kind.in_(kinds))
                .order_by(Attachment.id.desc())
                .limit(limit).offset(offset).all())
        return jsonify({
            'ok': True, 'kind': bucket,
            'items': [{
                'id': a.id,
                'kind': a.kind,
                'mime': a.mime,
                'name': a.original_name,
                'size': a.size,
                'created_at': (a.created_at.isoformat()
                               if a.created_at else None),
                'message_id': a.message_id,
                'url': f'/attachments/{a.id}',
            } for a in rows],
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
        from data.contacts import MessengerHandle
        if msg.handle_id is None:
            return None
        h = db.query(MessengerHandle).filter(
            MessengerHandle.id == msg.handle_id).first()
        return h.tg_chat_id if h is not None else None

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
        db.delete(msg)
        db.commit()
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
        и отчитывается в `/api/replies/<id>/done`."""
        from data.pending_replies import (PendingReply, STATUS_PENDING,
                                          STATUS_PICKED)
        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return jsonify({'error': 'unauthorized'}), 401
        device.last_seen_ip = request.remote_addr
        device.last_seen_at = datetime.now()

        items = (db.query(PendingReply)
                 .filter(PendingReply.user_id == device.user_id,
                         PendingReply.status == STATUS_PENDING)
                 .order_by(PendingReply.id.asc()).all())
        now = datetime.now()
        out = []
        for it in items:
            it.status = STATUS_PICKED
            it.picked_up_at = now
            it.device_id = device.id
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
        from data.pending_replies import (PendingReply, STATUS_SENT,
                                          STATUS_FAILED)
        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return jsonify({'error': 'unauthorized'}), 401

        pr = db.query(PendingReply).filter(
            PendingReply.id == reply_id,
            PendingReply.user_id == device.user_id).first()
        if pr is None:
            return jsonify({'error': 'not_found'}), 404

        body = request.get_json(silent=True) or {}
        ok = bool(body.get('ok'))
        error = body.get('error') or None
        now = datetime.now()

        if ok:
            handle = db.query(MessengerHandle).filter(
                MessengerHandle.id == pr.handle_id).first()
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
            )
            db.add(msg)
            pr.status = STATUS_SENT
            pr.sent_at = now
            pr.error = None
        else:
            pr.status = STATUS_FAILED
            pr.error = (error or '')[:200]

        db.commit()
        return jsonify({'ok': True})

    @app.route('/api/pending_replies/<int:reply_id>/status', methods=['GET'])
    def api_pending_reply_status(reply_id):
        """Веб-панель опрашивает, чем кончилась попытка отправки."""
        if not session.get('user_id'):
            return jsonify({'error': 'unauthorized'}), 401
        from data.pending_replies import PendingReply
        db = get_db()
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
        return render_template('download.html')

    @app.route('/download/skillwood.apk')
    def download_apk():
        candidates = [
            os.path.join(os.getcwd(), 'dist'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dist'),
        ]
        for d in candidates:
            if os.path.exists(os.path.join(d, 'skillwood.apk')):
                return send_from_directory(
                    d, 'skillwood.apk',
                    as_attachment=True,
                    mimetype='application/vnd.android.package-archive',
                )
        abort(404)

    @app.route('/add', methods=['POST'])
    def add_message():
        from data.contacts import record_message

        sender = request.form.get('sender')
        text_value = request.form.get('text')
        messenger_name = request.form.get('messenger_name')
        package_name = (request.form.get('package_name') or '').strip() or None

        if not sender or not text_value or not messenger_name:
            return 'Bad Request', 400

        db = get_db()
        device = _device_from_bearer(db)
        if device is None:
            return 'Unauthorized', 401
        device.last_seen_ip = request.remote_addr
        device.last_seen_at = datetime.now()
        db.commit()

        record_message(db, device.user_id, messenger_name, sender, text_value,
                       package_name=package_name)
        return 'OK', 200

    @app.route('/add_media', methods=['POST'])
    def add_media():
        # Приём медиа от Android-клиента
        from sqlalchemy.exc import IntegrityError

        from data.attachments import Attachment
        from data.contacts import find_or_create_handle
        from data.crypto import encrypt_bytes

        sender = request.form.get('sender')
        messenger_name = request.form.get('messenger_name')
        kind = request.form.get('kind') or 'image'
        dedup_key = (request.form.get('dedup_key') or '').strip() or None
        caption = request.form.get('text') or ''
        package_name = (request.form.get('package_name') or '').strip() or None
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

        now = datetime.now()
        handle = find_or_create_handle(db, user_id, messenger_name, sender,
                                       package_name=package_name)
        placeholder = {'image': '📷 Фото',
                       'sticker': '🩷 Стикер',
                       'video': '🎬 Видео'}.get(kind, '📎 Вложение')
        msg = Messages(
            sender=sender,
            text=caption or placeholder,
            messenger_name=messenger_name,
            time=now.strftime("%H:%M"),
            user_id=user_id,
            handle_id=handle.id,
            created_at=now,
        )
        db.add(msg)
        db.flush()

        rel_dir = str(user_id)
        os.makedirs(os.path.join(_media_root(), rel_dir), exist_ok=True)
        stored_name = uuid.uuid4().hex + '.enc'
        stored_path = f"{rel_dir}/{stored_name}"
        with open(os.path.join(_media_root(), stored_path), 'wb') as f:
            f.write(encrypt_bytes(data))

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
        return 'OK', 200

    @app.route('/attachments/<int:attachment_id>')
    def attachment_get(attachment_id):
        if not session.get('user_id'):
            return 'Unauthorized', 401
        from data.attachments import Attachment
        from data.crypto import decrypt_bytes
        db = get_db()
        att = (db.query(Attachment)
               .filter(Attachment.id == attachment_id,
                       Attachment.user_id == session['user_id']).first())
        if att is None:
            return 'Not Found', 404
        full = os.path.join(_media_root(), att.stored_path)
        if not os.path.exists(full):
            return 'Not Found', 404
        with open(full, 'rb') as f:
            raw = decrypt_bytes(f.read())
        return Response(raw, mimetype=att.mime or 'application/octet-stream')

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


if __name__ == '__main__':
    app = create_app()
    port = int(os.environ.get('PORT', '5000'))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
