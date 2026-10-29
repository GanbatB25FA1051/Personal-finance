"""Runtime configuration checks for hosted deployments."""

from collections.abc import Mapping


def hosted_database_configured(environ: Mapping[str, str]) -> bool:
    """Require a persistent database on Render; local development may use SQLite."""
    if environ.get('RENDER', '').lower() != 'true':
        return True
    return bool(environ.get('DATABASE_URL', '').strip())
