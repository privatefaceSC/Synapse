"""Одноразовый перенос Synapse при остановленных писателях.

Пример: SKILLWOOD_DATABASE_URL=postgresql+psycopg://... \
    python -m scripts.migrate_sqlite_to_postgres --sqlite db/blogs.db

Скрипт отказывается писать в непустую PostgreSQL-базу. Исходный SQLite
открывается только для чтения; содержимое сообщений не выводится в лог.
"""

import argparse
import datetime
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import quote

import sqlalchemy as sa

from data import __all_models  # noqa: F401 — регистрация всех таблиц
from data.db_sessions import SqlAlchemyBase, _hot_index_statements


def _source_connection(path):
    absolute = Path(path).resolve(strict=True)
    if not absolute.is_file():
        raise ValueError("Источник должен быть файлом SQLite")
    uri = "file:" + quote(str(absolute).replace("\\", "/"), safe="/:") + "?mode=ro"
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _quoted(name):
    return '"' + name.replace('"', '""') + '"'


def _normalized(value, column):
    if value is None:
        return None
    if isinstance(column.type, sa.Boolean):
        return bool(value)
    if isinstance(column.type, sa.DateTime):
        if isinstance(value, str):
            return datetime.datetime.fromisoformat(value)
        return value
    if isinstance(value, memoryview):
        return value.tobytes()
    return value


def _check_source(db, tables):
    if db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        raise RuntimeError("SQLite не прошла проверку целостности")
    orphans = {"attachments": set(), "message_edits": set()}
    for table_name, rowid, parent, _ in db.execute("PRAGMA foreign_key_check"):
        if (table_name, parent) not in (
                ("attachments", "messages"),
                ("message_edits", "messages")):
            raise RuntimeError("В SQLite есть нарушенный внешний ключ: "
                               f"таблица {table_name}, строка {rowid}")
        record = db.execute(
            f"SELECT id FROM {_quoted(table_name)} WHERE rowid = ?",
            (rowid,)).fetchone()
        if record is None:
            raise RuntimeError(f"Не найдена строка {table_name}, rowid={rowid}")
        orphans[table_name].add(record[0])
    present = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    missing = [table.name for table in tables if table.name not in present]
    if missing:
        raise RuntimeError("В SQLite отсутствуют таблицы: " + ", ".join(missing))
    counts = {
        table.name: db.execute(
            f"SELECT COUNT(*) FROM {_quoted(table.name)}").fetchone()[0]
        for table in tables
    }
    if orphans["attachments"]:
        linked = db.execute(
            "SELECT COUNT(*) FROM saved_stickers s "
            "JOIN attachments a ON a.id = s.source_attachment_id "
            "LEFT JOIN messages m ON m.id = a.message_id "
            "WHERE m.id IS NULL").fetchone()[0]
        if linked:
            raise RuntimeError("Сохранённые стикеры ссылаются на "
                               "осиротевшие вложения")
    return counts, orphans


def _source_rows(db, table, batch_size):
    cursor = db.execute(f"SELECT * FROM {_quoted(table.name)} ORDER BY id")
    wanted = {column.name: column for column in table.columns}
    if {column_info[0] for column_info in cursor.description} != set(wanted):
        raise RuntimeError(f"Различаются колонки таблицы {table.name}")
    while rows := cursor.fetchmany(batch_size):
        yield [
            {name: _normalized(row[name], column)
             for name, column in wanted.items()}
            for row in rows
        ]


def _verify_table(db, conn, table, target_table, batch_size, skipped_ids):
    source = (row for batch in _source_rows(db, table, batch_size)
              for row in batch if row["id"] not in skipped_ids)
    columns = [column.name for column in table.columns]
    result = conn.execution_options(stream_results=True).execute(
        sa.select(*(target_table.c[name] for name in columns))
        .order_by(target_table.c.id))
    copied = 0
    for row in result:
        original = next(source, None)
        if original is None:
            raise RuntimeError(f"В PostgreSQL лишние строки: {table.name}")
        for name in columns:
            column = table.c[name]
            if original[name] != _normalized(row._mapping[name], column):
                raise RuntimeError(
                    f"Сверка не прошла: {table.name}, id={original['id']}, "
                    f"колонка {name}")
        copied += 1
    if next(source, None) is not None:
        raise RuntimeError(f"В PostgreSQL не хватает строк: {table.name}")
    return copied


