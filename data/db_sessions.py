import os
import threading
from contextlib import contextmanager

import sqlalchemy as sa
import sqlalchemy.orm as orm
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

SqlAlchemyBase = orm.declarative_base()

__factory = None
__engine = None
__schema_thread_lock = threading.Lock()
# Увеличивать при каждом изменении wanted/indexes ниже. Первый WSGI worker
# применяет миграции, остальные после общего file-lock читают только PRAGMA.
_SCHEMA_VERSION = 2


@contextmanager
def _schema_lock(db_file):
    """Сериализовать create_all/миграции между WSGI-процессами.

    AlwaysData может одновременно поднять несколько uWSGI workers.
    Без file-lock они все пытаются получить SQLite schema write-lock.
    """
    with __schema_thread_lock:
        if db_file == ":memory:" or os.name == "nt":
            yield
            return
        lock_path = os.path.abspath(db_file) + ".migrate.lock"
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        import fcntl
        lock_file = open(lock_path, "a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


def global_init(db_file):
    global __factory, __engine

    if __factory:
        return

    if not db_file or not db_file.strip():
        raise Exception("Необходимо указать файл базы данных.")

    db_file = db_file.strip()
    db_dir = os.path.dirname(db_file)
    if db_dir and db_file != ":memory:":
        os.makedirs(db_dir, exist_ok=True)

    conn_str = f'sqlite:///{db_file}'
    print(f"Подключение к базе данных по адресу {conn_str}")

    # WAL на NFS не включаем: shared-memory locking для него не
    # безопасен. Обычный SQLAlchemy QueuePool важен на сетевом
    # home-диске: новое SQLite-соединение на каждый polling
    # оказалось на AlwaysData намного дороже повторного использования.
    engine_kwargs = {
        "echo": False,
        "connect_args": {"check_same_thread": False, "timeout": 5},
    }
    if db_file == ":memory:":
        engine_kwargs["poolclass"] = StaticPool
    engine = sa.create_engine(conn_str, **engine_kwargs)

    @sa.event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    @sa.event.listens_for(engine, "checkout")
    def _reset_sqlite_busy_timeout(dbapi_connection, _record, _proxy):
        # Best-effort presence/heartbeat временно уменьшают timeout до
        # 100 мс. При возврате соединения из pool обычные операции снова
        # получают штатные пять секунд ожидания.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    __engine = engine
    # Сессии здесь короткоживущие (один HTTP-запрос/событие).
    # Не просрачиваем все ORM-объекты после commit: иначе рендер
    # тут же повторно читает те же строки с медленного NFS.
    __factory = orm.sessionmaker(bind=engine, expire_on_commit=False)

    from . import __all_models

    with _schema_lock(db_file):
        with engine.connect() as conn:
            schema_version = int(
                conn.exec_driver_sql("PRAGMA user_version").scalar() or 0)
        if schema_version < _SCHEMA_VERSION:
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
                     ("tg_grouped_id", "BIGINT"),
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
                  ("last_seen_at", "DATETIME"),
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
        if conn.exec_driver_sql(
                "SELECT 1 FROM users WHERE username IS NULL OR username = '' "
                "LIMIT 1").first():
            conn.exec_driver_sql(
                "UPDATE users SET username = 'user' || id "
                "WHERE username IS NULL OR username = ''")
        if conn.exec_driver_sql(
                "SELECT 1 FROM users WHERE created_at IS NULL LIMIT 1").first():
            conn.exec_driver_sql(
                "UPDATE users SET created_at = modified_date "
                "WHERE created_at IS NULL")
        # Горячие запросы ленты и Telegram bridge. ForeignKey в SQLite
        # сам по себе индекс не создаёт; без этих индексов каждый
        # polling открытого чата сканировал всю таблицу messages.
        indexes = (
            "CREATE INDEX IF NOT EXISTS ix_messages_handle_created_id "
            "ON messages(handle_id, created_at DESC, id DESC)",
            "CREATE INDEX IF NOT EXISTS ix_messages_user_tg_handle "
            "ON messages(user_id, tg_message_id, handle_id)",
            "CREATE INDEX IF NOT EXISTS ix_messages_notification_dedup "
            "ON messages(user_id, notification_dedup_key)",
            "CREATE INDEX IF NOT EXISTS ix_messages_delivery_recovery "
            "ON messages(delivery_status, tg_message_id, delivery_started_at)",
            "CREATE INDEX IF NOT EXISTS ix_attachments_message "
            "ON attachments(message_id)",
            "CREATE INDEX IF NOT EXISTS ix_attachments_stored_path "
            "ON attachments(stored_path)",
            "CREATE INDEX IF NOT EXISTS ix_contacts_user "
            "ON contacts(user_id)",
            "CREATE INDEX IF NOT EXISTS ix_handles_contact "
            "ON messenger_handles(contact_id)",
            "CREATE INDEX IF NOT EXISTS ix_handles_telegram_lookup "
            "ON messenger_handles(user_id, messenger_name, tg_chat_id)",
            "CREATE INDEX IF NOT EXISTS ix_direct_messages_sender_created "
            "ON direct_messages(sender_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_direct_messages_recipient_created "
            "ON direct_messages(recipient_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_direct_attachments_message "
            "ON direct_attachments(message_id)",
            "CREATE INDEX IF NOT EXISTS ix_pending_replies_device_queue "
            "ON pending_replies(user_id, status, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_chat_folders_user_messenger "
            "ON chat_folders(user_id, messenger_name, position)",
            "CREATE INDEX IF NOT EXISTS ix_chat_folder_members_folder "
            "ON chat_folder_members(folder_id, position)",
            "CREATE INDEX IF NOT EXISTS ix_chat_folder_members_handle "
            "ON chat_folder_members(handle_id)",
        )
        existing_indexes = {
            row[0] for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }
        all_indexes = (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username "
            "ON users(username)",
        ) + indexes
        for statement in all_indexes:
            # Не выполняем даже CREATE INDEX IF NOT EXISTS повторно:
            # на NFS и эта проверка берёт schema write-lock.
            match = statement.split(" INDEX IF NOT EXISTS ", 1)
            if len(match) != 2:
                continue
            index_name = match[1].split(None, 1)[0]
            if index_name in existing_indexes:
                continue
            conn.exec_driver_sql(statement)
            existing_indexes.add(index_name)
        conn.exec_driver_sql(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def create_session() -> Session:
    global __factory
    return __factory()


def _reset_for_tests():
    global __factory, __engine
    if __engine is not None:
        __engine.dispose()
    __engine = None
    __factory = None
