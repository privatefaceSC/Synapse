import datetime

import sqlalchemy

from .db_sessions import SqlAlchemyBase


class SavedSticker(SqlAlchemyBase):
    __tablename__ = 'saved_stickers'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                           autoincrement=True)
    user_id = sqlalchemy.Column(sqlalchemy.Integer,
                                sqlalchemy.ForeignKey("users.id"),
                                nullable=False)
    source_attachment_id = sqlalchemy.Column(
        sqlalchemy.Integer, sqlalchemy.ForeignKey("attachments.id"),
        nullable=True)
    kind = sqlalchemy.Column(sqlalchemy.String, nullable=False,
                             default='sticker')
    mime = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    original_name = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    stored_path = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    size = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)
    # Набор, из которого пришёл стикер. Для Telegram это telegram:<set id>
    # или telegram:<short name>; для локальных/импортированных наборов -
    # стабильный локальный ключ.
    pack_key = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    pack_title = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    item_key = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now,
                                   nullable=False)
    last_used_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)

    __table_args__ = (
        sqlalchemy.UniqueConstraint('user_id', 'stored_path',
                                    name='uq_saved_sticker_user_path'),
    )
