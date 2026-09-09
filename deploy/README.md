# Деплой Synapse на бесплатный 24/7 VPS

Этот вариант рассчитан на Oracle Cloud Always Free VM с Ubuntu. GitHub Pages
для Synapse не подходит, потому что проекту нужен живой Python/Flask-процесс,
SQLite-база, Telegram-сессия Telethon и папка с медиа.

## Что уже подготовлено в репозитории

- `wsgi.py` - точка входа для production WSGI-сервера.
- `requirements-prod.txt` - обычные зависимости плюс Gunicorn для Linux.
- `deploy/synapse.service` - systemd-сервис, который будет держать Synapse
  запущенным после перезагрузки сервера.
- `deploy/nginx-synapse.conf` - nginx-прокси снаружи на порт 80.
- `deploy/env.example` - шаблон переменных окружения без реальных секретов.

## Что сделать на сервере

Создай Ubuntu VM, открой входящие порты 80 и 443 в правилах сети Oracle,
зайди на сервер по SSH и выполни:

```bash
sudo apt update
sudo apt install -y git python3-venv nginx
sudo mkdir -p /srv
sudo chown "$USER:$USER" /srv
cd /srv
git clone -b dev YOUR_GITHUB_REPO_URL synapse
cd /srv/synapse
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-prod.txt
```

Подготовь секреты:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
sudo mkdir -p /etc/synapse
sudo cp deploy/env.example /etc/synapse/synapse.env
sudo nano /etc/synapse/synapse.env
sudo chmod 600 /etc/synapse/synapse.env
```

В `/etc/synapse/synapse.env` обязательно заполни:

- `SKILLWOOD_ENCRYPTION_KEY` - ключ из команды выше.
- `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` - из https://my.telegram.org/apps.
- `TELEGRAM_OWNER_USER_ID` - обычно `1`, если в базе первый пользователь твой.

Запусти приложение как сервис:

```bash
sudo cp deploy/synapse.service /etc/systemd/system/synapse.service
sudo systemctl daemon-reload
sudo systemctl enable --now synapse
sudo systemctl status synapse --no-pager
```

Подключи nginx:

```bash
sudo cp deploy/nginx-synapse.conf /etc/nginx/sites-available/synapse
sudo ln -sf /etc/nginx/sites-available/synapse /etc/nginx/sites-enabled/synapse
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

После этого сайт должен открываться по публичному IP сервера:

```text
http://YOUR_SERVER_IP/
```

## Обновление с GitHub

Когда правки уже запушены в `origin/dev`, на сервере достаточно:

```bash
cd /srv/synapse
git pull origin dev
. .venv/bin/activate
pip install -r requirements-prod.txt
sudo systemctl restart synapse
```

## Полезные проверки

```bash
sudo journalctl -u synapse -f
sudo systemctl restart synapse
sudo nginx -t
```

Если имя пользователя на сервере не `ubuntu`, замени `User=ubuntu` и
`Group=ubuntu` в `/etc/systemd/system/synapse.service` на своё имя.
