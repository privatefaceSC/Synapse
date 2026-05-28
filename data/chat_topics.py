"""Сохранённые «темы чата», извлечённые LLM-ом из переписки.

Полная история тем хранится в БД: один контакт → набор `ChatTopic`.
В отличие от in-memory кэша эти темы переживают перезапуск процесса
и переход в другой чат — пользователь видит их сразу при открытии
переписки как «закреплённые сообщения сверху».

`message_ids` — JSON-массив id всех сообщений, которые LLM отнесла к
этой теме. Используется в UI для подсветки всех bubble-ов выбранной
темы (а не только её начала). Если LLM выдала только start_id —
массив содержит одно значение.

`position` — порядковый номер в текущем наборе (0 = первый в пин-баре).
При новом анализе все старые темы удаляются и пишутся свежие — мы не
накапливаем историю анализов, только текущий снимок.
"""
import datetime
import json

import sqlalchemy

from .db_sessions import SqlAlchemyBase


class ChatTopic(SqlAlchemyBase):
    __tablename__ = "chat_topics"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                           autoincrement=True)
    contact_id = sqlalchemy.Column(sqlalchemy.Integer,
                                   sqlalchemy.ForeignKey("contacts.id"),
                                   nullable=False, index=True)
    # Telegram-форумы: один Contact содержит несколько изолированных тем
    # (Messages.tg_topic_id). LLM-«темы чата» считаются ОТДЕЛЬНО для
    # каждой темы форума, иначе при переключении между темами форума мы
    # бы видели одни и те же закрепы. Для обычных (не форумных) чатов
    # значение NULL.
    topic_id = sqlalchemy.Column(sqlalchemy.BigInteger,
                                 nullable=True, index=True)
    title = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    start_message_id = sqlalchemy.Column(sqlalchemy.Integer,
                                         nullable=False)
    # JSON-массив всех id сообщений темы — для подсветки в ленте.
    message_ids_json = sqlalchemy.Column(sqlalchemy.Text, nullable=True)
    # Порядок в пин-баре (0 — первая показывается).
    position = sqlalchemy.Column(sqlalchemy.Integer, nullable=False,
                                 default=0)
    computed_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                    default=datetime.datetime.now,
                                    nullable=False)
    # На каком объёме анализировалась (для UI и кэш-валидации).
    analyzed_count = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)
    # max(Messages.id) на момент анализа — fingerprint для кэша.
    fingerprint_max_id = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)


def _topic_filter(topic_id):
    """`topic_id == NULL` в SQL никогда не True — для NULL-веток (не-форум
    или «все темы») нужен `IS NULL`. Возвращает готовый SQLAlchemy-фильтр."""
    if topic_id is None:
        return ChatTopic.topic_id.is_(None)
    return ChatTopic.topic_id == int(topic_id)


def get_topics(db, contact_id: int, topic_id=None):
    """Все темы контакта (для форум-чата — конкретной темы форума) в
    порядке position. Парсит message_ids_json."""
    rows = (db.query(ChatTopic)
            .filter(ChatTopic.contact_id == contact_id,
                    _topic_filter(topic_id))
            .order_by(ChatTopic.position.asc(), ChatTopic.id.asc())
            .all())
    out = []
    for r in rows:
        try:
            ids = json.loads(r.message_ids_json) if r.message_ids_json else []
        except (TypeError, ValueError):
            ids = []
        out.append({
            "id": r.id,
            "title": r.title,
            "start_id": r.start_message_id,
            "message_ids": ids,
            "computed_at": r.computed_at,
            "analyzed_count": r.analyzed_count,
        })
    return out


