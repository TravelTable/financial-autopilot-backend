# Financial Autopilot Backend (MVP-ready)

This backend owns Gmail access (refresh tokens), syncs finance-related emails, extracts transactions/subscriptions, schedules alerts, and drafts refund/cancel emails.

## Stack
- FastAPI (API)
- Postgres (DB)
- Redis + Celery (background sync/parsing/alerts)
- SQLAlchemy + Alembic (ORM + migrations)

## Quick start (Docker)
1. Copy env:
   ```bash
   cp .env.example .env
   ```
2. Fill in:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
   - `GOOGLE_REDIRECT_URI`
   - `JWT_SECRET`
   - `TOKEN_ENCRYPTION_KEY` (see note below)

3. Run:
   ```bash
   docker compose up --build
   ```
4. Apply migrations:
   ```bash
   docker compose exec api alembic upgrade head
   ```
5. Open API docs:
   - http://localhost:8000/docs

## TOKEN_ENCRYPTION_KEY
Generate a Fernet key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Notes
- LLM provider is optional (template + rules work without it).
- Push notification delivery is stubbed (DB + scheduling is implemented).
