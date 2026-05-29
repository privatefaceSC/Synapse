import datetime

import sqlalchemy
from sqlalchemy import orm

from .db_sessions import SqlAlchemyBase


class Contact(SqlAlchemyBase):
    __tablename__ = 'contacts'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    user_id = sqlalchemy.Column(sqlalchemy.Integer, sqlalchemy.ForeignKey("users.id"), nullable=False)
    display_name = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime, default=datetime.datetime.now, nullable=False)
    last_read_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    # Относительный путь к зашифрованному фото профиля (из Telegram).
    avatar_path = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    # Закреплённый сверху списка: NULL = не закреплён, иначе момент закрепления
    # (по нему сортируем — более свежие pin'ы выше).
    pinned_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    # Беззвучный режим: True = не показывать браузерные уведомления и не пищать.
    # Бэйдж непрочитанного всё равно остаётся — это «выключить звук», не «не следить».
    muted = sqlalchemy.Column(sqlalchemy.Boolean, default=False, nullable=True)
    # Блокировка: NULL = не заблокирован. Иначе момент блокировки. Если стоит,
    # record_message() молча игнорирует новые входящие от этого контакта —
    # сообщение не сохраняется ни в БД, ни в Telegram оно само собой остаётся
    # (мы лишь не показываем). Старая переписка по-прежнему доступна.
    blocked_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)

    handles = orm.relationship("MessengerHandle", back_populates="contact",
                               foreign_keys="MessengerHandle.contact_id")


class MessengerHandle(SqlAlchemyBase):
    __tablename__ = 'messenger_handles'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    contact_id = sqlalchemy.Column(sqlalchemy.Integer, sqlalchemy.ForeignKey("contacts.id"), nullable=False)
    user_id = sqlalchemy.Column(sqlalchemy.Integer, sqlalchemy.ForeignKey("users.id"), nullable=False)
    messenger_name = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    sender_raw = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    sender_normalized = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime, default=datetime.datetime.now, nullable=False)
    # Telegram chat_id (peer) — заполняется мостом, нужен для отправки
    # ответов из веб-панели. У не-Telegram личностей остаётся NULL.
    tg_chat_id = sqlalchemy.Column(sqlalchemy.BigInteger, nullable=True)
    # Тип Telegram-чата: 'private' / 'group' / 'channel'.
    tg_chat_type = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    # True — Telegram-форум (одна супергруппа с множеством «тем-разделов»).
    # В UI такие чаты показываются особо: при выборе контакта сначала
    # открывается список тем, и только после клика — лента темы.
    tg_is_forum = sqlalchemy.Column(sqlalchemy.Boolean, nullable=True,
                                    default=False)
    # Android-пакет мессенджера (ru.oneme.app, com.whatsapp и т.п.) — нужен,
    # чтобы Android-клиент мог найти соответствующее уведомление в шторке
    # и ответить через RemoteInput. У Telegram-личностей не используется.
    package_name = sqlalchemy.Column(sqlalchemy.String, nullable=True)

    __table_args__ = (
        sqlalchemy.UniqueConstraint('user_id', 'messenger_name', 'sender_raw',
                                    name='uq_handle_user_messenger_sender'),
    )

    contact = orm.relationship("Contact", back_populates="handles", foreign_keys=[contact_id])


def find_or_create_handle(db, user_id: int, messenger_name: str, sender_raw: str,
                          tg_chat_id=None, tg_chat_type=None, package_name=None):
    from .matching import normalize, split_group_sender

    handle = (
        db.query(MessengerHandle)
        .filter(
            MessengerHandle.user_id == user_id,
            MessengerHandle.messenger_name == messenger_name,
            MessengerHandle.sender_raw == sender_raw,
        )
        .first()
    )
    if handle:
        # Дозаполняем chat_id/тип/package_name, если личность была создана
        # раньше (до появления соответствующей функциональности).
        if tg_chat_id is not None and handle.tg_chat_id != tg_chat_id:
            handle.tg_chat_id = tg_chat_id
            db.flush()
        if tg_chat_type is not None and handle.tg_chat_type != tg_chat_type:
            handle.tg_chat_type = tg_chat_type
            db.flush()
        if package_name is not None and handle.package_name != package_name:
            handle.package_name = package_name
            db.flush()
        return handle

    parsed = split_group_sender(sender_raw)
    if parsed is not None:
        prefix, _member = parsed
        all_handles = (
            db.query(MessengerHandle)
            .filter(MessengerHandle.user_id == user_id)
            .all()
        )
        siblings = []
        for h in all_handles:
            if h.sender_raw == sender_raw:
                continue
            sp = split_group_sender(h.sender_raw)
            if sp is not None and sp[0] == prefix:
                siblings.append(h)

        if siblings:
            existing_group = (db.query(Contact)
                              .filter(Contact.user_id == user_id,
                                      Contact.display_name == prefix).first())
            sibling_contact_ids = {h.contact_id for h in siblings}

            if existing_group is not None and sibling_contact_ids == {existing_group.id}:
                group_contact_id = existing_group.id
            else:
                if existing_group is None:
                    group_contact = Contact(user_id=user_id, display_name=prefix)
                    db.add(group_contact)
                    db.flush()
                    group_contact_id = group_contact.id
                else:
                    group_contact_id = existing_group.id

                old_contact_ids = set()
                for h in siblings:
                    if h.contact_id != group_contact_id:
                        old_contact_ids.add(h.contact_id)
                        h.contact_id = group_contact_id
                db.flush()

                for old_id in old_contact_ids:
                    remaining = (db.query(MessengerHandle)
                                 .filter(MessengerHandle.contact_id == old_id).count())
                    if remaining == 0:
                        db.query(Contact).filter(Contact.id == old_id).delete(
                            synchronize_session=False)
                db.flush()

            handle = MessengerHandle(
                contact_id=group_contact_id,
                user_id=user_id,
                messenger_name=messenger_name,
                sender_raw=sender_raw,
                sender_normalized=normalize(sender_raw),
                tg_chat_id=tg_chat_id,
                tg_chat_type=tg_chat_type,
                package_name=package_name,
            )
            db.add(handle)
            db.flush()
            return handle

    contact = Contact(user_id=user_id, display_name=sender_raw)
    db.add(contact)
    db.flush()
    handle = MessengerHandle(
        contact_id=contact.id,
        user_id=user_id,
        messenger_name=messenger_name,
        sender_raw=sender_raw,
        sender_normalized=normalize(sender_raw),
        tg_chat_id=tg_chat_id,
        tg_chat_type=tg_chat_type,
        package_name=package_name,
    )
    db.add(handle)
    db.flush()
    return handle


