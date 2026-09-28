"""Persistent chat folders shared by the contacts UI and messenger bridges.

Folder membership is deliberately tied to ``MessengerHandle`` rather than
``Contact``.  A contact may combine Telegram, MAX and Synapse identities, but
folders belong to one messenger and must not leak to the other identities.
"""

import datetime

import sqlalchemy
from sqlalchemy import orm

from .db_sessions import SqlAlchemyBase


class ChatFolder(SqlAlchemyBase):
    __tablename__ = "chat_folders"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                           autoincrement=True)
    user_id = sqlalchemy.Column(
        sqlalchemy.Integer, sqlalchemy.ForeignKey("users.id"), nullable=False)
    messenger_name = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    # ``telegram`` folders mirror Telegram dialog filters.  ``local`` folders
    # are authoritative in Synapse (MAX, Synapse, VK, WhatsApp, ...).
    source = sqlalchemy.Column(sqlalchemy.String, nullable=False,
                               default="local")
    remote_id = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)
    remote_kind = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    title = sqlalchemy.Column(sqlalchemy.String, nullable=False)
    icon = sqlalchemy.Column(sqlalchemy.String, nullable=True)
    color = sqlalchemy.Column(sqlalchemy.Integer, nullable=True)
    position = sqlalchemy.Column(sqlalchemy.Integer, nullable=False,
                                 default=0)
    read_only = sqlalchemy.Column(sqlalchemy.Boolean, nullable=False,
                                  default=False)
    # A normalized copy of Telegram's DialogFilter.  Remote mutations still
    # fetch the fresh filter before writing; this JSON is for diagnostics and
    # deterministic materialization, never the source of truth for writes.
    definition_json = sqlalchemy.Column(sqlalchemy.Text, nullable=True)
    created_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now,
                                   nullable=False)
    updated_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                   default=datetime.datetime.now,
                                   onupdate=datetime.datetime.now,
                                   nullable=False)
    synced_at = sqlalchemy.Column(sqlalchemy.DateTime, nullable=True)

    members = orm.relationship(
        "ChatFolderMember", back_populates="folder",
        cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (
        sqlalchemy.UniqueConstraint(
            "user_id", "messenger_name", "source", "remote_id",
            name="uq_chat_folder_remote"),
    )


class ChatFolderMember(SqlAlchemyBase):
    __tablename__ = "chat_folder_members"

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                           autoincrement=True)
    folder_id = sqlalchemy.Column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey("chat_folders.id", ondelete="CASCADE"),
        nullable=False)
    handle_id = sqlalchemy.Column(
        sqlalchemy.Integer,
        sqlalchemy.ForeignKey("messenger_handles.id", ondelete="CASCADE"),
        nullable=False)
    position = sqlalchemy.Column(sqlalchemy.Integer, nullable=False,
                                 default=0)
    pinned = sqlalchemy.Column(sqlalchemy.Boolean, nullable=False,
                               default=False)
    membership_source = sqlalchemy.Column(sqlalchemy.String, nullable=False,
                                          default="explicit")

    folder = orm.relationship("ChatFolder", back_populates="members")

    __table_args__ = (
        sqlalchemy.UniqueConstraint(
            "folder_id", "handle_id", name="uq_chat_folder_member"),
    )
