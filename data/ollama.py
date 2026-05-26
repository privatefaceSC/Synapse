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


# Шаблон запроса для извлечения тем. Намеренно короткий и строгий —
# маленькие модели (3B) теряются от длинных multi-step инструкций.
# JSON в bare-array формате: проще парсить, меньше мест где модель может
# наврать со схемой.
_TOPICS_PROMPT = """Ниже сообщения из переписки. У каждого есть [id=N].

Твоя задача: выдели 5-8 главных тем, о которых говорили в этом чате.
Для каждой темы укажи:
- короткое название (2-6 слов);
- start_id — id ПЕРВОГО сообщения, с которого эта тема началась;
- message_ids — список ВСЕХ id сообщений, которые относятся к этой теме \
(включая start_id).

Ответь СТРОГО в формате JSON-массива, без пояснений до или после:
[
  {{"title": "Защита диплома", "start_id": 12345, \
"message_ids": [12345, 12346, 12350, 12378]}},
  {{"title": "Поездка на дачу", "start_id": 12678, \
"message_ids": [12678, 12679, 12690]}}
]

Сообщения:
{messages}
"""


def extract_topics(messages: list, model: str = None,
                   max_topics: int = 8) -> list:
    """Принимает список dict-ов [{id, text}], возвращает список тем
    [{title, start_id}]. Сообщения без текста (только медиа) фильтруются
    вызывающим кодом.

    Если LLM вернёт мусор вместо JSON — пытаемся вытащить JSON-массив
    регуляркой. Если совсем не получилось — возвращаем []."""
    if not messages:
        return []
    # Формируем компактный листинг. Длинные сообщения подрезаем — для
    # извлечения тем хватает первых ~200 символов; экономим контекст.
    lines = []
    for m in messages:
        mid = m.get("id")
        text = (m.get("text") or "").replace("\n", " ").strip()
        if not text:
            continue
        # 120 символов хватает, чтобы LLM поняла суть. На слабых GPU с
        # маленьким контекстом (4k) важно не перегружать промпт.
        if len(text) > 120:
            text = text[:120] + "…"
        lines.append(f"[id={mid}] {text}")
    if not lines:
        return []
    prompt = _TOPICS_PROMPT.format(messages="\n".join(lines))
    raw = generate(prompt, model=model, temperature=0.2)
    return _parse_topics_response(raw, max_topics)


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
