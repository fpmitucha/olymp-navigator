#!/bin/sh
# Копия базы бота (подписки, кабинет, заявки) в data/backup/, хранит 14 последних.
# Cron: 15 4 * * * cd /opt/olymp-navigator && sh deploy/backup.sh >> data/backup.log 2>&1
set -e
mkdir -p data/backup
docker compose exec -T bot python -c "
import sqlite3, datetime
src = sqlite3.connect('data/bot.sqlite3')
dst = sqlite3.connect('data/backup/bot-' + datetime.date.today().isoformat() + '.sqlite3')
src.backup(dst)
dst.close()
"
ls -1t data/backup/bot-*.sqlite3 | tail -n +15 | xargs -r rm --
echo "$(date '+%F %T') backup ok"
