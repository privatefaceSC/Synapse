"""Реакции на сообщения (Telegram-style).

В одну строку хранится одна (message_id, emoji) пара. Колонка `count` —
сколько участников чата поставили этот эмодзи (для группы); для лички это
обычно 0 или 1. Флаг `mine` — стоит ли эта реакция от моего имени.

Источник состояния — UpdateMessageReactions из Telethon: при каждом событии
мы перезаписываем все реакции конкретного сообщения. То есть таблица — это
снимок, не журнал.
"""
import datetime

import sqlalchemy

from .db_sessions import SqlAlchemyBase


class MessageReaction(SqlAlchemyBase):
    __tablename__ = "message_reactions"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    message_id = sqlalchemy.Column(sqlalchemy.Integer,
                                   sqlalchemy.ForeignKey("messages.id"),
                                   nullable=False, index=True)
    emoji = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    count = sqlalchemy.Column(sqlalchemy.Integer, nullable=False, default=0)
    mine = sqlalchemy.Column(sqlalchemy.Boolean, nullable=False, default=False)
    updated_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now, nullable=False)

    __table_args__ = (
        sqlalchemy.UniqueConstraint("message_id", "emoji",
                                    name="uq_reaction_msg_emoji"),
    )


def replace_reactions(db, message_id: int, items):
    """Полностью заменить набор реакций у сообщения. items — список dict-ов:
    [{'emoji': '👍', 'count': 2, 'mine': True}, ...]."""
    db.query(MessageReaction).filter(
        MessageReaction.message_id == message_id).delete(
        synchronize_session=False)
    for it in items:
        emoji = it.get("emoji")
        if not emoji:
            continue
        db.add(MessageReaction(
            message_id=message_id,
            emoji=emoji,
            count=int(it.get("count") or 0),
            mine=bool(it.get("mine")),
        ))
    db.flush()
