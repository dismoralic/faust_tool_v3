#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v pkg >/dev/null 2>&1; then
    printf '%s\n' 'Этот установщик предназначен для Termux.'
    exit 1
fi

printf '%s\n' 'Обновляю список пакетов Termux...'
pkg update -y

printf '%s\n' 'Устанавливаю Python и инструменты сборки...'
pkg install -y python git clang make pkg-config openssl libffi rust

printf '%s\n' 'Создаю отдельное окружение Python...'
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip setuptools wheel

printf '%s\n' 'Устанавливаю основные зависимости...'
.venv/bin/python -m pip install -r requirements-core.txt

printf '%s\n' 'Пробую установить ускорение cryptg...'
if ! .venv/bin/python -m pip install 'cryptg>=0.5,<1'; then
    printf '%s\n' 'cryptg не установился. Faust Tool продолжит работать без ускорения.'
fi

printf '%s\n' 'Настройка Telegram API и Ollama...'
.venv/bin/python setup_config.py

printf '\n%s\n' 'Установка завершена.'
printf '%s' 'Запустить Faust Tool сейчас? [Y/n] '
read -r FAUST_START
case "${FAUST_START:-Y}" in
    n|N|no|NO|нет|Нет|НЕТ)
        printf '%s\n' 'Для запуска выполни: bash start_termux.sh'
        ;;
    *)
        exec .venv/bin/python -m userbot.userbot
        ;;
esac
