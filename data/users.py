import datetime

import sqlalchemy

from .crypto import EncryptedText
from .db_sessions import SqlAlchemyBase


class User(SqlAlchemyBase):
    __tablename__ = 'users'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    surname = sqlalchemy.Column(sqlalchemy.String)
    name = sqlalchemy.Column(sqlalchemy.String)
    sex = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    email = sqlalchemy.Column(sqlalchemy.String, unique=True)
    hashed_password = sqlalchemy.Column(sqlalchemy.String)
    modified_date = sqlalchemy.Column(sqlalchemy.DateTime, default=datetime.datetime.now)
    connect_code = sqlalchemy.Column(sqlalchemy.String, nullable=True, unique=True)
    # Публичный User ID (как @username в Telegram) — по нему пользователя
    # находят во внутреннем мессенджере. Уникальный, латиница/цифры/«_».
    username = sqlalchemy.Column(sqlalchemy.String, nullable=True, unique=True)


class Messages(SqlAlchemyBase):
    __tablename__ = 'messages'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    sender = sqlalchemy.Column(sqlalchemy.String)
    text = sqlalchemy.Column(EncryptedText())
    messenger_name = sqlalchemy.Column(sqlalchemy.String)
    time = sqlalchemy.Column(sqlalchemy.String)
    user_id = sqlalchemy.Column(sqlalchemy.Integer, sqlalchemy.ForeignKey("users.id"), nullable=True)
    handle_id = sqlalchemy.Column(sqlalchemy.Integer,
                                  sqlalchemy.ForeignKey("messenger_handles.id"), nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime, default=datetime.datetime.now, nullable=True)
    # True — сообщение отправлено владельцем из веб-панели (исходящее).
    outgoing = sqlalchemy.Column(sqlalchemy.Boolean, default=False, nullable=True)
    # id сообщения в Telegram — нужен для удаления/редактирования.
    tg_message_id = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=True)
    # Когда собеседник в Telegram прочитал это исходящее сообщение (приходит
    # из UpdateReadHistoryOutbox). NULL = ещё не прочитано (одна галочка
    # в UI). Не NULL = прочитано (двойная галочка).
    tg_read_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    # Когда автор удалил это сообщение в самом мессенджере (Telegram присылает
    # UpdateDeleteMessages/UpdateDeleteChannelMessages). У нас сообщение
    # остаётся в БД, но в ленте показывается как «🗑 Сообщение удалено».
    deleted_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    # У Telegram-сообщений с автоудалением (one-shot photo/video, ttl_seconds)
    # запоминаем — для пометки «🔥 одноразовое» в UI. Само медиа мы уже
    # успели скачать в _handle_message, файл остаётся даже после ttl.
    tg_ttl_seconds = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)
    # id сообщения (в этой же таблице), на которое это — ответ (reply).
    reply_to_message_id = sqlalchemy.Column(
        sqlalchemy.Integer, sqlalchemy.ForeignKey("messages.id"), nullable=True)
    # Для пересланных (forward) сообщений из Telegram: имя оригинального
    # автора (как Telegram отдаёт в `Message.fwd_from`) и его telegram-chat_id.
    # chat_id используем чтобы при клике на «Переслано от …» в UI перейти к
    # нашему Contact, у которого есть MessengerHandle с таким tg_chat_id.
    # У «скрытых» пересылок (приватные настройки автора) chat_id=NULL,
    # тогда ник остаётся некликабельным.
    fwd_from_name = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    fwd_from_tg_chat_id = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=True)
    # Для forum-чатов Telegram (одна группа → много тем): id «головного»
    # сообщения темы. Telegram кладёт его в `Message.reply_to.reply_to_top_id`
    # для всех сообщений темы; само головное сообщение имеет
    # `tg_message_id == tg_topic_id`. Сообщения из обычных групп — NULL.
    tg_topic_id = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=True)
    # Название темы (только для «головного» сообщения темы) — Telegram
    # кладёт его в `MessageActionTopicCreate.title`. У остальных
    # сообщений темы поле остаётся NULL — мы достаём название из головы.
    tg_topic_title = sqlalchemy.Column(sqlalchemy.String, nullable=True)
