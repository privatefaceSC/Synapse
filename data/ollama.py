"""Тонкий клиент к локальной Ollama (https://ollama.com).

Ollama — отдельное приложение, которое крутит open-source LLM на машине
пользователя и слушает HTTP-API на `http://localhost:11434`. Мы НЕ
тащим в зависимости проекта `torch`/`transformers`/`llama-cpp` — просто
шлём `requests.post` на /api/generate и получаем JSON-ответ.

Если Ollama не запущена — `is_available()` вернёт False, и фичи на её
основе (темы чата, транскрипция, summary) корректно скажут «нет Ollama».
"""
import json
import logging
import os
import re

import requests


_LOG = logging.getLogger(__name__)

# Локальный сервер Ollama по умолчанию. Можно перебить переменной
# окружения OLLAMA_HOST (формат "http://host:port").
DEFAULT_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

# Модель по умолчанию. qwen2.5:3b — хорошо знает русский, помещается в
# 4-6 ГБ VRAM или просто на CPU 16 ГБ RAM. Если у пользователя в env
# задано другое — берём это.
DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")


def is_available(timeout: float = 1.5) -> bool:
    """Запущена ли Ollama локально и отвечает ли. Дешёвая проверка
    (HEAD на /api/tags), результат не кэшируем здесь — кэш вешает
    вызывающий код, иначе ложно-«нет» при первом включении."""
    try:
        r = requests.get(f"{DEFAULT_HOST}/api/tags", timeout=timeout)
        return r.status_code == 200
    except requests.RequestException:
        return False


def installed_models() -> list:
    """Список скачанных моделей (имена). Пустой список если Ollama
    недоступна или моделей нет."""
    try:
        r = requests.get(f"{DEFAULT_HOST}/api/tags", timeout=3)
        if r.status_code != 200:
            return []
        data = r.json() or {}
        return [m.get("name", "") for m in (data.get("models") or [])
                if m.get("name")]
    except requests.RequestException:
        return []


def generate(prompt: str, model: str = None, temperature: float = 0.3,
             timeout: float = 600, num_ctx: int = 4096) -> str:
    """Синхронный запрос к Ollama. Возвращает сырой текст ответа.
    Бросает RuntimeError если Ollama недоступна или вернула ошибку.

    Дефолты подобраны под слабые GPU (4 ГБ VRAM, например GTX 1050 Ti):
    - num_ctx=4096 — KV-кэш помещается рядом с моделью qwen2.5:3b
      (1.9 ГБ весов + ~1 ГБ kv). При 8k+ VRAM переполняется и часть
      слоёв уходит в RAM → ответ в 5-10 раз медленнее.
    - timeout=600 — первый запрос «прогревает» модель (загрузка в VRAM
      может занять до 1 минуты), плюс генерация 200-500 токенов на
      слабой карте — ещё 1-2 минуты. Потом обращения мгновенные."""
    model = model or DEFAULT_MODEL
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
        # keep_alive=30m — после первого вопроса модель не выгружается
        # 30 минут. Следующие запросы из кэша моделей мгновенные, не
        # требуют повторной загрузки весов в VRAM.
        "keep_alive": "30m",
    }
    try:
        r = requests.post(f"{DEFAULT_HOST}/api/generate",
                          json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"Ollama недоступна: {exc}") from exc
    if r.status_code == 404:
        raise RuntimeError(
            f"Модель '{model}' не скачана. Выполни: ollama pull {model}")
    if r.status_code != 200:
        raise RuntimeError(f"Ollama вернула HTTP {r.status_code}: "
                           f"{r.text[:200]}")
    data = r.json() or {}
    return (data.get("response") or "").strip()


