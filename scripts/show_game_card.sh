#!/usr/bin/env bash
# Витрина карточки матча на dev-боте: все виды сообщений с карточкой (рассылка, меню, /today,
# буллиты, плей-офф, сводка ночи) — кодом модулей сообщений `telegram_bot/` из рабочей копии, без
# мержа в develop. Запускается с ноутбука: вшивает их в шаблон
# `scripts/show_game_card_probe.py`, заливает пробу на VM и прогоняет в контейнере dev-стека —
# сообщения приходят в чат <chat_id> от dev-бота. Инструкция — DEVELOPMENT.md, §«Dev-стенд».
#
#   bash scripts/show_game_card.sh <chat_id>                    — главный матч: последний с овертаймом
#   bash scripts/show_game_card.sh <chat_id> <гости> <хозяева>  — главный матч: последний, напр. BUF CHI
set -euo pipefail

usage() {
  echo "Использование: $0 <chat_id> [<гости> <хозяева>]" >&2
  exit 1
}

[ $# -eq 1 ] || [ $# -eq 3 ] || usage
[[ $1 =~ ^-?[0-9]+$ ]] || usage
if [ $# -eq 3 ]; then
  [[ $2 =~ ^[A-Z]{3}$ && $3 =~ ^[A-Z]{3}$ ]] || usage
fi

VM=nhl-bot
ZONE=europe-west1-b
PROJECT=nhl-bot-prod
ROOT=$(cd "$(dirname "$0")/.." && pwd)
PROBE=$(mktemp -t show_game_card.XXXXXX)
trap 'rm -f "$PROBE"' EXIT

# Проба = шаблон + модули сообщений рабочей копии (base64: в контейнере dev-стека код develop).
# Порядок — порядок импорта: модуль идёт после тех, из которых импортирует.
MODULES="rich_markup leaderboard_messages team_messages game_messages country_messages bot_messages"
python3 - "$ROOT/telegram_bot" "$ROOT/scripts/show_game_card_probe.py" "$PROBE" $MODULES <<'PY'
import base64, sys
src_dir, template, out, *modules = sys.argv[1:]
entries = []
for name in modules:
    b64 = base64.b64encode(open(f"{src_dir}/{name}.py", "rb").read()).decode()
    chunks = "\n".join(f'        "{b64[i:i + 100]}"' for i in range(0, len(b64), 100))
    entries.append(f'    "{name}": (\n{chunks}\n    ),')
line = '_SRCS: dict = {}  # MESSAGES_BASE64: строку заменяет show_game_card.sh'
text = open(template).read()
assert text.count(line) == 1, "в шаблоне нет строки _SRCS"
open(out, "w").write(text.replace(line, "_SRCS = {\n" + "\n".join(entries) + "\n}"))
PY

gcloud compute scp "$PROBE" "$VM:/tmp/show_game_card.py" --zone "$ZONE" --project "$PROJECT"
gcloud compute ssh "$VM" --zone "$ZONE" --project "$PROJECT" --command \
  "cd /opt/nhl_bot_dev && sudo -u deploy docker compose run --rm -T --no-deps -w /app/telegram_bot \
   sync python - $* < /tmp/show_game_card.py; rm -f /tmp/show_game_card.py"
