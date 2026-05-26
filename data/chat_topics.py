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


def get_topics(db, contact_id: int):
    """Все темы контакта в порядке position. Парсит message_ids_json."""
    rows = (db.query(ChatTopic)
            .filter(ChatTopic.contact_id == contact_id)
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


def replace_topics(db, contact_id: int, topics: list, analyzed_count: int,
                   fingerprint_max_id: int):
    """Полностью переписать набор тем контакта. `topics` — список dict-ов
    {title, start_id, message_ids}. Position проставляется по порядку.

    Удаляем старые ChatTopic'и контакта и вставляем свежие — мы не
    ведём историю анализов, только текущий снимок."""
    db.query(ChatTopic).filter(
        ChatTopic.contact_id == contact_id).delete(
        synchronize_session=False)
    now = datetime.datetime.now()
    for pos, t in enumerate(topics):
        ids = t.get("message_ids") or [t.get("start_id")]
        # Гарантируем что start_id есть в message_ids — пригодится для
        # подсветки начала темы наравне со связанными.
        if t.get("start_id") not in ids:
            ids = [t["start_id"]] + ids
        db.add(ChatTopic(
            contact_id=contact_id,
            title=t["title"],
            start_message_id=int(t["start_id"]),
            message_ids_json=json.dumps(ids, ensure_ascii=False),
            position=pos,
            computed_at=now,
            analyzed_count=analyzed_count,
            fingerprint_max_id=int(fingerprint_max_id or 0),
        ))
    db.flush()


def fingerprint(db, contact_id: int):
    """Возвращает (max_id, count) текущего набора. Используется для
    решения: пересчитывать или брать сохранённое."""
    row = (db.query(ChatTopic.fingerprint_max_id, ChatTopic.analyzed_count)
           .filter(ChatTopic.contact_id == contact_id)
           .order_by(ChatTopic.id.asc()).first())
    if not row:
        return (None, None)
    return (row[0], row[1])
