"""Короткоживущие действия пользователей внутреннего Synapse.

Состояния вроде «печатает» и «записывает голосовое» слишком частые и
мимолётные, чтобы писать их в основную SQLite-базу. На AlwaysData это снова
создало бы конкуренцию за write-lock. Небольшие атомарно заменяемые файлы в
локальном временном каталоге видны всем uWSGI worker-ам и сами протухают.
"""

import os
import tempfile
import time
import uuid


ALLOWED_KINDS = frozenset({
    "typing",
    "recording_voice",
    "recording_video",
    "recording_video_note",
    "uploading_photo",
    "uploading_video",
    "uploading_video_note",
    "uploading_voice",
    "uploading_file",
    "choosing_sticker",
})

_DEFAULT_TTL_SECONDS = 8.0


def _root() -> str:
    configured = (os.environ.get("SYNAPSE_RUNTIME_DIR") or "").strip()
    base = configured or tempfile.gettempdir()
    return os.path.join(base, "skillwood-live-activity")


def _path(recipient_id: int, sender_id: int) -> str:
    return os.path.join(
        _root(), f"activity-{int(recipient_id)}-{int(sender_id)}.txt")


def clear_activity(recipient_id: int, sender_id: int) -> None:
    try:
        os.remove(_path(recipient_id, sender_id))
    except FileNotFoundError:
        pass
    except OSError:
        # Это необязательный UI-сигнал: ошибка временного каталога не должна
        # мешать отправке настоящего сообщения.
        pass


def set_activity(recipient_id: int, sender_id: int, kind: str,
                 ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> bool:
    kind = (kind or "").strip().lower()
    if kind not in ALLOWED_KINDS:
        return False
    root = _root()
    target = _path(recipient_id, sender_id)
    temporary = os.path.join(root, "." + uuid.uuid4().hex + ".tmp")
    try:
        os.makedirs(root, exist_ok=True)
        expires_at = time.time() + max(2.0, float(ttl_seconds))
        with open(temporary, "w", encoding="ascii") as stream:
            stream.write(f"{expires_at:.6f}\n{kind}\n")
        os.replace(temporary, target)
        return True
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False


def activity_status(recipient_id: int, sender_id: int) -> dict:
    target = _path(recipient_id, sender_id)
    try:
        with open(target, encoding="ascii") as stream:
            lines = stream.read(160).splitlines()
        expires_at = float(lines[0])
        kind = lines[1].strip().lower()
    except (OSError, ValueError, IndexError):
        return {"active": False, "kind": None, "authors": []}
    if kind not in ALLOWED_KINDS or expires_at <= time.time():
        clear_activity(recipient_id, sender_id)
        return {"active": False, "kind": None, "authors": []}
    return {
        "active": True,
        "typing": True,
        "kind": kind,
        "authors": [],
        "expires_at": expires_at,
    }
