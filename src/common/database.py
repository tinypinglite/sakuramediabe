from src.config.config import settings
from src.model import get_database, init_database


def ensure_database_ready():
    try:
        database = get_database()
    except RuntimeError:
        database = init_database(settings.database)
    if database.is_closed():
        database.connect()
    return database
