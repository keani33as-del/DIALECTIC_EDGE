# Contributing to Dialectic Edge

Короткий гайд для контрибьюторов и AI-агентов (Devin / Codex / Cursor / Claude Code).
Полный контекст архитектуры — в [AGENTS.md](AGENTS.md).

## Setup

```bash
git clone https://github.com/keani33as-del/DIALECTIC_EDGE.git
cd DIALECTIC_EDGE
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Lint / pre-commit hooks (одноразово после клона)
pip install ruff==0.7.4 mypy==1.13.0 pre-commit
pre-commit install

cp .env.example .env  # затем заполни ключи
```

Python: **3.12** (см. `.python-version` / `pyproject.toml`).

## Команды повседневной работы

| Что | Команда |
|---|---|
| Запустить тесты | `python -m unittest discover -s tests -p "test_*.py" -v` |
| Ruff (errors only) | `ruff check --select=E,F .` |
| Mypy (soft mode, только refactor/) | `mypy refactor/` |
| Pre-commit на все файлы | `pre-commit run --all-files` |
| Запустить бота локально | `python main.py` (нужен `BOT_TOKEN`) |

## PR-правила

1. **Ветка от master**, имя: `devin/<timestamp>-<slug>` или `feature/<slug>`.
2. **Один PR — одна тема.** Гигиена, рефакторинг хендлера и фичевый код — три разных PR.
3. **CI должен быть зелёный** до запроса ревью:
   - `unit-fast` (minimal deps) — быстрая обратная связь
   - `unit-full` (full requirements.txt + smoke-imports) — handlers/AI/charts реально проверяются
   - `ruff` / `mypy (soft)` — non-blocking, но не должны добавлять новых нарушений
4. **Тесты обязательны** для нового кода в `signal_trader.py`, `signals.py`, `auto_tracker.py`,
   `core/dynamic_risk.py`, `core/sizing_state.py` — это торговая логика, регрессий не прощает.
5. **Не коммить** `.env`, `*.db`, `risk_state.json`, `sizing_state.json`, секреты в README.
6. **Не правь** `DIGEST_CACHE.md` / `FORECASTS.md` / `AUTO_TRACK.md` вручную — их пишет бот.

## Стиль кода

- `ruff` сконфигурирован мягко: ловим `E`/`F` (реальные баги), игнорируем cosmetic
  (`E501` line length, `E701/E702/E741`, `F541`) — см. `pyproject.toml`.
- `mypy` в soft-режиме: `ignore_missing_imports=true`, `check_untyped_defs=false`.
  Для нового кода в `refactor/` — старайся аннотировать сигнатуры.
- Не вводи новые зависимости в `requirements.txt` без обсуждения в issue/PR.
- Markdown-файлы (`*.md`) исключены из pre-commit-хуков — их часто пишет бот.

## Что **не** делать

- Не правь `main.py` без необходимости — там 5378 строк, любое изменение трудно review'ить.
  Новые хендлеры — в `refactor/handlers/*`.
- Не меняй сигнатуры функций в `signals.py` / `signal_trader.py` — на них завязаны тесты
  и активные позиции в БД.
- Не делай `git push --force` на master. На своих feature-ветках — только
  `--force-with-lease` после rebase.
- Не коммить файлы > 200 KB (pre-commit это блокирует) — состояние храним в SQLite,
  а не в git.

## Полезные ссылки

- [AGENTS.md](AGENTS.md) — карта репо для AI-агентов
- [README.md](README.md) — описание продукта + архитектура
- [.env.example](.env.example) — все переменные окружения
- CI: `.github/workflows/tests.yml` (unit) + `.github/workflows/lint.yml` (ruff/mypy)