# Шаблон запроса для извлечения тем. Намеренно строгий и с примером —
# маленькие модели (3B) теряются от абстрактных инструкций, но few-shot
# вытягивает качество названий и полноту message_ids.
#
# Цели промпта:
# 1. Названия — КОНКРЕТНЫЕ (не «обсуждение», «разговор», «переписка»),
#    в именительном падеже, по-русски.
# 2. message_ids ОБЯЗАТЕЛЬНО заполнять — иначе подсветка в UI пустая
#    (на нашей стороне есть эвристика-фолбэк, но LLM может справиться).
# 3. Темы НЕ пересекаются по сообщениям (одно сообщение — одна тема),
#    иначе подсветка наложится и пользователь запутается.
_TOPICS_PROMPT = """Ты помощник, который анализирует кусок переписки и \
выделяет темы разговора. Сообщения помечены [id=N].

ПРАВИЛА:
1. Выдели СТОЛЬКО тем, сколько реально обсуждалось — может быть 1, \
а может 5. НЕ объединяй разные сюжеты в одну «общую переписку». \
Разные подтемы (план vs обсуждение, проблема vs решение, разные \
вопросы) — это разные темы.
2. Название каждой темы — 2-5 слов, КОНКРЕТНОЕ и по-русски. \
Плохо: «обсуждение», «разговор», «переписка», «вопросы», «дела», \
«общий чат», «новости». \
Хорошо: «Сборка APK на Gradle», «Подарок маме на день рождения», \
«Поиск работы в Яндексе», «Болезнь сына», «Поездка на дачу».
3. start_id — id САМОГО ПЕРВОГО сообщения, в котором тема началась.
4. message_ids — список ВСЕХ id сообщений, относящихся к теме, в том \
числе ответы, уточнения, итоги. НЕ оставляй только один id. \
Включи start_id в message_ids.
5. Одно сообщение относится к ОДНОЙ теме (не дублируй id между темами).

ФОРМАТ ОТВЕТА — СТРОГО JSON-массив, без пояснений до или после, без \
```json``` обёрток:
[
  {{"title": "Защита диплома", "start_id": 12345, \
"message_ids": [12345, 12346, 12350, 12378, 12381]}},
  {{"title": "Поездка на дачу с родителями", "start_id": 12678, \
"message_ids": [12678, 12679, 12690, 12702]}}
]

ВХОДНЫЕ СООБЩЕНИЯ:
{messages}

JSON-ответ:"""


def extract_topics(messages: list, model: str = None,
                   max_topics: int = 8) -> list:
    """Принимает список dict-ов [{id, text}], возвращает список тем
    [{title, start_id}]. Сообщения без текста (только медиа) фильтруются
    вызывающим кодом.

    Если LLM вернёт мусор вместо JSON — пытаемся вытащить JSON-массив
    регуляркой. Если совсем не получилось — возвращаем []."""
    if not messages:
        return []
    # Формируем компактный листинг. Длинные сообщения подрезаем —
    # 250 символов даёт LLM достаточно контекста, чтобы понять о чём
    # сообщение и связать его с темой; 120 (что было раньше) часто
    # оставляли только начало вступительного оборота без сути.
    lines = []
    for m in messages:
        mid = m.get("id")
        text = (m.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > 250:
            text = text[:250] + "…"
        lines.append(f"[id={mid}] {text}")
    if not lines:
        return []
    prompt = _TOPICS_PROMPT.format(messages="\n".join(lines))
    raw = generate(prompt, model=model, temperature=0.3)
    return _parse_topics_response(raw, max_topics)


# Промпт для инкрементальной классификации: «вот темы, которые уже есть
# в чате; вот новые сообщения; для каждого скажи к какой из существующих
# тем оно относится или создай новую». Отдельный промпт, потому что
# задача другая — не извлечение, а классификация.
_CLASSIFY_PROMPT = """Ты помощник, который продолжает разбирать переписку. \
У чата уже есть выделенные темы. Пришли НОВЫЕ сообщения — раскидай их \
по существующим темам или заведи новую тему, если ни одна не подходит.

СУЩЕСТВУЮЩИЕ ТЕМЫ:
{existing}

НОВЫЕ СООБЩЕНИЯ (помечены [id=N]):
{new_msgs}

ПРАВИЛА:
1. Для каждого нового сообщения укажи "topic": номер существующей темы \
(0, 1, 2…) ЕСЛИ сообщение явно продолжает её.
2. Если сообщение начинает что-то новое, чего нет среди существующих \
тем — укажи "topic": "NEW" и поле "title" с конкретным названием \
(2-5 слов, по-русски, не «обсуждение»).
3. Несколько подряд идущих новых сообщений на одну новую тему — давай \
им один и тот же "title" (он будет смержен в одну новую тему).
4. Не дублируй id между темами.

ФОРМАТ ОТВЕТА — СТРОГО JSON-массив, без пояснений и markdown:
[
  {{"id": 12345, "topic": 0}},
  {{"id": 12346, "topic": 2}},
  {{"id": 12347, "topic": "NEW", "title": "Покупка ноутбука"}},
  {{"id": 12348, "topic": "NEW", "title": "Покупка ноутбука"}}
]

JSON-ответ:"""


def classify_new_messages(existing_topics: list, new_messages: list,
                          model: str = None) -> list:
    """Раскидать НОВЫЕ сообщения по существующим темам или завести
    новую. `existing_topics` — список dict-ов {title, sample_text} с
    кратким примером сообщения для контекста. `new_messages` — список
    dict-ов {id, text}.

    Возвращает список dict-ов {id, topic, [title]}. Где `topic` — либо
    int (индекс existing_topics), либо строка "NEW" (и тогда есть
    `title` для новой темы)."""
    if not new_messages or not existing_topics:
        return []
    existing_lines = []
    for i, t in enumerate(existing_topics):
        sample = (t.get("sample_text") or "").replace("\n", " ").strip()
        if len(sample) > 100:
            sample = sample[:100] + "…"
        existing_lines.append(f"{i}. {t['title']} — пример: «{sample}»")
    new_lines = []
    for m in new_messages:
        text = (m.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        if len(text) > 200:
            text = text[:200] + "…"
        new_lines.append(f"[id={m['id']}] {text}")
    if not new_lines:
        return []
    prompt = _CLASSIFY_PROMPT.format(
        existing="\n".join(existing_lines),
        new_msgs="\n".join(new_lines))
    raw = generate(prompt, model=model, temperature=0.2)
    return _parse_classify_response(raw)


def _parse_classify_response(raw: str) -> list:
    """LLM возвращает JSON-массив классификаций. Парсим терпимо
    (как и в _parse_topics_response)."""
    raw = raw.strip()
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]+\]", raw)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            mid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        topic = item.get("topic")
        if topic == "NEW" or topic == "new":
            title = (item.get("title") or "").strip()
            if not title:
                continue
            out.append({"id": mid, "topic": "NEW", "title": title})
        else:
            try:
                tidx = int(topic)
            except (TypeError, ValueError):
                continue
            out.append({"id": mid, "topic": tidx})
    return out