def migrate(sqlite_path, postgres_url, batch_size=250):
    url = sa.engine.make_url(postgres_url)
    if url.drivername not in ("postgresql", "postgresql+psycopg"):
        raise ValueError("Нужен адрес PostgreSQL")
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    tables = SqlAlchemyBase.metadata.sorted_tables
    source = _source_connection(sqlite_path)
    engine = None
    try:
        source.execute("BEGIN")
        counts, orphans = _check_source(source, tables)
        print("SQLite проверена; строк:", sum(counts.values()))
        if any(orphans.values()):
            print("Строки без исходного сообщения будут сохранены "
                  "в synapse_orphan_rows:",
                  sum(len(ids) for ids in orphans.values()))
        engine = sa.create_engine(url, connect_args={"connect_timeout": 5})
        with engine.begin() as conn:
            conn.exec_driver_sql("SELECT pg_advisory_xact_lock(732106441)")
            existing = sa.inspect(conn).get_table_names()
            if existing:
                raise RuntimeError("Целевая PostgreSQL-база не пуста; "
                                   "перенос прерван без изменений")
            SqlAlchemyBase.metadata.create_all(conn)
            conn.exec_driver_sql(
                "CREATE TABLE synapse_orphan_rows ("
                "source_table VARCHAR(64) NOT NULL, "
                "source_id BIGINT NOT NULL, payload TEXT NOT NULL, "
                "PRIMARY KEY (source_table, source_id))")
            archive = sa.Table("synapse_orphan_rows", sa.MetaData(),
                               autoload_with=conn)
            # Reflection использует физические TEXT-колонки. ORM TypeDecorator
            # здесь повторно зашифровал бы уже зашифрованные сообщения.
            reflected = sa.MetaData()
            target_tables = {
                table.name: sa.Table(table.name, reflected, autoload_with=conn)
                for table in tables
            }
            for table in tables:
                target = target_tables[table.name]
                inserted = 0
                deferred_replies = []
                archived = []
                for batch in _source_rows(source, table, batch_size):
                    for row in batch:
                        for name, value in row.items():
                            if isinstance(value, str) and "\x00" in value:
                                raise RuntimeError(
                                    f"PostgreSQL не принимает NUL: "
                                    f"{table.name}, id={row['id']}, колонка {name}")
                    if orphans.get(table.name):
                        kept = []
                        for row in batch:
                            if row["id"] in orphans[table.name]:
                                archived.append({
                                    "source_table": table.name,
                                    "source_id": row["id"],
                                    "payload": json.dumps(
                                        row, ensure_ascii=False,
                                        default=str, sort_keys=True),
                                })
                            else:
                                kept.append(row)
                        batch = kept
                    if not batch:
                        continue
                    if table.name == "messages":
                        for row in batch:
                            if row["reply_to_message_id"] is not None:
                                deferred_replies.append(
                                    (row["id"], row["reply_to_message_id"]))
                                row["reply_to_message_id"] = None
                    conn.execute(target.insert(), batch)
                    inserted += len(batch)
                if inserted + len(archived) != counts[table.name]:
                    raise RuntimeError(f"Изменился источник: {table.name}")
                if archived:
                    conn.execute(archive.insert(), archived)
                    saved = [tuple(row) for row in conn.execute(
                        sa.select(archive.c.source_id, archive.c.payload)
                        .where(archive.c.source_table == table.name)
                        .order_by(archive.c.source_id)).all()]
                    expected = sorted(
                        ((row["source_id"], row["payload"])
                         for row in archived), key=lambda row: row[0])
                    if saved != expected:
                        raise RuntimeError(
                            f"Не удалось проверить архив {table.name}")
                if deferred_replies:
                    conn.execute(
                        target.update().where(target.c.id == sa.bindparam("row_id"))
                        .values(reply_to_message_id=sa.bindparam("reply_id")),
                        [{"row_id": row_id, "reply_id": reply_id}
                         for row_id, reply_id in deferred_replies])
                if inserted:
                    sequence = conn.execute(sa.text(
                        "SELECT pg_get_serial_sequence(:table_name, 'id')"),
                        {"table_name": table.name}).scalar()
                    if sequence:
                        highest_id = conn.execute(
                            sa.select(sa.func.max(target.c.id))).scalar()
                        conn.execute(sa.text(
                            "SELECT setval(CAST(:sequence AS regclass), "
                            ":highest_id, true)"),
                            {"sequence": sequence, "highest_id": highest_id})
                print(f"Перенесено {table.name}: {inserted}")
            for statement in _hot_index_statements()[1:]:
                conn.exec_driver_sql(statement)
            # Проверка идёт до COMMIT; любое расхождение откатывает перенос.
            for table in tables:
                checked = _verify_table(
                    source, conn, table, target_tables[table.name], batch_size,
                    orphans.get(table.name, set()))
                print(f"Проверено {table.name}: {checked}")
            # SQLAlchemy сохраняет stream_results в опциях Connection;
            # CREATE TABLE нельзя выполнять через серверный курсор.
            conn.execution_options(stream_results=False)
            conn.exec_driver_sql(
                "CREATE TABLE synapse_schema_version (version INTEGER NOT NULL)")
            conn.exec_driver_sql(
                "INSERT INTO synapse_schema_version (version) VALUES (1)")
        print("Перенос завершён и сохранён в PostgreSQL")
    finally:
        source.close()
        if engine is not None:
            engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="db/blogs.db")
    parser.add_argument("--batch-size", type=int, default=250)
    args = parser.parse_args()
    postgres_url = os.environ.get("SKILLWOOD_DATABASE_URL")
    if not postgres_url:
        parser.error("Задайте SKILLWOOD_DATABASE_URL")
    if not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size должен быть от 1 до 1000")
    migrate(args.sqlite, postgres_url, args.batch_size)


if __name__ == "__main__":
    main()