def replace_topics(db, contact_id: int, topic_id, topics: list,
                   analyzed_count: int, fingerprint_max_id: int):
    """Полностью переписать набор тем для пары (contact_id, topic_id).
    `topic_id`=None — обычный (не форумный) чат.
    `topics` — список dict-ов {title, start_id, message_ids}. Position
    проставляется по порядку.

    Удаляем старые ChatTopic'и ТОЛЬКО для этой пары и вставляем свежие —
    закрепы соседних тем форума не трогаем."""
    db.query(ChatTopic).filter(
        ChatTopic.contact_id == contact_id,
        _topic_filter(topic_id)).delete(synchronize_session=False)
    now = datetime.datetime.now()
    tid_value = None if topic_id is None else int(topic_id)
    for pos, t in enumerate(topics):
        ids = t.get("message_ids") or [t.get("start_id")]
        # Гарантируем что start_id есть в message_ids — пригодится для
        # подсветки начала темы наравне со связанными.
        if t.get("start_id") not in ids:
            ids = [t["start_id"]] + ids
        db.add(ChatTopic(
            contact_id=contact_id,
            topic_id=tid_value,
            title=t["title"],
            start_message_id=int(t["start_id"]),
            message_ids_json=json.dumps(ids, ensure_ascii=False),
            position=pos,
            computed_at=now,
            analyzed_count=analyzed_count,
            fingerprint_max_id=int(fingerprint_max_id or 0),
        ))
    db.flush()


def fingerprint(db, contact_id: int, topic_id=None):
    """Возвращает (max_id, count) текущего набора для пары
    (contact_id, topic_id). Используется для решения: пересчитывать
    или брать сохранённое."""
    row = (db.query(ChatTopic.fingerprint_max_id, ChatTopic.analyzed_count)
           .filter(ChatTopic.contact_id == contact_id,
                   _topic_filter(topic_id))
           .order_by(ChatTopic.id.asc()).first())
    if not row:
        return (None, None)
    return (row[0], row[1])


def append_to_topic(db, ct_id: int, new_message_ids: list):
    """Добавить id-ы сообщений в message_ids_json существующей темы.
    Дубликаты не плодим, сортируем по возрастанию (хронология)."""
    row = db.query(ChatTopic).filter(ChatTopic.id == ct_id).first()
    if not row:
        return
    try:
        cur = json.loads(row.message_ids_json) if row.message_ids_json else []
    except (TypeError, ValueError):
        cur = []
    cur_set = set(cur)
    for mid in new_message_ids:
        cur_set.add(int(mid))
    row.message_ids_json = json.dumps(sorted(cur_set), ensure_ascii=False)
    db.flush()


def add_topic(db, contact_id: int, topic_id, title: str, start_id: int,
              message_ids: list, fingerprint_max_id: int):
    """Создать новую тему — НЕ удаляя существующие. Position = max+1.
    Используется в инкрементальном режиме когда LLM нашла новую тему
    среди новых сообщений."""
    max_pos = (db.query(sqlalchemy.func.max(ChatTopic.position))
               .filter(ChatTopic.contact_id == contact_id,
                       _topic_filter(topic_id)).scalar()) or -1
    ids = list(message_ids) or [start_id]
    if start_id not in ids:
        ids = [start_id] + ids
    db.add(ChatTopic(
        contact_id=contact_id,
        topic_id=None if topic_id is None else int(topic_id),
        title=title,
        start_message_id=int(start_id),
        message_ids_json=json.dumps(sorted(set(ids)), ensure_ascii=False),
        position=max_pos + 1,
        computed_at=datetime.datetime.now(),
        analyzed_count=None,
        fingerprint_max_id=int(fingerprint_max_id or 0),
    ))
    db.flush()


def bump_fingerprint(db, contact_id: int, topic_id, new_max_id: int):
    """Обновить fingerprint_max_id у всех тем пары (contact, topic) —
    после инкрементального прохода это значит «мы дошли до этого id»,
    и при следующем запросе кэш будет считаться актуальным."""
    (db.query(ChatTopic)
     .filter(ChatTopic.contact_id == contact_id, _topic_filter(topic_id))
     .update({ChatTopic.fingerprint_max_id: int(new_max_id)},
             synchronize_session=False))
    db.flush()
