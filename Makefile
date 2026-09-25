.PHONY: backend frontend worker test migrate
backend:
	cd backend && uvicorn app.main:app --reload
frontend:
	cd frontend && npm run dev
worker:
	cd backend && python -m app.workers.runner
test:
	python -m pytest backend/tests -v
migrate:
	cd backend && alembic upgrade head
