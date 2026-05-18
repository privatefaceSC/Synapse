import datetime

import sqlalchemy
from sqlalchemy import orm

from .db_sessions import SqlAlchemyBase


class Attachment(SqlAlchemyBase):
    __tablename__ = 'attachments'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    user_id = sqlalchemy.Column(sqlalchemy.Integer,
                                sqlalchemy.ForeignKey("users.id"), nullable=False)
    message_id = sqlalchemy.Column(sqlalchemy.Integer,
                                   sqlalchemy.ForeignKey("messages.id"), nullable=False)
    kind = sqlalchemy.Column(sqlalchemy.String, nullable=False)  # пока только 'image'
    mime = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    original_name = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    stored_path = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    size = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)
    dedup_key = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now, nullable=False)

    __table_args__ = (
        sqlalchemy.UniqueConstraint('user_id', 'dedup_key',
                                    name='uq_attachment_user_dedup'),
    )

    message = orm.relationship("Messages", foreign_keys=[message_id])
