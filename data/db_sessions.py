import os

import sqlalchemy as sa
import sqlalchemy.orm as orm
from sqlalchemy.orm import Session

SqlAlchemyBase = orm.declarative_base()

__factory = None

def global_init(db_file):
    global __factory

    if __factory:
        return

    if not db_file or not db_file.strip():
        raise Exception("Необходимо указать файл базы данных.")

    db_file = db_file.strip()
    db_dir = os.path.dirname(db_file)
    if db_dir and db_file != ":memory:":
        os.makedirs(db_dir, exist_ok=True)

    conn_str = f'sqlite:///{db_file}?check_same_thread=False'
    print(f"Подключение к базе данных по адресу {conn_str}")

    engine = sa.create_engine(conn_str, echo=False)
    __factory = orm.sessionmaker(bind=engine)

    from . import __all_models

    SqlAlchemyBase.metadata.create_all(engine)
    _apply_light_migrations(engine)


def _apply_light_migrations(engine):
    """Идемпотентно добавляет недостающие nullable-колонки в уже
    существующую БД. `create_all` создаёт только отсутствующие таблицы,
    но не дописывает новые колонки в старые — поэтому ALTER вручную.
    Только аддитивные изменения, данные не трогаются."""
    wanted = {
        "messenger_handles": [("tg_chat_id", "BIGINT"),
                              ("tg_chat_type", "VARCHAR"),
                              ("package_name", "VARCHAR"),
                              ("tg_is_forum", "BOOLEAN"),
                              ("tg_forum_checked_at", "DATETIME"),
                              ("is_group", "BOOLEAN")],
        "messages": [("outgoing", "BOOLEAN"), ("tg_message_id", "BIGINT"),
                     ("reply_to_message_id", "INTEGER"),
                     ("tg_read_at", "DATETIME"),
                     ("deleted_at", "DATETIME"),
                     ("tg_ttl_seconds", "INTEGER"),
                     ("fwd_from_name", "VARCHAR"),
                     ("fwd_from_tg_chat_id", "BIGINT"),
                     ("fwd_from_messenger", "VARCHAR"),
                     ("fwd_from_synapse_user_id", "INTEGER"),
                     ("author_tg_chat_id", "BIGINT"),
                     ("tg_topic_id", "BIGINT"),
                     ("tg_topic_title", "VARCHAR"),
                     ("text_html", "TEXT"),
                     ("pinned_at", "DATETIME"),
                     ("notification_dedup_key", "VARCHAR"),
                     ("delivery_status", "VARCHAR"),
                     ("delivery_error", "TEXT"),
                     ("delivery_caption", "TEXT"),
                     ("delivery_silent", "BOOLEAN"),
                     ("delivery_reply_to_tg_id", "BIGINT"),
                     ("delivery_schedule_at", "DATETIME"),
                     ("delivery_started_at", "DATETIME"),
                     ("author_avatar_path", "VARCHAR")],
        "contacts": [("avatar_path", "VARCHAR"),
                     ("pinned_at", "DATETIME"),
                     ("muted", "BOOLEAN"),
                     ("archived", "BOOLEAN"),
                     ("blocked_at", "DATETIME")],
        "users": [("username", "VARCHAR"),
                  ("created_at", "DATETIME"),
                  ("preferred_lang", "VARCHAR"),
                  ("about_seen_at", "DATETIME"),
                  ("is_creator", "BOOLEAN")],
        "chat_topics": [("topic_id", "BIGINT")],
        "web_push_subscriptions": [("origin", "VARCHAR")],
        "attachments": [("sticker_pack_key", "VARCHAR"),
                        ("sticker_pack_title", "VARCHAR"),
                        ("sticker_item_key", "VARCHAR")],
        "direct_attachments": [("sticker_pack_key", "VARCHAR"),
                               ("sticker_pack_title", "VARCHAR"),
                               ("sticker_item_key", "VARCHAR")],
        "saved_stickers": [("pack_key", "VARCHAR"),
                           ("pack_title", "VARCHAR"),
                           ("item_key", "VARCHAR")],
        "pending_replies": [("client_send_key", "VARCHAR")],
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            info = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in info}
            for name, sqltype in cols:
                if name not in existing:
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {sqltype}")
        # User ID нельзя добавить как UNIQUE-колонку через ALTER в SQLite,
        # поэтому проставляем существующим пользователям значение по умолчанию
        # (user<id>) и навешиваем уникальный индекс отдельно.
        conn.exec_driver_sql(
            "UPDATE users SET username = 'user' || id "
            "WHERE username IS NULL OR username = ''")
        conn.exec_driver_sql(
            "UPDATE users SET created_at = modified_date "
            "WHERE created_at IS NULL")
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username "
            "ON users(username)")


def create_session() -> Session:
    global __factory
    return __factory()


def _reset_for_tests():
    global __factory
    __factory = None