def extract_topics_chunked(messages: list, model: str = None,
                           chunk_size: int = 100,
                           max_topics_total: int = 12) -> list:
    """Извлечение тем чанками. Маленькие модели (qwen2.5:3b) на одном
    проходе с >150 сообщений склонны выдавать одну общую тему, потому
    что промпт не помещается в 4k контекст и обрывается. Бьём ВХОД на
    куски по `chunk_size` (по умолчанию 100), для каждого зовём LLM
    отдельно, потом склеиваем результаты.

    Каждый чанк гарантированно помещается в контекст → модель видит
    весь свой кусок диалога и выдаёт 2-3 чётких темы. На большом чате
    итог: 3-6 чанков × 2-3 темы = 6-18 тем (потом обрежем до
    `max_topics_total`).

    Чанки идут в хронологическом порядке (как пришли messages), темы
    тоже сохраняют хронологию — в пин-баре UI «свежее ниже»."""
    if not messages:
        return []
    out = []
    seen_starts = set()
    for i in range(0, len(messages), chunk_size):
        chunk = messages[i:i + chunk_size]
        try:
            chunk_topics = extract_topics(chunk, model=model)
        except RuntimeError:
            # Один битый чанк не должен валить весь анализ —
            # пропускаем, идём дальше.
            continue
        for t in chunk_topics:
            # Дубль-предохранитель: если LLM в разных чанках указала
            # один и тот же start_id (граничный случай), не дублируем.
            sid = t.get("start_id")
            if sid in seen_starts:
                continue
            seen_starts.add(sid)
            out.append(t)
            if len(out) >= max_topics_total:
                return out
    return out


def _parse_topics_response(raw: str, max_topics: int) -> list:
    """LLM иногда оборачивает JSON в ```json ... ``` или добавляет
    префикс «Вот темы:». Сначала пробуем чистый json.loads, потом
    регуляркой ищем первый массив."""
    raw = raw.strip()
    parsed = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]+\]", raw)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, list):
        return []
    out = []
    for item in parsed[:max_topics]:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        try:
            start_id = int(item.get("start_id"))
        except (TypeError, ValueError):
            continue
        if not title or start_id <= 0:
            continue
        # message_ids опционально (старые ответы или модель проигнорила
        # поле) — в этом случае рассматриваем тему как «одно сообщение».
        msg_ids = []
        raw_ids = item.get("message_ids")
        if isinstance(raw_ids, list):
            for x in raw_ids:
                try:
                    msg_ids.append(int(x))
                except (TypeError, ValueError):
                    pass
        if not msg_ids:
            msg_ids = [start_id]
        out.append({
            "title": title,
            "start_id": start_id,
            "message_ids": msg_ids,
        })
    return out
