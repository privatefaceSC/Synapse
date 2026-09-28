"""Cloud translation adapters used by the web messenger.

The application deliberately has no public translation service enabled by
default.  An administrator must choose a provider through
``TRANSLATION_PROVIDER`` and supply that provider's credentials in the
environment.  This avoids accidentally sending private chat messages to an
unknown third party.

Supported providers:

* ``libretranslate`` -- any LibreTranslate-compatible ``/translate`` REST API;
* ``yandex`` -- Yandex Cloud Translate API v2.

Only safe, user-facing errors leave this module.  Provider response bodies,
request headers and credentials must never be included in an exception shown
by the HTTP route.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import requests


DEFAULT_YANDEX_URL = (
    "https://translate.api.cloud.yandex.net/translate/v2/translate"
)
MAX_TEXT_CHARS = 10_000
REQUEST_TIMEOUT = (3.05, 15)
SUPPORTED_TARGET_LANGS = {
    "ru", "en", "es", "de", "fr", "it", "pt",
    "uk", "tr", "zh", "ja", "ko", "ar",
}


class TranslationError(RuntimeError):
    """Base error whose fields are safe to return to the browser."""

    code = "translation_error"
    public_detail = "Не удалось перевести сообщение. Попробуйте позже."
    http_status = 502

    def __init__(self, detail: str | None = None):
        self.public_detail = detail or type(self).public_detail
        super().__init__(self.public_detail)


class TranslationNotConfigured(TranslationError):
    code = "translation_not_configured"
    public_detail = "Перевод сообщений пока не настроен на сервере."
    http_status = 503


class TranslationUnavailable(TranslationError):
    code = "translation_unavailable"
    public_detail = "Сервис перевода временно недоступен. Попробуйте позже."
    http_status = 503


class TranslationRateLimited(TranslationError):
    code = "translation_limit"
    public_detail = "Лимит сервиса перевода исчерпан. Попробуйте позже."
    http_status = 503


class TranslationRequestInvalid(TranslationError):
    code = "translation_invalid"
    public_detail = "Это сообщение не удалось отправить на перевод."
    http_status = 400


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise TranslationNotConfigured()
    return value


def _normalize_target(target_lang: str) -> str:
    target = (target_lang or "").strip().lower()
    if target not in SUPPORTED_TARGET_LANGS:
        raise TranslationRequestInvalid(
            "Выбранный язык перевода не поддерживается.")
    return target


def _validate_text(text: str) -> str:
    value = (text or "").strip()
    if not value:
        raise TranslationRequestInvalid("В сообщении нет текста для перевода.")
    if len(value) > MAX_TEXT_CHARS:
        raise TranslationRequestInvalid(
            f"Сообщение для перевода не должно превышать "
            f"{MAX_TEXT_CHARS} символов.")
    return value


def _safe_post(url: str, **kwargs):
    """POST JSON and convert all transport failures to sanitized errors."""
    try:
        response = requests.post(url, timeout=REQUEST_TIMEOUT, **kwargs)
    except (requests.Timeout, requests.ConnectionError):
        raise TranslationUnavailable() from None
    except requests.RequestException:
        raise TranslationUnavailable() from None

    if response.status_code in (401, 403):
        # A bad/revoked API key is an operator configuration problem.  Do not
        # echo the provider response: it may contain account details.
        raise TranslationNotConfigured(
            "Сервис перевода настроен неверно. Сообщите администратору.")
    if response.status_code in (429, 456):
        raise TranslationRateLimited()
    if response.status_code >= 500:
        raise TranslationUnavailable()
    if response.status_code < 200 or response.status_code >= 300:
        raise TranslationError()

    try:
        payload = response.json()
    except (TypeError, ValueError):
        raise TranslationError() from None
    if not isinstance(payload, (dict, list)):
        raise TranslationError()
    return payload


def _libretranslate_url() -> str:
    raw = _required_env("LIBRETRANSLATE_URL").rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise TranslationNotConfigured(
            "Адрес сервиса перевода настроен неверно.")
    return raw if parsed.path.rstrip("/").endswith("/translate") \
        else f"{raw}/translate"


def _translate_libretranslate(text: str, target: str) -> str:
    body = {
        "q": text,
        "source": "auto",
        "target": target,
        "format": "text",
    }
    api_key = (os.environ.get("LIBRETRANSLATE_API_KEY") or "").strip()
    if api_key:
        body["api_key"] = api_key
    payload = _safe_post(
        _libretranslate_url(),
        headers={"Content-Type": "application/json"},
        json=body,
    )
    translated = payload.get("translatedText") if isinstance(payload, dict) else None
    if not isinstance(translated, str) or not translated.strip():
        raise TranslationError()
    return translated.strip()


def _translate_yandex(text: str, target: str) -> str:
    api_key = _required_env("YANDEX_TRANSLATE_API_KEY")
    folder_id = _required_env("YANDEX_TRANSLATE_FOLDER_ID")
    url = (os.environ.get("YANDEX_TRANSLATE_URL")
           or DEFAULT_YANDEX_URL).strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise TranslationNotConfigured(
            "Адрес сервиса перевода настроен неверно.")
    payload = _safe_post(
        url,
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
            # Chat messages may contain private data.  Yandex documents this
            # header as the way to opt out of server-side request logging.
            "x-data-logging-enabled": "false",
            "x-folder-id": folder_id,
        },
        json={
            "targetLanguageCode": target,
            "texts": [text],
            "folderId": folder_id,
            "format": "PLAIN_TEXT",
        },
    )
    translations = payload.get("translations") if isinstance(payload, dict) else None
    translated = (translations[0].get("text")
                  if isinstance(translations, list) and translations
                  and isinstance(translations[0], dict) else None)
    if not isinstance(translated, str) or not translated.strip():
        raise TranslationError()
    return translated.strip()


def translate(text: str, target_lang: str) -> str:
    """Translate text with the explicitly configured external provider."""
    value = _validate_text(text)
    target = _normalize_target(target_lang)
    provider = (os.environ.get("TRANSLATION_PROVIDER") or "").strip().lower()
    if provider in ("libre", "libretranslate"):
        return _translate_libretranslate(value, target)
    if provider in ("yandex", "yandex_cloud", "yandex-cloud"):
        return _translate_yandex(value, target)
    if not provider:
        raise TranslationNotConfigured()
    raise TranslationNotConfigured(
        "Неизвестный сервис перевода в настройках сервера.")
