# Явный образ вместо nixpacks.
#
# nixpacks.toml переопределял только [start] — фазы setup/install целиком
# отдавались автодетекту той версии nixpacks, которую Railway запускает в
# этот день, а набор nix-пакетов нигде не был закреплён. Две сборки подряд
# упали за ~5 секунд на шаге создания образа, то есть ДО pip install: это
# единственный шаг, который в проекте ничем не фиксировался.
#
# Здесь всё закреплено: версия Python, шаги установки, команда запуска.
FROM python:3.11-slim

# PYTHONUNBUFFERED: логи uvicorn/приложения должны уходить в stdout сразу,
# иначе на Railway они появляются пачками с задержкой и отладка деплоя
# превращается в угадывание.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости отдельным слоем: правка кода не пересобирает pip install.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# PORT задаёт Railway. Форма sh -c обязательна: в exec-форме $PORT не
# раскрывается и uvicorn получает литеральную строку "$PORT".
# Фолбэк на 8000 — чтобы образ запускался и локально, без переменной.
#
# exec ОБЯЗАТЕЛЕН. В python:3.11-slim /bin/sh — это dash, а dash (в отличие
# от bash) не делает implicit exec даже для единственной команды: он форкает.
# Без exec PID 1 — это dash, uvicorn — PID 2. Ядро не доставляет init'у
# PID-namespace сигналы с диспозицией по умолчанию, а dash их детям не
# форвардит, поэтому SIGTERM от Railway до uvicorn НЕ доходит — процесс
# просто убивается SIGKILL'ом по истечении grace-периода.
# Следствие: весь блок корректного завершения в main.py (ожидание
# _SCANNING/_MONITORING/_ENTERING) не выполняется никогда, и рестарт в
# момент между принятым ордером и записью в trades оставляет позицию,
# которая после старта усыновляется как MANUAL — то есть навсегда без
# досылки стопа, вне слотов и вне дневного лимита.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
