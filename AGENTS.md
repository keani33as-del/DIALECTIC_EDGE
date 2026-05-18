# AGENTS.md

Карта репозитория для AI-агентов (Devin, Codex, Cursor, Claude Code, Aider).
Цель — быстро дать контекст и предотвратить классические ошибки: правка god-object'ов,
сломанная торговая логика, регрессии в state-сохранении.

---

## TL;DR

**Что это.** Telegram-бот для AI-анализа крипто/макро рынка. Multi-agent debate
(Bull / Bear / Verifier / Synth) + own paper-autotrader на 5-мин цикле.
Single-tenant (один пользователь = один владелец бота). Деплой — Railway.

**Что критично не сломать:**

1. Торговую логику в <code>signal_trader.py</code>, <code>signals.py</code>, <code>auto_tracker.py</code>, <code>core/dynamic_risk.py</code> — на ней живут открытые позиции.
2. Сохранение state: SQLite (`dialectic_edge.db`), `risk_state.json`, `sizing_state.json`, а также markdown-кэш в git (`DIGEST_CACHE.md`, `AUTO_TRACK.md`, `FORECASTS.md`).
3. Хендлеры aiogram в `main.py` — там 70+ `@dp.message(...)` регистраций, переписывать только в рамках полной миграции в `refactor/handlers/`.

**Точка входа:** `main.py` → `async def main()` (нижняя часть файла, ~line 4900+).
Запускает `dp.start_polling(bot)` параллельно с `Scheduler` и (если `FEATURE_AUTOTRADE=1`) `run_signal_trader`.

---

## Карта файлов (что где живёт)

| Файл / каталог | Зачем | Размер |
|---|---|---|
| `main.py` | Bootstrap + все Telegram-хендлеры. **God-object**, не разрастать. | ~5378 строк |
| `signal_trader.py` | Автотрейдер (5-мин loop, vol-target, ATR stops, Split TP, Trailing). | ~2237 строк |
| `agents.py` | Multi-agent debate (Bull/Bear/Verifier/Synth/Speechwriter). | ~1696 строк |
| `signals.py` | Bybit / Binance signals: funding, OI, top-trader L/S, whales. | ~1097 строк |
| `web_search.py` | Сборщик данных: Yahoo, FRED, CoinGecko, GDELT, Tavily. | ~1669 строк |
| `database.py` | SQLite-обёртка для позиций / trade_decision_log / digest. | ~1089 строк |
| `analysis_service.py` | Pipeline `/daily`: данные → дебаты → торговый план. | – |
| `ai_provider.py` | Router LLM-провайдеров (Cerebras → Groq → Mistral → OpenRouter → Together → Gemini). | – |
| `scheduler.py` | Cron-задачи: ежедневный дайджест, audit, аудит-репорт. | – |
| `core/dynamic_risk.py` | Kelly + vol-target sizing engine. Состояние в `risk_state.json`. | – |
| `core/sizing_state.py` | Persistent state для adaptive Kelly (bake-in calibration). | – |
| `market_indicators/` | MVRV, SOPR, QE/QT, scorer, aggregator. | – |
| `refactor/handlers/` | **Целевая** структура хендлеров (debate/market/profile/portfolio/admin). Импортируется из `main.py`, но 70 `@dp.message` всё ещё живут в `main.py`. | – |
| `refactor/providers/` | AI / cache / database / market / news / storage providers (новый стиль). | – |
| `tests/` | 252 unit-теста на unittest. CI запускает их в двух job'ах (`unit-fast` + `unit-full`). | – |

**Состояние в git (anti-pattern, исторически):** `DIGEST_CACHE.md` (~497 KB),
`FORECASTS.md`, `AUTO_TRACK.md` — markdown'ы, в которые бот пишет дайджесты и
аудит-логи через GitHub API. Не править руками, не удалять из истории без user-approval.

---

## Что **уже** настроено

- `pyproject.toml` — ruff (soft: `E`/`F` only), mypy (soft), pytest (asyncio strict).
- `.pre-commit-config.yaml` — ruff (no-fix) + базовые pre-commit-hooks (whitespace,
  merge-conflicts, check-yaml, check-added-large-files >200 KB).
- `.github/workflows/lint.yml` — ruff + mypy (refactor/ only), non-blocking.
- `.github/workflows/tests.yml` — два job'а:
  - `unit-fast` — minimal deps (aiohttp/aiosqlite/feedparser/numpy/certifi), ~30 сек
  - `unit-full` — полный `requirements.txt` + smoke-imports 13 модулей + 252 теста, ~1.5–2 мин
- `.env.example` — все переменные окружения с комментариями.
- `CONTRIBUTING.md` — короткий гайд для контрибьюторов.

## Что **ещё не** настроено (если просят — это roadmap)

