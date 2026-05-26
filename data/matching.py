import unicodedata


def normalize(s: str) -> str:
    s = s.lower().strip()
    return ''.join(
        ch for ch in s
        if unicodedata.category(ch).startswith(('L', 'N'))
    )


def split_group_sender(sender_raw: str):
    if not sender_raw or ":" not in sender_raw:
        return None
    prefix, _, member = sender_raw.partition(":")
    prefix, member = prefix.strip(), member.strip()
    if not prefix or not member:
        return None
    return prefix, member


# Тексты-заглушки, которые мост подставляет когда у медиа нет подписи —
# чтобы в БД у сообщения был хоть какой-то превью-текст (для списка
# контактов слева, для пушей, для outline). В bubble чата эти заглушки
# не нужны: само вложение и так показывается. Используется `is_media_placeholder`,
# чтобы скрыть строку текста в UI, не теряя её в превью.
_MEDIA_PLACEHOLDER_PREFIXES = (
    "📷 ", "🎬 ", "🎤 ", "🎵 ", "🩷 ", "📎 ",
)
_MEDIA_PLACEHOLDER_EXACT = {
    "📷 Фото", "🎬 Видео", "🎤 Голосовое сообщение",
    "🎵 Аудио", "🩷 Стикер", "📎 Файл", "📎 Вложение",
}


def is_media_placeholder(text) -> bool:
    """True, если text — это технический плейсхолдер вида «📷 Фото» /
    «📎 имя_файла», и его надо прятать в bubble когда есть вложение."""
    if not text:
        return False
    t = text.strip()
    if t in _MEDIA_PLACEHOLDER_EXACT:
        return True
    return any(t.startswith(p) for p in _MEDIA_PLACEHOLDER_PREFIXES)


def display_author(sender_raw: str, contact_display_name: str) -> str:
    if not sender_raw or not contact_display_name:
        return sender_raw
    parsed = split_group_sender(sender_raw)
    if parsed is None:
        return sender_raw
    prefix, member = parsed
    if prefix == contact_display_name.strip():
        return member
    return sender_raw
