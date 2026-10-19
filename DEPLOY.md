# Запуск на сервере

**Одной командой с Windows** (из папки проекта, бот на компьютере перед этим остановить):

```powershell
git pull
powershell -ExecutionPolicy Bypass -File deploy\deploy.ps1 -Server root@203.0.113.10
```

Скрипт копирует на сервер `data/` и `.env`, ставит Docker, забирает код, добавляет в `.env` `WEB_SECRET`
и `DOMAIN=:80` (если их нет) и запускает бота, сайт и Caddy. Повторный запуск — обновление: база на
сервере не перезаписывается. Ниже — то же самое по шагам.

Бот и сайт работают в Docker на одном сервере и делят базу `data/bot.sqlite3`.
HTTPS делает Caddy: сертификат Let's Encrypt он получит и продлит сам.

## Что нужно

- VPS с Ubuntu 22.04/24.04, 1 ГБ памяти хватит (Timeweb, Selectel, REG.RU — от ~300 ₽/мес).
- Домен, A-запись которого указывает на IP сервера (например, `bvishechka.ru` → `203.0.113.10`).

## Первый запуск

```sh
# на сервере
curl -fsSL https://get.docker.com | sh
git clone https://github.com/fpmitucha/olymp-navigator.git /opt/olymp-navigator
cd /opt/olymp-navigator
git checkout main          # после слияния PR; до этого — claude/benefits
```

Перенеси с компьютера перечни и базу бота (подписки, кабинет). Команда для PowerShell на Windows,
из папки проекта:

```powershell
scp -r data root@203.0.113.10:/opt/olymp-navigator/
```

Перечни можно и скачать заново на сервере: `docker compose run --rm app rsr fetch` (см. README).

Создай `/opt/olymp-navigator/.env` (токен — только здесь, в git его нет):

```
TELEGRAM_BOT_TOKEN=...
BOT_ADMIN_ID=...            # твой Telegram ID: сюда приходят заявки и сообщения об ошибках
WEB_SECRET=...              # длинная случайная строка: openssl rand -hex 32
DOMAIN=bvishechka.ru
```

Перед запуском **останови бота на своём компьютере**: два экземпляра с одним токеном мешают друг другу.

```sh
docker compose up -d --build bot web
docker compose --profile https up -d caddy
docker compose logs -f bot web          # Ctrl+C — выйти из логов, сервисы продолжат работать
```

Через минуту сайт откроется на `https://<домен>`.

## Без домена — по IP-адресу

Пока домена нет, сайт можно открыть по адресу сервера: `http://203.0.113.10`. В `.env` вместо домена
напишите:

```
DOMAIN=:80
```

Caddy тогда отдаёт сайт по обычному HTTP на 80-м порту, без сертификата (Let's Encrypt выдаёт его только
на домен). Вход через Telegram и кабинет работают и так. Когда появится домен — поменяйте `DOMAIN` на него
и перезапустите Caddy: `docker compose --profile https up -d caddy` — сертификат он получит сам.

Если на сервере включён файрвол, откройте порты:

```sh
ufw allow 22 && ufw allow 80 && ufw allow 443 && ufw enable
```

## Обновление

```sh
cd /opt/olymp-navigator
git pull
docker compose up -d --build bot web
```

## Резервные копии

```sh
crontab -e
# добавить строку:
15 4 * * * cd /opt/olymp-navigator && sh deploy/backup.sh >> data/backup.log 2>&1
```

Каждую ночь копия базы ложится в `data/backup/`, хранятся 14 последних. Время от времени
скачивай их к себе: `scp root@203.0.113.10:/opt/olymp-navigator/data/backup/* .`

## Перед открытием кабинета всем

- Политика конфиденциальности и согласие на обработку персональных данных (152-ФЗ):
  храним Telegram ID, имя, класс, дипломы, заявки на сопровождение.
- Уведомление Роскомнадзора об обработке персональных данных — подаётся оператором (тобой или ИП).
