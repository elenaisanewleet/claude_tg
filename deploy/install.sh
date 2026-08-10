#!/usr/bin/env bash
# Установка claude-tg: venv, зависимости, проверка Claude Code.
# Использование:  ./deploy/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say() { printf '\033[1;35m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

say "1/4 · Проверяю Python"
PY="${PYTHON:-python3}"
"$PY" - <<'PYCODE'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"Нужен Python 3.11+, а сейчас {sys.version.split()[0]}")
print("Python", sys.version.split()[0], "— ок")
PYCODE

say "2/4 · Виртуальное окружение и зависимости"
[ -d .venv ] || "$PY" -m venv .venv
./.venv/bin/pip install --upgrade pip >/dev/null
./.venv/bin/pip install -r requirements.txt

say "3/4 · Claude Code"
if command -v claude >/dev/null 2>&1; then
  echo "Найден: $(claude --version)"
else
  warn "CLI 'claude' не найден."
  if command -v npm >/dev/null 2>&1; then
    say "Ставлю @anthropic-ai/claude-code…"
    npm install -g @anthropic-ai/claude-code@latest
  else
    warn "Нет npm. Поставь Node.js 18+, затем: npm i -g @anthropic-ai/claude-code"
    warn "Либо укажи путь к бинарнику в CLAUDE_CLI_PATH."
  fi
fi

say "4/4 · Конфигурация"
if [ ! -f .env ]; then
  cp .env.example .env
  warn "Создан .env — впиши TELEGRAM_BOT_TOKEN и OWNER_USER_ID."
else
  echo ".env уже есть — не трогаю."
fi

cat <<'DONE'

Готово. Дальше:
  1. claude auth login          # вход под своей подпиской Claude (один раз)
  2. заполни .env               # токен бота и твой user_id
  3. ./.venv/bin/python -m claude_tg

Автозапуск: deploy/claude-tg.service (Linux) или deploy/com.claude-tg.bot.plist (macOS).
DONE
