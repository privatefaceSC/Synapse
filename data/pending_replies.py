"""Очередь отложенных ответов «через шторку» Android.

Веб-панель кладёт сюда задачу «отправить вот этот текст в этот чат через
notification reply на Android». Android-клиент периодически опрашивает,
находит соответствующий `StatusBarNotification` в шторке, отправляет ответ
через `RemoteInput` и отчитывается обратно. Сервер на успехе создаёт обычную
запись `Messages` с `outgoing=True`, и она проявляется в ленте.
"""
import datetime

import sqlalchemy

from .crypto import EncryptedText
from .db_sessions import SqlAlchemyBase


# Статусы жизненного цикла задачи.
STATUS_PENDING = "pending"      # ждёт Android
STATUS_PICKED = "picked"        # Android забрал в работу
STATUS_SENT = "sent"            # успешно отправлено
STATUS_FAILED = "failed"        # отправить не удалось (нет активного уведомления и т.п.)
STATUS_EXPIRED = "expired"      # протухло (не забрали за окно)


class PendingReply(SqlAlchemyBase):
    __tablename__ = "pending_replies"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    user_id = sqlalchemy.Column(sqlalchemy.Integer,
                                sqlalchemy.ForeignKey("users.id"), nullable=False)
    handle_id = sqlalchemy.Column(sqlalchemy.Integer,
                                  sqlalchemy.ForeignKey("messenger_handles.id"),
                                  nullable=False)
    # Что отправить (шифруется как обычный текст сообщения).
    text = sqlalchemy.Column(EncryptedText(), nullable=False)
    # Куда — копия из хэндла на момент постановки, чтобы Android матчил SBN.
    package_name = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    sender_label = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    # Жизненный цикл.
    status = sqlalchemy.Column(sqlalchemy.String, nullable=False,
                               default=STATUS_PENDING)
    error = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now, nullable=False)
    picked_up_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    sent_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    # Какое устройство отработало задачу (для диагностики).
    device_id = sqlalchemy.Column(sqlalchemy.Integer,
                                  sqlalchemy.ForeignKey("devices.id"), nullable=True)
    # На какое наше Messages это ответ (для quote-цитаты как в Telegram).
    reply_to_message_id = sqlalchemy.Column(
        sqlalchemy.Integer, sqlalchemy.ForeignKey("messages.id"), nullable=True)