- Domigration `@dp.message` из `main.py` → `refactor/handlers/*.register(dp)` (95% сделано, остались сами регистрации).
- State в Postgres + alembic (сейчас SQLite + markdown в git).
- Backtest harness (есть `backtester.py` 12 KB как заготовка, не reproducible).
- `/healthz` HTTP endpoint (для Railway HA).
- Rate-limiter на telegram-команды (нет ни одного — есть риск зачерпнуть AI-провайдеры).
- Sentry / structured logging.
- Web-UI (только Telegram сейчас).

---

## Правила для агентов

### Don'ts (порядок убывания опасности)

1. **Не трогай торговую логику** в `signal_trader.py`, `signals.py`, `core/dynamic_risk.py`, `auto_tracker.py` без явной просьбы. Если правишь — обязательно покрывай тестами.
2. **Не правь** `DIGEST_CACHE.md`, `AUTO_TRACK.md`, `FORECASTS.md` руками. Их пишет бот через GitHub API.
3. **Не добавляй** `@dp.message(...)` в `main.py`. Новые хендлеры — в `refactor/handlers/*` и регистрируй через register-функцию.
4. **Не вводи** новые зависимости в `requirements.txt` без обсуждения. CI ставит полный `requirements.txt` в `unit-full` job, новые пакеты замедляют CI.
5. **Не force-push** на master. На своей ветке — только `git push --force-with-lease` после rebase.
6. **Не коммить** секреты. Шаблоны в `.env.example` — это шаблоны, реальные значения только в `.env` (gitignored) или Railway Variables.
7. **Не делай большие PR.** Один PR — одна тема. Гигиена, рефакторинг и фича — три разных PR.

### Do's

1. **Перед любой правкой** — прочитай `pyproject.toml` и `.pre-commit-config.yaml`.
2. **Запусти тесты** локально: `python -m unittest discover -s tests -p "test_*.py"` — должны быть 252 / 252.
3. **Для нового кода** — старайся попадать в `refactor/handlers/*` или `refactor/providers/*`. В корне — только если правишь существующий файл.
4. **Тесты пиши** в `tests/test_<feature>.py` стиля unittest (миграция на pytest опциональна — pytest умеет запускать unittest без переписывания).
5. **Smoke-import**: если меняешь handler/provider — он должен импортироваться без сайд-эффектов. CI это проверит в `unit-full` job.
6. **Любая фича за фичефлагом** — `os.getenv("FEATURE_XXX", "0")`. По умолчанию OFF.

### Если правишь `main.py`

- Считай, что любая правка > 50 строк требует review владельца.
- НЕ перемешивай boostrap-код (`async def main`) с handler'ами.
- Используй существующие helpers вместо копипаста (`_main_menu_kb`, `persistent_kb`, `feedback_keyboard` и т.п.).

### Если правишь автотрейдер

- Прочитай `tests/test_autotrade_*.py` — там whiplash-сценарии, reentry-cooldown, sizing.
- `signal_trader.py:_close_position_if_needed` — самая горячая функция. Любая правка → 3 теста минимум.
- Anti-whiplash параметры (`AUTOTRADE_MIN_HOLD_MINUTES`, `AUTOTRADE_REVERSAL_STRENGTH_DELTA`, `AUTOTRADE_REENTRY_COOLDOWN_MIN`) защищают капитал. Не повышай thresholds без бэктеста.

---

## Workflow для типичных задач

### «Добавить новую Telegram-команду»

1. Создай файл в `refactor/handlers/<feature>_handler.py` с `register(dp)` функцией.
2. В `register(dp)` повесь `dp.message.register(handler, Command("xxx"))`.
3. В `main.py` вызови `<feature>_handler.register(dp)` рядом с другими `register(dp)`.
4. Тест: `tests/test_<feature>.py` со stub'ом `aiogram.types.Message`.

### «Поправить логику дебатов»

1. Только в `agents.py` или `analysis_service.py`. Не трогай `main.py`.
2. Добавь regression test'ы — `tests/test_agents_*.py` уже есть как пример.

### «Изменить параметр риска»

1. В `core/dynamic_risk.py` или `config.py`. Сделай его env-переменной с дефолтом.
2. Допиши `.env.example`.
3. Тест: проверка что fallback к дефолту работает при пустом env.

### «Починить CI»

1. Сначала прочитай `.github/workflows/tests.yml` и `lint.yml`.
2. Воспроизведи локально: `pip install -r requirements.txt && python -m unittest discover -s tests`.
3. Если ruff/mypy ругаются — починить **только новые** нарушения, легаси не трогать (`continue-on-error: true` на lint job).

### «Поднять Postgres миграцию» (на будущее)

1. Это **не** делается за 1 PR. Сначала — alembic init + dual-write (Postgres + SQLite параллельно).
2. Затем — переключение читателей. Затем — отключение SQLite. Минимум 3 PR.
3. Не начинай эту работу без явного approval'а владельца.

---

## Контакты / источники

- Главный репо: <https://github.com/keani33as-del/DIALECTIC_EDGE>
- Подробное описание архитектуры: [README.md](README.md)
- Переменные окружения: [.env.example](.env.example)
- Правила контрибуции: [CONTRIBUTING.md](CONTRIBUTING.md)
