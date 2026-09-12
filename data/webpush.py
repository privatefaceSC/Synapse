"""Web Push для фоновых уведомлений браузера.

Service Worker получает payload даже когда вкладка закрыта или телефон
заблокирован. Сервер хранит PushSubscription и отправляет сообщение через
push-сервис браузера (FCM/Mozilla/etc.) с VAPID-авторизацией.
"""

import base64
import datetime
import json
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _db_dir():
    return os.path.join(os.getcwd(), "db")


def _vapid_private_path():
    return os.path.join(_db_dir(), "webpush_vapid_private.pem")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _load_or_create_private_key():
    env_key = os.environ.get("WEB_PUSH_VAPID_PRIVATE_KEY")
    if env_key:
        return serialization.load_pem_private_key(
            env_key.encode("utf-8"), password=None)

    path = _vapid_private_path()
    if os.path.exists(path):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(path, "wb") as f:
        f.write(pem)
    return key


def vapid_public_key() -> str:
    env_public = os.environ.get("WEB_PUSH_VAPID_PUBLIC_KEY")
    if env_public:
        return env_public.strip()
    key = _load_or_create_private_key()
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return _b64url(raw)


def _vapid_private_for_pywebpush():
    return os.environ.get("WEB_PUSH_VAPID_PRIVATE_KEY") or _vapid_private_path()


def _vapid_claims():
    sub = (os.environ.get("WEB_PUSH_SUBJECT")
           or os.environ.get("WEB_PUSH_VAPID_SUBJECT")
           or "mailto:synapse@example.invalid")
    return {"sub": sub}


def _subscription_info(sub):
    return {
        "endpoint": sub.endpoint,
        "keys": {
            "p256dh": sub.p256dh,
            "auth": sub.auth,
        },
    }


def save_subscription(db, user_id: int, payload: dict, user_agent=None):
    from data.webpush_subscriptions import WebPushSubscription

    subscription = payload.get("subscription") if "subscription" in payload else payload
    if not isinstance(subscription, dict):
        raise ValueError("bad_subscription")
    endpoint = (subscription.get("endpoint") or "").strip()
    keys = subscription.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise ValueError("bad_subscription")

    sub = db.query(WebPushSubscription).filter(
        WebPushSubscription.endpoint == endpoint).first()
    now = datetime.datetime.now()
    if sub is None:
        sub = WebPushSubscription(endpoint=endpoint, created_at=now)
        db.add(sub)
    sub.user_id = user_id
    sub.p256dh = p256dh
    sub.auth = auth
    sub.user_agent = (user_agent or "")[:500] or None
    sub.enabled = True
    sub.updated_at = now
    sub.failed_at = None
    sub.last_error = None
    db.commit()
    return sub


def disable_subscription(db, user_id: int, endpoint: str):
    from data.webpush_subscriptions import WebPushSubscription

    sub = (db.query(WebPushSubscription)
           .filter(WebPushSubscription.user_id == user_id,
                   WebPushSubscription.endpoint == endpoint)
           .first())
    if sub is None:
        return False
    sub.enabled = False
    sub.updated_at = datetime.datetime.now()
    db.commit()
    return True


def _send_subscription_payload(sub, payload: str):
    from pywebpush import webpush

    return webpush(
        subscription_info=_subscription_info(sub),
        data=payload,
        vapid_private_key=_vapid_private_for_pywebpush(),
        vapid_claims=_vapid_claims(),
        ttl=24 * 60 * 60,
        timeout=5,
    )


def _message_payload(db, msg):
    from data.contacts import Contact, MessengerHandle

    if msg is None or bool(msg.outgoing):
        return None
    handle = db.query(MessengerHandle).filter(
        MessengerHandle.id == msg.handle_id).first()
    if handle is None:
        return None
    contact = db.query(Contact).filter(Contact.id == handle.contact_id).first()
    if contact is None or bool(contact.muted):
        return None

    contact_name = contact.display_name or msg.sender or "Сообщение"
    sender = msg.sender or contact_name
    if sender and sender not in ("Вы", contact_name):
        title = f"{sender} — {contact_name}"
    else:
        title = contact_name
    text = msg.text or ""
    if len(text) > 180:
        text = text[:177].rstrip() + "..."
    return {
        "title": title,
        "body": text,
        "tag": f"synapse-contact-{contact.id}",
        "url": f"/contacts/{contact.id}",
        "contact_id": contact.id,
        "message_id": msg.id,
    }


def notify_message(message_id: int) -> dict:
    """Отправить push по сохранённому входящему сообщению."""
    from data import db_sessions
    from data.users import Messages
    from data.webpush_subscriptions import WebPushSubscription

    db = db_sessions.create_session()
    sent = 0
    failed = 0
    disabled = 0
    try:
        msg = db.query(Messages).filter(Messages.id == message_id).first()
        payload = _message_payload(db, msg)
        if payload is None:
            return {"sent": 0, "failed": 0, "disabled": 0}
        subs = (db.query(WebPushSubscription)
                .filter(WebPushSubscription.user_id == msg.user_id,
                        WebPushSubscription.enabled.is_(True))
                .all())
        data = json.dumps(payload, ensure_ascii=False)
        for sub in subs:
            try:
                _send_subscription_payload(sub, data)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                code = getattr(exc, "status_code", None)
                sub.last_error = str(exc)[:500]
                sub.failed_at = datetime.datetime.now()
                if code in (404, 410):
                    sub.enabled = False
                    disabled += 1
        if failed or disabled:
            db.commit()
        return {"sent": sent, "failed": failed, "disabled": disabled}
    finally:
        db.close()


def notify_direct_message(db, direct_msg, recipient_id: int,
                          sender_name: str | None = None) -> dict:
    """Push для внутреннего Synapse до ленивого зеркалирования в Messages."""
    from data.contacts import Contact, MessengerHandle
    from data.webpush_subscriptions import WebPushSubscription

    handle = (db.query(MessengerHandle)
              .filter(MessengerHandle.user_id == recipient_id,
                      MessengerHandle.messenger_name == "Synapse",
                      MessengerHandle.sender_raw == f"synapse:{direct_msg.sender_id}")
              .first())
    contact = (db.query(Contact).filter(Contact.id == handle.contact_id).first()
               if handle is not None else None)
    if contact is not None and bool(contact.muted):
        return {"sent": 0, "failed": 0, "disabled": 0}
    title = sender_name or (contact.display_name if contact else "Synapse")
    body = direct_msg.text or "Сообщение"
    payload = {
        "title": title,
        "body": body[:177].rstrip() + "..." if len(body) > 180 else body,
        "tag": f"synapse-direct-{direct_msg.sender_id}",
        "url": "/contacts",
        "contact_id": contact.id if contact else None,
        "message_id": None,
    }
    subs = (db.query(WebPushSubscription)
            .filter(WebPushSubscription.user_id == recipient_id,
                    WebPushSubscription.enabled.is_(True))
            .all())
    sent = 0
    failed = 0
    data = json.dumps(payload, ensure_ascii=False)
    for sub in subs:
        try:
            _send_subscription_payload(sub, data)
            sent += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            sub.last_error = str(exc)[:500]
            sub.failed_at = datetime.datetime.now()
    if failed:
        db.commit()
    return {"sent": sent, "failed": failed, "disabled": 0}
