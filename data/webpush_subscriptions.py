import datetime

import sqlalchemy

from .db_sessions import SqlAlchemyBase


class WebPushSubscription(SqlAlchemyBase):
    """Push-подписка конкретного браузера/устройства пользователя."""

    __tablename__ = 'web_push_subscriptions'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                           autoincrement=True)
    user_id = sqlalchemy.Column(sqlalchemy.Integer,
                                sqlalchemy.ForeignKey("users.id"),
                                nullable=False)
    endpoint = sqlalchemy.Column(sqlalchemy.String, nullable=False,
                                 unique=True)
    p256dh = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    auth = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    user_agent = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    enabled = sqlalchemy.Column(sqlalchemy.Boolean, default=True,
                                nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now)
    updated_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now)
    failed_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    last_error = sqlalchemy.Column(sqlalchemy.String, nullable=True)
