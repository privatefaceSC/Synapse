# Автодеплой на AlwaysData

Каждый push в `dev` запускает `.github/workflows/deploy-alwaysdata.yml`:
проверка Flask, обновление серверного checkout точным коммитом, штатный
перезапуск сайта через API AlwaysData и проверка `/api/ping` до появления
новой ревизии. Если проверка не прошла, Actions помечает деплой ошибкой.
Серверная SQLite-база, Telegram-сессия и `media/` не трогаются.

## Однократная настройка

1. В AlwaysData создайте отдельный SSH-ключ для CI и добавьте его **публичную**
   часть пользователю `synapseivan` в `Remote access → SSH/SFTP → SSH keys`.
   Приватную часть сохраните только как repository secret
   `ALWAYSDATA_SSH_PRIVATE_KEY` в GitHub (`Settings → Secrets and variables →
   Actions`). SSH-пользователь AlwaysData имеет доступ ко всему аккаунту,
   поэтому ключ нельзя коммитить или выдавать сторонним workflow.
   Для беззапросного входа CI приватный ключ должен быть без пароля; проверьте
   это командой `ssh-keygen -y -P '' -f путь_к_ключу` перед загрузкой секрета.
2. Workflow получает публичный SSH-ключ AlwaysData и сверяет его с
   ED25519 SHA256-отпечатком из панели `Remote access → SSH/SFTP`.
   При смене сервера AlwaysData обновите отпечаток в workflow после проверки
   в панели. `StrictHostKeyChecking` включён.
3. В AlwaysData должна быть включена 2FA, иначе панель не даёт создать токен.
   Создайте API-ключ AlwaysData для перезапуска сайта и сохраните как
   repository secret `ALWAYSDATA_API_KEY`. Workflow обращается к
   `POST https://api.alwaysdata.com/v1/site/1075475/restart/`.
4. Проверьте, что серверный checkout лежит в `$HOME/synapse`, стоит на `dev`
   и может выполнить `git fetch origin dev` без интерактивного ввода пароля.
   У пользователя `synapseivan` уже есть рабочий `git pull` для этого
   репозитория; это подключение нужно сохранить.
5. Запушьте файлы CI в `dev`. В GitHub `Actions → Deploy Synapse to
   AlwaysData` проверьте зелёный результат. Затем проверьте
   `https://synapseivan.alwaysdata.net/api/ping`: поле `revision` должно
   совпадать с первыми 12 символами коммита.

Старое ежеминутное задание AlwaysData `git pull origin dev && touch wsgi.py`
следует оставить выключенным: оно создаёт лишнюю нагрузку, а `touch wsgi.py`
сам по себе не гарантировал загрузку нового кода uWSGI.

Если `requirements.txt` изменится, workflow до переключения коммита обновит
зависимости в серверном `.venv`. Обычные push не запускают серверный `pip`.
