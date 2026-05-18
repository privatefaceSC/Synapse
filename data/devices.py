import datetime
import hashlib
import secrets

import sqlalchemy

from .db_sessions import SqlAlchemyBase


class Device(SqlAlchemyBase):
    __tablename__ = 'devices'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True, autoincrement=True)
    user_id = sqlalchemy.Column(sqlalchemy.Integer,
                                sqlalchemy.ForeignKey("users.id"), nullable=False)
    name = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    token_hash = sqlalchemy.Column(sqlalchemy.String, nullable=False, unique=True, index=True)
    last_seen_ip = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    last_seen_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now, nullable=False)


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
