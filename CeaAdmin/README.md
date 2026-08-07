# CeaAdmin — Веб-панель администратора

Общая веб-панель управления для экосистемы Cea (CeaAI и CeaVPN).

## Быстрый запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
python -m ceaadmin.admin_web --host 0.0.0.0 --port 8090
```

Откройте `http://127.0.0.1:8090`.

## Переменные окружения (.env)

```env
DATABASE_URL=sqlite:///./data/ceai.sqlite3
ADMIN_DATABASE_URL=postgresql://user:password@host:5432/dbname
ADMIN_WEB_HOST=0.0.0.0
ADMIN_WEB_PORT=8090
ADMIN_WEB_PASSWORD=случайный-пароль-длиной-не-менее-20-символов
ADMIN_WEB_SESSION_SECRET=отдельная-случайная-строка-длиной-не-менее-32-символов
ADMIN_TELEGRAM_IDS=123456789
```
