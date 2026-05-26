# CLAUDE.md

Этот файл содержит инструкции для Claude Code (claude.ai/code) при работе с этим репозиторием.

## Запуск приложения

```bash
# Активировать venv (Windows / bash)
source .venv/Scripts/activate

# Установка зависимостей
pip install -r requirements.txt

# Запустить Flask-сервер на 0.0.0.0:5000
python main.py
```

Зависимости зафиксированы в `requirements.txt` (Flask 3.1, Flask-Login, SQLAlchemy 2.0, sqlalchemy-serializer, Werkzeug, requests, pytest).

Тесты на `pytest` в [tests/](tests/). Запуск:

```bash
.venv/Scripts/pytest -v
```

Линтера и отдельного шага сборки в проекте нет.

## Архитектура

Это небольшое Flask-приложение, работающее как **мост между собственным Android-клиентом (Notification Listener) и веб-панелью** для просмотра уведомлений из мессенджеров. Интерфейс и тексты — на русском.

### Создание приложения

Точка входа — `create_app(db_path="db/blogs.db")` в [main.py](main.py). Внутри: `db_sessions.global_init(db_path)`, создание Flask-app, регистрация маршрутов через `register_routes(app)`, `teardown_appcontext` для закрытия per-request сессии. Сессия создаётся лениво в `get_db()` и хранится в `flask.g.db`.

### Привязка устройства (центральная нетривиальная идея)

1. Регистрация на `/register` → сервер генерирует 8-значный `connect_code`, сохраняет на `User`.
2. Код показывается на `/code` (auto-refresh 3 секунды); страница редиректит на `/home`, как только у пользователя появляется хотя бы один `Device`.
3. Android-клиент шлёт `POST /api/connect` с `{code, device_name}`. Сервер находит `User` по `connect_code`, создаёт `Device(user_id, name, token_hash)` и возвращает **сырой Bearer-токен** — больше нигде в открытом виде он не хранится (в БД лежит только `sha256(token)`).
4. Все последующие запросы Android-клиента (`/add`, `/api/me`) идут с заголовком `Authorization: Bearer <token>`. Сервер ищет `Device` по `hash_token(token)` и определяет владельца через `device.user_id`. См. `_device_from_bearer` в [main.py](main.py).
5. На каждый успешный `POST /add` обновляются `device.last_seen_ip` и `device.last_seen_at`.

### Контакт-центричная модель сообщений

Сообщения не валятся в одну ленту, а группируются по контактам:

```
User
 └─ 1:N → Contact(display_name)            ← реальный человек глазами владельца
            └─ 1:N → MessengerHandle       ← конкретная личность в конкретном мессенджере
                       └─ 1:N → Messages   ← сообщение, привязано к handle
```

`POST /add` ищет `MessengerHandle` по `(user_id, messenger_name, sender_raw)`. Не нашёл — создаёт новый `Contact` с `display_name = sender` и привязывает handle. Объединение и перенос контактов — только вручную (см. `/contacts/manage`); автоматических подсказок слияния в проекте нет.

UI:
- `/contacts` — двухпанельный лейаут (список / переписка).
- `/contacts/<id>` — переписка с контактом, агрегирует сообщения всех его handles.
- `/contacts/manage` — управление связями: переименование, перемещение handle между контактами, объединение контактов.

Старый `/messages` редиректит на `/contacts`.

### Слой данных

- **SQLite** в `db/blogs.db`. Путь по умолчанию в `create_app("db/blogs.db")`, в тестах — `:memory:`.
- Engine создаётся с `check_same_thread=False`. Сессия — **per-request** через `flask.g`: `get_db()` создаёт сессию при первом обращении в обработчике, `teardown_appcontext` закрывает.
- Модели:
  - [data/users.py](data/users.py) — `User`, `Messages`. У `Messages` поля `handle_id` (FK) и `created_at` добавлены под контакты; `sender`/`messenger_name` остаются как исторический снимок.
  - [data/contacts.py](data/contacts.py) — `Contact`, `MessengerHandle`, плюс сервисные функции `find_or_create_handle`, `record_message`, `merge_contacts`.
- [data/matching.py](data/matching.py) — `normalize`, `split_group_sender`, `display_author` (нормализация имён и разбор групповых отправителей вида «группа: имя»).
- Новые модели должны импортироваться в [data/__all_models.py](data/__all_models.py).
- `data/db_sessions.py` экспортирует `global_init`, `create_session`, `_reset_for_tests` (последняя — только для pytest-фикстур).

### Маршруты

Все живут в [main.py](main.py). Веб-авторизация — через `flask.session['user_id']`; декоратора `@login_required` нет, каждый защищённый маршрут проверяет `session.get('user_id')` вручную. Android-API авторизуется Bearer-токеном через `_device_from_bearer`.

- `/`, `/home`, `/login`, `/register`, `/logout`, `/code` — страницы аутентификации/профиля.
- `/api/ping` — публичный health-check для Android-клиента.
- `/api/connect` (POST, JSON `{code, device_name}`) — обмен `connect_code` на Bearer-токен и создание `Device`.
- `/api/me` (GET, Bearer) — кто я и какое устройство.
- `/add` (POST, Bearer) — приём сообщений. Поля формы: `sender`, `text`, `messenger_name`. 400 без обязательных полей, 401 без валидного Bearer.
- `/download`, `/download/skillwood.apk` — страница и раздача APK Android-клиента.
- `/contacts`, `/contacts/<id>`, `/contacts/manage` — UI.
- `/contacts/<id>/rename`, `/contacts/<id>/delete`, `/contacts/merge`, `/contacts/handles/<id>/move` — управление связями.
- `/messages/<id>/delete` — удаление одного сообщения.

### Миграция исторических данных

[data/migrations.py](data/migrations.py) — одноразовые скрипты, запускаются через `python -m data.migrations [имя_миграции]`. Уже отработали на проде, оставлены ради идемпотентности и тестов:

- `migrate_to_contacts_v1` — привязка старых `Messages` к новым `Contact + MessengerHandle`. Без аргумента вызывается именно она.
- `migrate_group_handles_v1` — раскладка sender'ов вида `«группа: имя»` по правильным контактам.
- `migrate_encrypt_messages_v1` — шифрование исторических `Messages.text` через [data/crypto.py](data/crypto.py).

### Тестирование

Тесты на `pytest` в [tests/](tests/). Фикстура `db_session` (в [tests/conftest.py](tests/conftest.py)) переинициализирует фабрику на in-memory SQLite через `db_sessions._reset_for_tests()` + `global_init(":memory:")`. Тесты с маршрутами создают приложение через `create_app(":memory:")` и используют `app.test_client()`.

## Соглашения, которые стоит сохранять

- Все строки, обращённые к пользователю (шаблоны, сообщения об ошибках, отладочные `print`), — на русском. При правках сохраняй язык.
- Все шаблоны под [templates/](templates/) используют Bootstrap 5 и наследуются от [base.html](templates/base.html) через `{% extends 'base.html' %}`.
- `SECRET_KEY` захардкожен как `'yandexlyceum_secret_key'` (это учебный проект Яндекс Лицея). Не меняй его без согласования с пользователем.
