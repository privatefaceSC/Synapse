"""Web Push для фоновых уведомлений браузера.

Service Worker получает payload даже когда вкладка закрыта или телефон
заблокирован. Сервер хранит PushSubscription и отправляет сообщение через
push-сервис браузера (FCM/Mozilla/etc.) с VAPID-авторизацией.
"""

import base64
import datetime
import json
import os
from urllib.parse import urlparse

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


def _vapid_subject(origin: str | None = None) -> str:
    configured = (os.environ.get("WEB_PUSH_SUBJECT")
                  or os.environ.get("WEB_PUSH_VAPID_SUBJECT")
                  or "").strip()
    if configured:
        return configured
    public_origin = _public_origin(origin)
    if public_origin.startswith("https://"):
        return public_origin
    return "mailto:admin@example.com"


def _vapid_claims(origin: str | None = None):
    sub = _vapid_subject(origin)
    return {"sub": sub}


def _subscription_info(sub):
    return {
        "endpoint": sub.endpoint,
        "keys": {
            "p256dh": sub.p256dh,
            "auth": sub.auth,
        },
    }


def _clean_origin(value: str | None) -> str | None:
    value = (value or "").strip().rstrip("/")
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


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
    sub.origin = (_clean_origin(payload.get("origin"))
                  or _clean_origin(getattr(sub, "origin", None)))
    sub.user_agent = (user_agent or "")[:500] or None
    sub.enabled = True
    sub.updated_at = now
    sub.failed_at = None
    sub.last_error = None
    if sub.user_agent:
        stale_subs = (db.query(WebPushSubscription)
                      .filter(WebPushSubscription.user_id == user_id,
                              WebPushSubscription.endpoint != endpoint,
                              WebPushSubscription.user_agent == sub.user_agent,
                              WebPushSubscription.enabled.is_(True))
                      .all())
        for stale in stale_subs:
            stale_origin = _clean_origin(getattr(stale, "origin", None))
            if stale_origin and sub.origin and stale_origin != sub.origin:
                continue
            stale.enabled = False
            stale.updated_at = now
            stale.failed_at = None
            stale.last_error = None
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
        vapid_claims=_vapid_claims(getattr(sub, "origin", None)),
        ttl=24 * 60 * 60,
        timeout=5,
    )


def _endpoint_label(endpoint: str) -> str:
    host = urlparse(endpoint or "").netloc
    return host or "push-сервис"


def _first_header_value(value: str | None) -> str:
    return (value or "").split(",", 1)[0].strip()


def _public_origin(origin: str | None = None) -> str:
    clean = _clean_origin(origin)
    if clean:
        return clean
    origin = (os.environ.get("WEB_PUSH_ORIGIN")
              or os.environ.get("SKILLWOOD_PUBLIC_ORIGIN")
              or "").strip().rstrip("/")
    clean = _clean_origin(origin)
    if clean:
        return clean
    try:
        from flask import has_request_context, request
        if has_request_context():
            proto = (_first_header_value(
                request.headers.get("X-Forwarded-Proto")) or request.scheme)
            host = (_first_header_value(
                request.headers.get("X-Forwarded-Host")) or request.host)
            return _clean_origin(f"{proto}://{host}") or request.url_root.rstrip("/")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _absolute_notification_url(url: str | None, origin: str | None = None) -> str:
    url = (url or "/contacts").strip() or "/contacts"
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return url
    if not url.startswith("/"):
        url = "/" + url
    origin = _public_origin(origin)
    return f"{origin}{url}" if origin else url


