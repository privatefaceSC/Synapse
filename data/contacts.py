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

    __table_args__ = (
        sqlalchemy.UniqueConstraint('user_id', 'messenger_name', 'sender_raw',
                                    name='uq_handle_user_messenger_sender'),
    )

    contact = orm.relationship("Contact", back_populates="handles", foreign_keys=[contact_id])


class MergeSuggestion(SqlAlchemyBase):
    __tablename__ = 'merge_suggestions'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    user_id = sqlalchemy.Column(sqlalchemy.Integer, sqlalchemy.ForeignKey("users.id"), nullable=False)
    source_handle_id = sqlalchemy.Column(sqlalchemy.Integer,
                                         sqlalchemy.ForeignKey("messenger_handles.id"), nullable=False)
    target_contact_id = sqlalchemy.Column(sqlalchemy.Integer,
                                          sqlalchemy.ForeignKey("contacts.id"), nullable=False)
    score = sqlalchemy.Column(sqlalchemy.Float, nullable=False)
    status = sqlalchemy.Column(sqlalchemy.String, nullable=False, default="pending")
    created_at = sqlalchemy.Column(sqlalchemy.DateTime, default=datetime.datetime.now, nullable=False)

    __table_args__ = (
        sqlalchemy.UniqueConstraint('source_handle_id', 'target_contact_id',
                                    name='uq_suggestion_handle_contact'),
    )

    source_handle = orm.relationship("MessengerHandle", foreign_keys=[source_handle_id])
    target_contact = orm.relationship("Contact", foreign_keys=[target_contact_id])


def find_or_create_handle(db, user_id: int, messenger_name: str, sender_raw: str):
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

                moved_handle_ids = []
                old_contact_ids = set()
                for h in siblings:
                    if h.contact_id != group_contact_id:
                        old_contact_ids.add(h.contact_id)
                        h.contact_id = group_contact_id
                        moved_handle_ids.append(h.id)
                db.flush()

                for old_id in old_contact_ids:
                    remaining = (db.query(MessengerHandle)
                                 .filter(MessengerHandle.contact_id == old_id).count())
                    if remaining == 0:
                        conditions = [MergeSuggestion.target_contact_id == old_id]
                        if moved_handle_ids:
                            conditions.append(MergeSuggestion.source_handle_id.in_(moved_handle_ids))
                        db.query(MergeSuggestion).filter(
                            MergeSuggestion.status == "pending",
                            sqlalchemy.or_(*conditions),
                        ).update({MergeSuggestion.status: "dismissed"},
                                 synchronize_session=False)
                        db.query(Contact).filter(Contact.id == old_id).delete(
                            synchronize_session=False)
                db.flush()

            handle = MessengerHandle(
                contact_id=group_contact_id,
                user_id=user_id,
                messenger_name=messenger_name,
                sender_raw=sender_raw,
                sender_normalized=normalize(sender_raw),
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
    )
    db.add(handle)
    db.flush()
    return handle


def record_message(db, user_id: int, messenger_name: str, sender_raw: str, text: str):
    import datetime as _dt

    from .users import Messages

    handle = find_or_create_handle(db, user_id, messenger_name, sender_raw)
    now = _dt.datetime.now()
    msg = Messages(
        sender=sender_raw,
        text=text,
        messenger_name=messenger_name,
        time=now.strftime("%H:%M"),
        user_id=user_id,
        handle_id=handle.id,
        created_at=now,
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

    src_handle_ids = [h.id for h in
                      db.query(MessengerHandle).filter(MessengerHandle.contact_id == source_id).all()]

    db.query(MessengerHandle).filter(MessengerHandle.contact_id == source_id).update(
        {MessengerHandle.contact_id: target_id}, synchronize_session=False
    )

    conditions = [MergeSuggestion.target_contact_id == source_id]
    if src_handle_ids:
        conditions.append(MergeSuggestion.source_handle_id.in_(src_handle_ids))
    db.query(MergeSuggestion).filter(
        MergeSuggestion.status == "pending",
        sqlalchemy.or_(*conditions),
    ).update({MergeSuggestion.status: "dismissed"}, synchronize_session=False)

    db.delete(src)
    db.commit()
