# Telegram Hosting V1

## Local run
1. Install Python 3.12+ and Docker.
2. Edit ADMIN_PASSWORD and SECRET_KEY in docker-compose.yml.
3. Run:
   docker compose up -d --build
4. Open http://127.0.0.1:8000
5. Admin login defaults to the values in docker-compose.yml.

## User flow
Register -> Admin approves a manual payment -> user receives an active plan -> user uploads a .py bot -> Start.

## Important
This is an MVP/development version. Before selling public hosting, add:
- HTTPS + domain
- PostgreSQL
- reverse proxy
- stronger authentication/CSRF protection
- per-plan container memory/CPU enforcement
- storage quotas
- payment verification workflow
- abuse controls/rate limits
- backups and monitoring
- isolated worker nodes instead of exposing Docker socket to the web app

Never run untrusted code directly on the host OS.
