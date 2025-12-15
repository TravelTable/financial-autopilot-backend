dev:
	docker compose up --build

migrate:
	docker compose exec api alembic upgrade head

logs:
	docker compose logs -f api worker beat
