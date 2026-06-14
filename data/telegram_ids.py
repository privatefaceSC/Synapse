"""Утилиты нормализации Telegram chat_id.

Telethon в разных местах отдаёт один и тот же channel/megagroup двумя
формами: короткий channel_id (например, 1457724382) и chat_id события
с префиксом -100 (например, -1001457724382). Для сравнения связанных
discussion-групп нужно учитывать обе формы.
"""


_CHANNEL_PREFIX = 1_000_000_000_000


def chat_id_variants(chat_id):
    """Все известные числовые формы одного Telegram channel/megagroup id."""
    if chat_id is None:
        return set()
    try:
        value = int(chat_id)
    except (TypeError, ValueError):
        return set()
    variants = {value}
    abs_value = abs(value)
    variants.add(abs_value)
    if value > 0:
        variants.add(-(_CHANNEL_PREFIX + value))
    elif abs_value > _CHANNEL_PREFIX:
        variants.add(abs_value - _CHANNEL_PREFIX)
    return variants
