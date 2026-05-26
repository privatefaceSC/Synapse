"""История редактирований сообщений.

Telegram (и Edit-кнопка в нашем UI) показывают только финальную версию
сообщения с пометкой «ред.». Мы храним ПОЛНУЮ историю: каждая прошлая
версия — отдельная строка `MessageEdit`. Финальный (текущий) текст
остаётся в `Messages.text` и в этой таблице НЕ дублируется.

То есть для сообщения с тремя версиями таблица содержит две записи
(v1 и v2 — старые), а актуальная v3 живёт в `Messages.text`. Если
редактирований не было, в `message_edits` пусто.

Текст шифруется тем же `EncryptedText`, что и `Messages.text` — у нас
все пользовательские строки лежат в БД зашифрованными.
"""
import datetime

import sqlalchemy

from .crypto import EncryptedText
from .db_sessions import SqlAlchemyBase


class MessageEdit(SqlAlchemyBase):
    __tablename__ = "message_edits"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                           autoincrement=True)
    message_id = sqlalchemy.Column(sqlalchemy.Integer,
                                   sqlalchemy.ForeignKey("messages.id"),
                                   nullable=False, index=True)
    # Текст, который БЫЛ до этого редактирования (т.е. предыдущая версия).
    text = sqlalchemy.Column(EncryptedText(), nullable=True)
    # Момент, когда мы зафиксировали эту версию как устаревшую (т.е. когда
    # пришло следующее редактирование). Для самой первой записи это момент
    # первого edit'а — оригинальный момент отправки сидит в `Messages.created_at`.
    edited_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                  default=datetime.datetime.now,
                                  nullable=False)


def push_old_version(db, message_id: int, old_text, when=None):
    """Сохранить старую версию текста как новую запись `MessageEdit`.

    Вызывать ПЕРЕД тем как обновить `Messages.text` новым значением.
    Если `old_text` пустой/None, всё равно пишем — нулевая версия имеет
    смысл, чтобы видеть «сначала было пусто, потом X»."""
    db.add(MessageEdit(
        message_id=message_id,
        text=old_text or "",
        edited_at=when or datetime.datetime.now(),
    ))
    db.flush()
