#!/usr/bin/env bash
# Автозапуск claude-tg на macOS через launchd.
# Сам подставляет пути и имя пользователя — руками править ничего не нужно.
#
#   ./deploy/autostart-macos.sh            установить и запустить
#   ./deploy/autostart-macos.sh --status   посмотреть состояние
#   ./deploy/autostart-macos.sh --remove   выключить автозапуск
set -euo pipefail

LABEL="com.claude-tg.bot"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"

say()  { printf '\033[1;35m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

case "${1:-install}" in
  --status)
    launchctl list | grep -F "$LABEL" || echo "Не запущен."
    echo "Логи: $LOG_DIR/claude-tg.log"
    exit 0
    ;;
  --remove)
    launchctl unload -w "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    say "Автозапуск выключен."
    exit 0
    ;;
  install|"") ;;
  *) die "Неизвестный аргумент: $1" ;;
esac

[ "$(uname)" = "Darwin" ] || die "Скрипт для macOS. На Linux используй deploy/claude-tg.service."

PYTHON="$ROOT/.venv/bin/python"
[ -x "$PYTHON" ] || die "Нет $PYTHON — сначала запусти ./deploy/install.sh"
[ -f "$ROOT/.env" ] || die "Нет $ROOT/.env — скопируй .env.example и заполни его"

# PATH для launchd: там нет пользовательского окружения, а боту нужны node и claude.
EXTRA_PATHS=""
for binary in claude node npm; do
  location="$(command -v "$binary" 2>/dev/null || true)"
  [ -n "$location" ] && EXTRA_PATHS="$EXTRA_PATHS:$(dirname "$location")"
done
LAUNCH_PATH="$(printf '%s' "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin$EXTRA_PATHS" \
  | tr ':' '\n' | awk 'NF && !seen[$0]++' | paste -sd: -)"

command -v claude >/dev/null 2>&1 || warn "CLI 'claude' не найден в PATH — бот не сможет отвечать."

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>-m</string>
        <string>claude_tg</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$ROOT</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>$LAUNCH_PATH</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/claude-tg.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/claude-tg.err.log</string>
</dict>
</plist>
PLISTEOF

plutil -lint "$PLIST" >/dev/null || die "Сгенерированный plist некорректен: $PLIST"

launchctl unload -w "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

say "Готово. Бот запущен и будет подниматься после перезагрузки."
cat <<DONE

  Состояние:  ./deploy/autostart-macos.sh --status
  Логи:       tail -f $LOG_DIR/claude-tg.log
  Выключить:  ./deploy/autostart-macos.sh --remove

DONE
