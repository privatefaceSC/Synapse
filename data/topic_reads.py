"""Метка «прочитано до» для каждой темы форум-чата.

В обычном чате `Contact.last_read_at` отвечает за «бэйдж непрочитанного»
напротив контакта. У форум-чата каждая тема — фактически отдельный
суб-чат: открыл одну → она прочитана, остальные темы не должны разом
обнуляться. Поэтому ведём отдельный last_read_at на пару (handle, topic).

Уникальность по `(handle_id, topic_id)`. Если записи ещё нет — все
сообщения темы считаются непрочитанными (как при первом открытии).
"""
import datetime

import sqlalchemy

from .db_sessions import SqlAlchemyBase


class TopicReadState(SqlAlchemyBase):
    __tablename__ = "topic_read_state"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                           autoincrement=True)
    handle_id = sqlalchemy.Column(sqlalchemy.Integer,
                                  sqlalchemy.ForeignKey("messenger_handles.id"),
                                  nullable=False, index=True)
    topic_id = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=False)
    last_read_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                     default=datetime.datetime.now,
                                     nullable=False)

    __table_args__ = (
        sqlalchemy.UniqueConstraint("handle_id", "topic_id",
                                    name="uq_topic_read_handle_topic"),
    )


def mark_topic_read(db, handle_id: int, topic_id: int, read_through=None):
    """Пометить тему прочитанной, но не писать в SQLite на
    каждом polling. ``read_through`` — время самого свежего входящего
    сообщения, которое видит клиент. Возвращает True при изменении."""
    now = datetime.datetime.now()
    state = db.query(TopicReadState).filter(
        TopicReadState.handle_id == handle_id,
        TopicReadState.topic_id == topic_id).first()
    if read_through is None:
        return False
    if state is not None and state.last_read_at is not None:
        if read_through <= state.last_read_at:
            return False
    if state is None:
        db.add(TopicReadState(handle_id=handle_id, topic_id=topic_id,
                              last_read_at=now))
    else:
        state.last_read_at = now
    db.flush()
    return True


def get_read_map(db, handle_ids: list) -> dict:
    """Вернуть {(handle_id, topic_id) -> last_read_at} для пачки handles.
    Используется в forum-topics.json для подсчёта unread per topic."""
    out = {}
    if not handle_ids:
        return out
    for s in (db.query(TopicReadState)
              .filter(TopicReadState.handle_id.in_(handle_ids)).all()):
        out[(s.handle_id, s.topic_id)] = s.last_read_at
    return out
