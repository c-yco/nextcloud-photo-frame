# Changelog

All notable changes to this project will be documented in this file.

## [v0.1.5] - 2026-01-05
### Fixed
- Set `PYTHONUNBUFFERED=1` in Dockerfile to ensure worker logs are visible in stdout.

## [v0.1.4] - 2026-01-05
### Changed
- Changed application port from 5000 (internal) / 80 (external) to 7880.

## [v0.1.3] - 2026-01-05
### Changed
- Removed Nginx reverse proxy.
- Exposed Gunicorn directly on the configured port.

## [v0.1.2] - 2026-01-05
### Changed
- Switched from Flask development server to Gunicorn for production.

## [v0.1.1] - 2026-01-05
### Documentation
- Updated Taskfile usage instructions.

## [v0.1.0] - 2026-01-05
### Added
- Release automation using Taskfile and GitHub Actions.
- Multi-language support (EN, DE, FR, ES).
- Cron scheduling for photo scanning.
- Weather display integration.
- Redis persistence.
- Docker GHCR image migration.
