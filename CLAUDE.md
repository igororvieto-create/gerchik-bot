# gerchik-bot

Бот-сканер сигналов и автотрейдер для Bybit (V5 API, USDT linear perps).
FastAPI + uvicorn на Railway, дашборд в static/index.html, БД — aiosqlite.

## Структура
- `strategy/scanner.py` — скоринг-движок: _classify → _direction (voting + confidence) → _score_signal (confluence caps)
- `strategy/trader.py` — вход/мониторинг позиций, риск-гарды
- `core/config.py` — все параметры (env vars)
- `core/state.py` — in-memory состояние (Signal, Position, state singleton)
- `core/db.py` — SQLite (signals, trades)
- `exchange/bybit.py` — async REST клиент Bybit V5
- `api/routes.py` — FastAPI endpoints + WebSocket
- `static/index.html` — весь фронтенд (дашборд)

## Известные рецидивирующие баги — ВСЕГДА проверять
- SL/TP исторически ставились только как чарт-маркеры, реально не долетая
  до биржи. После любого изменения в enter_trade/place_order — обязательно
  проверять, что SL/TP подтверждены через live-позицию с биржи, а не только
  что retCode==0.
- Score и direction раньше считались независимо друг от друга — при любых
  правках scoring engine проверять согласованность (confluence) факторов.

## Инварианты, которые нельзя нарушать
- Риск на сделку: максимум 1% от баланса
- Плечо: максимум 3-5x
- SL всегда обязателен перед входом — без исключений
- Не торговать новостные/листинговые спайки

## Перед тем как считать задачу выполненной
- Проверить синтаксис (python3 -m py_compile)
- Пройтись по cross-file зависимостям (scanner.py → trader.py → config.py)
- Явно перечислить: что могло сломаться в других местах от этого изменения

## Git
- Пушить в обе ветки: `git push origin main:claude/pepeto-JQ02Z` и `git push origin main`