def record_message(db, user_id: int, messenger_name: str, sender_raw: str, text: str,
                    tg_chat_id=None, author=None, outgoing=False, tg_chat_type=None,
                    tg_message_id=None, reply_to_tg_id=None, package_name=None,
                    tg_ttl_seconds=None, fwd_from_name=None,
                    fwd_from_tg_chat_id=None, tg_topic_id=None,
                    tg_topic_title=None, tg_is_forum=None,
                    text_html=None):
    """Записывает сообщение.

    `sender_raw` — ключ личности (контакта): для лички это имя
    собеседника, для группы/канала — название чата.
    `author` — кто именно написал (для подписи над сообщением). Если
    не задан, совпадает с `sender_raw` — поведение для лички и Android.
    `outgoing` — True, если сообщение отправлено владельцем аккаунта.
    `tg_chat_type` — 'private'/'group'/'channel' для Telegram.
    `reply_to_tg_id` — telegram-id сообщения, на которое это ответ; по
    нему в том же чате ищется наша запись Messages для цитаты.
    """
    import datetime as _dt

    from .users import Messages

    handle = find_or_create_handle(db, user_id, messenger_name, sender_raw,
                                   tg_chat_id=tg_chat_id,
                                   tg_chat_type=tg_chat_type,
                                   package_name=package_name)
    # Контакт заблокирован — молча игнорируем новые входящие. Свои
    # исходящие пропускаем (вдруг разблокировка и сами что-то ответили).
    if not outgoing:
        contact = db.query(Contact).filter(Contact.id == handle.contact_id).first()
        if contact is not None and contact.blocked_at is not None:
            return None
    # Если мост только что узнал, что чат — форум-канал, отметим
    # это на handle. Делается лениво — при первом сообщении.
    if tg_is_forum is not None and bool(handle.tg_is_forum) != bool(tg_is_forum):
        handle.tg_is_forum = bool(tg_is_forum)

    reply_to_message_id = None
    if reply_to_tg_id is not None:
        prior = (db.query(Messages)
                 .filter(Messages.handle_id == handle.id,
                         Messages.tg_message_id == reply_to_tg_id).first())
        if prior is not None:
            reply_to_message_id = prior.id

    now = _dt.datetime.now()
    msg = Messages(
        sender=author if author is not None else sender_raw,
        text=text,
        messenger_name=messenger_name,
        time=now.strftime("%H:%M"),
        user_id=user_id,
        handle_id=handle.id,
        created_at=now,
        outgoing=outgoing,
        tg_message_id=tg_message_id,
        reply_to_message_id=reply_to_message_id,
        tg_ttl_seconds=tg_ttl_seconds,
        fwd_from_name=fwd_from_name,
        fwd_from_tg_chat_id=fwd_from_tg_chat_id,
        tg_topic_id=tg_topic_id,
        tg_topic_title=tg_topic_title,
        text_html=text_html,
    )
    db.add(msg)
    db.commit()
    return msg


def merge_contacts(db, user_id: int, source_id: int, target_id: int) -> None:
    if source_id == target_id:
        raise ValueError("same")
    src = db.query(Contact).filter(Contact.id == source_id, Contact.user_id == user_id).first()
    tgt = db.query(Contact).filter(Contact.id == target_id, Contact.user_id == user_id).first()
    if not src or not tgt:
        raise LookupError()

    db.query(MessengerHandle).filter(MessengerHandle.contact_id == source_id).update(
        {MessengerHandle.contact_id: target_id}, synchronize_session=False
    )

    db.delete(src)
    db.commit()
