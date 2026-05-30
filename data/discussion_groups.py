"""Реестр linked discussion-групп Telegram-каналов.

Когда пользователь открывает «комментарии» под постом канала, Telethon
через GetDiscussionMessageRequest сообщает нам id связанной группы
обсуждений. Мы запоминаем его в этой таблице, чтобы события из этой
группы НЕ порождали отдельный Contact с именем «Комментарии» в общем
списке слева — комментарии живут только в полноэкранной шторке поверх
канала.

В отличие от in-memory set'а, эта таблица переживает рестарт Flask,
так что событие про новый комментарий, пришедшее после перезапуска,
тоже корректно игнорируется."""

import datetime

import sqlalchemy

from .db_sessions import SqlAlchemyBase


class DiscussionGroup(SqlAlchemyBase):
    __tablename__ = 'discussion_groups'

    id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True,
                            autoincrement=True)
    tg_chat_id = sqlalchemy.Column(sqlalchemy.BigInteger, unique=True,
                                    nullable=False)
    discovered_at = sqlalchemy.Column(sqlalchemy.DateTime,
                                       default=datetime.datetime.now,
                                       nullable=False)
