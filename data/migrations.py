from datetime import datetime

import sqlalchemy

from . import db_sessions
from .contacts import Contact, MergeSuggestion, MessengerHandle
from .crypto import _PREFIX as _ENC_PREFIX, encrypt as _encrypt_text
from .matching import normalize, split_group_sender
from .users import Messages, User


def migrate_to_contacts_v1(db) -> dict:
    contacts_created = 0
    handles_created = 0
    messages_linked = 0

    triples = (
        db.query(Messages.user_id, Messages.messenger_name, Messages.sender)
        .filter(Messages.handle_id.is_(None))
        .distinct()
        .all()
    )

    for user_id, messenger_name, sender in triples:
        if user_id is None or sender is None or messenger_name is None:
            continue

        handle = (
            db.query(MessengerHandle)
            .filter(
                MessengerHandle.user_id == user_id,
                MessengerHandle.messenger_name == messenger_name,
                MessengerHandle.sender_raw == sender,
            )
            .first()
        )
        if handle is None:
            contact = Contact(user_id=user_id, display_name=sender)
            db.add(contact)
            db.flush()
            contacts_created += 1
            handle = MessengerHandle(
                contact_id=contact.id,
                user_id=user_id,
                messenger_name=messenger_name,
                sender_raw=sender,
                sender_normalized=normalize(sender),
            )
            db.add(handle)
            db.flush()
            handles_created += 1

        updated = (
            db.query(Messages)
            .filter(
                Messages.handle_id.is_(None),
                Messages.user_id == user_id,
                Messages.messenger_name == messenger_name,
                Messages.sender == sender,
            )
            .update({
                Messages.handle_id: handle.id,
                Messages.created_at: datetime.now(),
            }, synchronize_session=False)
        )
        messages_linked += updated

    db.commit()
    return {
        "contacts_created": contacts_created,
        "handles_created": handles_created,
        "messages_linked": messages_linked,
    }


def migrate_group_handles_v1(db) -> dict:
    # Склейка handles с общим префиксом в группы. Запускать ПОСЛЕ migrate_to_contacts_v1.
    groups_created = 0
    handles_moved = 0
    contacts_removed = 0

    user_ids = [uid for (uid,) in db.query(User.id).all()]
    for user_id in user_ids:
        handles = (db.query(MessengerHandle)
                   .filter(MessengerHandle.user_id == user_id).all())
        groups: dict[str, list] = {}
        for h in handles:
            parsed = split_group_sender(h.sender_raw)
            if parsed is None:
                continue
            groups.setdefault(parsed[0], []).append(h)

        for prefix, hs in groups.items():
            if len(hs) < 2:
                continue
            contact_ids = {h.contact_id for h in hs}

            existing_group = (db.query(Contact)
                              .filter(Contact.user_id == user_id,
                                      Contact.display_name == prefix).first())

            if (existing_group and len(contact_ids) == 1
                    and next(iter(contact_ids)) == existing_group.id):
                continue

            if existing_group is None:
                group_contact = Contact(user_id=user_id, display_name=prefix)
                db.add(group_contact)
                db.flush()
                target_id = group_contact.id
                groups_created += 1
            else:
                target_id = existing_group.id

            for h in hs:
                if h.contact_id != target_id:
                    h.contact_id = target_id
                    handles_moved += 1
            db.flush()

        user_contacts = db.query(Contact).filter(Contact.user_id == user_id).all()
        for c in user_contacts:
            has_handles = (db.query(MessengerHandle)
                           .filter(MessengerHandle.contact_id == c.id).count())
            if has_handles == 0:
                db.query(MergeSuggestion).filter(
                    MergeSuggestion.status == "pending",
                    MergeSuggestion.target_contact_id == c.id,
                ).update({MergeSuggestion.status: "dismissed"},
                         synchronize_session=False)
                db.delete(c)
                contacts_removed += 1
        db.flush()

    db.commit()
    return {
        "groups_created": groups_created,
        "handles_moved": handles_moved,
        "contacts_removed": contacts_removed,
    }


def migrate_encrypt_messages_v1(db) -> dict:
    # Зашифровать незашифрованный `messages.text` в БД (raw SQL намеренно — иначе двойное шифрование).
    rows = db.execute(sqlalchemy.text("SELECT id, text FROM messages")).fetchall()
    encrypted = 0
    for row_id, text in rows:
        if text is None or text.startswith(_ENC_PREFIX):
            continue
        new_text = _encrypt_text(text)
        db.execute(
            sqlalchemy.text("UPDATE messages SET text = :t WHERE id = :i"),
            {"t": new_text, "i": row_id},
        )
        encrypted += 1
    db.commit()
    return {"encrypted": encrypted}


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "migrate_to_contacts_v1"
    func = globals().get(name)
    if func is None or not callable(func) or not name.startswith("migrate_"):
        print(f"Неизвестная миграция: {name}")
        sys.exit(1)
    db_sessions.global_init("db/blogs.db")
    session = db_sessions.create_session()
    try:
        stats = func(session)
        print(f"Миграция {name} завершена: {stats}")
    finally:
        session.close()
