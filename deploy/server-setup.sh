#!/bin/sh
# Ставит Docker, забирает код и запускает бота + сайт (+ Caddy на :80). Запускать на сервере от root.
# Повторный запуск = обновление: код подтягивается, база на сервере не перезаписывается.
set -e
DIR=/opt/olymp-navigator
BRANCH=${BRANCH:-claude/benefits}
REPO=https://github.com/fpmitucha/olymp-navigator.git

command -v git >/dev/null 2>&1 || { apt-get update -qq && apt-get install -y -qq git; }
command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh

if [ -d "$DIR/.git" ]; then
  cd "$DIR" && git fetch -q origin && git checkout -q "$BRANCH" && git pull -q origin "$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR" && cd "$DIR"
fi

# данные с компьютера — только при первом запуске, чтобы не затереть базу сервера
if [ -d /tmp/data ] && [ ! -f data/bot.sqlite3 ]; then
  mkdir -p data && cp -r /tmp/data/. data/
fi
rm -rf /tmp/data
if [ -f /tmp/.env ]; then
  [ -f .env ] || tr -d '\r' < /tmp/.env > .env
  rm -f /tmp/.env
fi
[ -f .env ] || { echo "ОШИБКА: нет .env (TELEGRAM_BOT_TOKEN=...)"; exit 1; }
grep -q '^WEB_SECRET=..' .env || echo "WEB_SECRET=$(openssl rand -hex 32)" >> .env
grep -q '^DOMAIN=' .env || echo "DOMAIN=:80" >> .env

if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80 >/dev/null; ufw allow 443 >/dev/null
fi

docker compose up -d --build bot web
docker compose --profile https up -d caddy

IP=$(curl -fsS4 --max-time 5 https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "Готово. Сайт: http://$IP   Логи: cd $DIR && docker compose logs -f bot web"