def _wire_payload(payload: dict, origin: str | None = None) -> dict:
    """Payload одновременно для Declarative Web Push и старого SW-формата."""
    title = str(payload.get("title") or "Synapse").strip() or "Synapse"
    body = str(payload.get("body") or "Новое сообщение")
    url = str(payload.get("url") or payload.get("navigate") or "/contacts")
    notification = {
        "title": title,
        "body": body,
        "navigate": _absolute_notification_url(url, origin=origin),
        "silent": False,
    }
    tag = payload.get("tag")
    if tag:
        notification["tag"] = str(tag)
    app_badge = payload.get("app_badge")
    if app_badge is not None:
        notification["app_badge"] = str(app_badge)

    compat = dict(payload)
    compat.update({
        "web_push": 8030,
        "notification": notification,
        "title": title,
        "body": body,
        "url": url,
        "navigate": notification["navigate"],
        "vapid_subject": _vapid_subject(origin),
    })
    if tag:
        compat["tag"] = str(tag)
    return compat


def subscription_status(db, user_id: int) -> dict:
    """Короткая диагностика push-подписок текущего пользователя."""
    from data.webpush_subscriptions import WebPushSubscription

    subs = (db.query(WebPushSubscription)
            .filter(WebPushSubscription.user_id == user_id)
            .order_by(WebPushSubscription.updated_at.desc().nullslast(),
                      WebPushSubscription.id.desc())
            .all())

    def iso(value):
        return value.isoformat(timespec="seconds") if value else None

    return {
        "subscriptions_total": len(subs),
        "subscriptions_active": sum(1 for s in subs if bool(s.enabled)),
        "subscriptions": [
            {
                "id": s.id,
                "enabled": bool(s.enabled),
                "endpoint": _endpoint_label(s.endpoint),
                "user_agent": s.user_agent,
                "created_at": iso(s.created_at),
                "updated_at": iso(s.updated_at),
                "failed_at": iso(s.failed_at),
                "last_error": s.last_error,
            }
            for s in subs
        ],
    }


def _send_payload_to_user(db, user_id: int, payload: dict) -> dict:
    from data.webpush_subscriptions import WebPushSubscription

    subs = (db.query(WebPushSubscription)
            .filter(WebPushSubscription.user_id == user_id,
                    WebPushSubscription.enabled.is_(True))
            .all())
    sent = 0
    failed = 0
    disabled = 0
    changed = False
    now = datetime.datetime.now()
    success_statuses = []
    for sub in subs:
        try:
            data = json.dumps(
                _wire_payload(payload, origin=getattr(sub, "origin", None)),
                ensure_ascii=False)
            response = _send_subscription_payload(sub, data)
            sent += 1
            status = (getattr(response, "status_code", None)
                      or getattr(response, "status", None))
            if status is not None:
                success_statuses.append(status)
            if sub.last_error or sub.failed_at:
                sub.last_error = None
                sub.failed_at = None
                sub.updated_at = now
                changed = True
        except Exception as exc:  # noqa: BLE001
            failed += 1
            code = getattr(exc, "status_code", None)
            sub.last_error = str(exc)[:500]
            sub.failed_at = now
            sub.updated_at = now
            changed = True
            if code in (404, 410):
                sub.enabled = False
                disabled += 1
    if changed:
        db.commit()
    return {
        "sent": sent,
        "failed": failed,
        "disabled": disabled,
        "active": len(subs),
        "success_statuses": success_statuses[:5],
    }


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

    db = db_sessions.create_session()
    try:
        msg = db.query(Messages).filter(Messages.id == message_id).first()
        payload = _message_payload(db, msg)
        if payload is None:
            return {"sent": 0, "failed": 0, "disabled": 0, "active": 0}
        return _send_payload_to_user(db, msg.user_id, payload)
    finally:
        db.close()


def notify_test(db, user_id: int) -> dict:
    """Отправить тестовый push текущему пользователю."""
    payload = {
        "title": "Проверка Synapse",
        "body": "Если это уведомление видно, фоновые push работают.",
        "tag": "synapse-webpush-test",
        "url": "/contacts",
        "contact_id": None,
        "message_id": None,
    }
    return _send_payload_to_user(db, user_id, payload)


def notify_direct_message(db, direct_msg, recipient_id: int,
                          sender_name: str | None = None) -> dict:
    """Push для внутреннего Synapse до ленивого зеркалирования в Messages."""
    from data.contacts import Contact, MessengerHandle

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
    return _send_payload_to_user(db, recipient_id, payload)
