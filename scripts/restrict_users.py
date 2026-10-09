"""Отключить фоновые каналы и удалить media неактивных пользователей.

Сообщения, контакты, аккаунты, устройства и Telegram-сессии не удаляются.
Без ``--apply`` скрипт только показывает план.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data import db_sessions  # noqa: E402
from data.devices import Device  # noqa: E402
from data.pending_replies import (PendingReply, STATUS_EXPIRED,
                                  STATUS_PENDING, STATUS_PICKED)  # noqa: E402
from data.users import Messages, User  # noqa: E402
from data.webpush_subscriptions import WebPushSubscription  # noqa: E402


def _directory_size(path: Path) -> int:
    total = 0
    try:
        entries = list(path.rglob('*'))
    except OSError:
        return 0
    for entry in entries:
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _safe_user_media_dir(media_root: Path, user_id: int) -> Path:
    root = media_root.resolve()
    target = (root / str(int(user_id))).resolve()
    if target.parent != root or target.name != str(int(user_id)):
        raise RuntimeError(f'unsafe media path: {target}')
    return target


def restrict_users(db_path: str, media_root: str, keep_user_ids: set[int],
                   apply: bool = False) -> dict:
    if not keep_user_ids:
        raise ValueError('at least one --keep-user is required')

    db_sessions.global_init(db_path)
    db = db_sessions.create_session()
    db_updates_applied = False
    db_update_error = None
    try:
        all_user_ids = {int(value) for (value,) in db.query(User.id).all()}
        restricted_ids = sorted(all_user_ids - set(keep_user_ids))
        message_count = (db.query(Messages.id)
                         .filter(Messages.user_id.in_(restricted_ids)).count()
                         if restricted_ids else 0)
        push_count = (db.query(WebPushSubscription.id)
                      .filter(WebPushSubscription.user_id.in_(restricted_ids),
                              WebPushSubscription.enabled.is_(True)).count()
                      if restricted_ids else 0)
        pending_count = (db.query(PendingReply.id)
                         .filter(PendingReply.user_id.in_(restricted_ids),
                                 PendingReply.status.in_(
                                     (STATUS_PENDING, STATUS_PICKED))).count()
                         if restricted_ids else 0)
        device_count = (db.query(Device.id)
                        .filter(Device.user_id.in_(restricted_ids)).count()
                        if restricted_ids else 0)

        needs_db_update = bool(push_count or pending_count)
        if apply and restricted_ids and needs_db_update:
            now = datetime.now()
            (db.query(WebPushSubscription)
             .filter(WebPushSubscription.user_id.in_(restricted_ids),
                     WebPushSubscription.enabled.is_(True))
             .update({WebPushSubscription.enabled: False,
                      WebPushSubscription.updated_at: now,
                      WebPushSubscription.last_error: 'service_restricted'},
                     synchronize_session=False))
            (db.query(PendingReply)
             .filter(PendingReply.user_id.in_(restricted_ids),
                     PendingReply.status.in_((STATUS_PENDING, STATUS_PICKED)))
             .update({PendingReply.status: STATUS_EXPIRED,
                      PendingReply.error: 'service_restricted',
                      PendingReply.device_id: None},
                     synchronize_session=False))
            try:
                db.commit()
                db_updates_applied = True
            except SQLAlchemyError as exc:
                # Ограничение доступа уже останавливает доставку. Очистка
                # файлов не должна срываться из-за занятого сетевого SQLite.
                db.rollback()
                db_update_error = str(exc)
        else:
            # Не делаем пустой commit: на сетевом SQLite он всё равно
            # запрашивает write-lock и может мешать работающему сайту.
            db.rollback()
    finally:
        db.close()

    root = Path(media_root)
    media_bytes = 0
    removed_dirs = []
    for user_id in restricted_ids:
        target = _safe_user_media_dir(root, user_id)
        if not target.exists():
            continue
        media_bytes += _directory_size(target)
        if apply:
            shutil.rmtree(target)
            removed_dirs.append(str(target))

    return {
        'applied': bool(apply),
        'kept_user_ids': sorted(keep_user_ids),
        'restricted_user_ids': restricted_ids,
        'messages_preserved': int(message_count),
        'database_updates_needed': bool(needs_db_update),
        'database_updates_applied': bool(db_updates_applied),
        'database_update_error': db_update_error,
        'webpush_disabled': int(
            push_count if apply and db_updates_applied else 0),
        'pending_replies_expired': int(
            pending_count if apply and db_updates_applied else 0),
        'registered_devices_kept_dormant': int(device_count),
        'media_bytes_found': int(media_bytes),
        'media_directories_removed': removed_dirs,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=os.environ.get(
        'SKILLWOOD_DB_PATH', str(PROJECT_ROOT / 'db' / 'blogs.db')))
    parser.add_argument('--media-root', default=os.environ.get(
        'SKILLWOOD_MEDIA_ROOT', str(PROJECT_ROOT / 'media')))
    parser.add_argument('--keep-user', type=int, action='append', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = restrict_users(
        args.db, args.media_root, set(args.keep_user), apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
