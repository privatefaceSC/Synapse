"""Внутренний мессенджер Synapse.

Отдельная от внешних контактов модель: пользователи сайта переписываются
друг с другом напрямую. `DirectMessage` — одно личное сообщение между двумя
пользователями, `DirectAttachment` — вложение (фото/видео/аудио/файл).

«Переписка» с пользователем — это все `DirectMessage`, где пара
(sender_id, recipient_id) совпадает с парой {я, собеседник} в любом порядке.
"""
import datetime

import sqlalchemy
from sqlalchemy import orm

from .crypto import EncryptedText
from .db_sessions import SqlAlchemyBase


class DirectMessage(SqlAlchemyBase):
    __tablename__ = 'direct_messages'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    sender_id = sqlalchemy.Column(sqlalchemy.Integer,
                                  sqlalchemy.ForeignKey("users.id"), nullable=False)
    recipient_id = sqlalchemy.Column(sqlalchemy.Integer,
                                     sqlalchemy.ForeignKey("users.id"), nullable=False)
    text = sqlalchemy.Column(EncryptedText(), nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now, nullable=False)
    # Когда получатель прочитал сообщение. NULL — ещё не прочитано.
    read_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)


class DirectAttachment(SqlAlchemyBase):
    __tablename__ = 'direct_attachments'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    message_id = sqlalchemy.Column(sqlalchemy.Integer,
                                   sqlalchemy.ForeignKey("direct_messages.id"),
                                   nullable=False)
    kind = sqlalchemy.Column(sqlalchemy.String, nullable=False)  # image/video/audio/file
    mime = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    original_name = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    # Относительный путь к зашифрованному файлу внутри media-каталога.
    stored_path = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    size = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now, nullable=False)

    message = orm.relationship("DirectMessage", foreign_keys=[message_id])
