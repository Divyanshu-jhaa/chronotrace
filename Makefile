.PHONY: docker-build docker-up docker-down docker-logs docker-ps docker-test

# Development: build single reusable image + start full stack (neo4j + mcp + api)
# Published image: use ghcr.io/<org>/chronotrace:0.1.0 via `docker compose --env-file .env up -d` without --build

docker-build:
	docker build -t chronotrace:dev .

docker-up:
	docker compose --env-file .env up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-ps:
	docker compose ps

docker-test:
	docker compose ps
	curl -f http://localhost:8000/health
	curl -f http://localhost:8000/ready || echo "ready not yet (neo4j starting)"
	curl -f http://localhost:8001/health || echo "mcp health not yet"
	@echo "docs: curl http://localhost:8000/docs | http://localhost:8001/docs"
