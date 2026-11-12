"""settings : responsabilité extraite sans changement de règle."""
import os

DEFAULT_TELEGRAM_INTERVAL_SECONDS = 30
MAX_INLINE_CADENCE_WAIT_SECONDS = 120
TELEGRAM_REQUEST_TIMEOUT_SECONDS = 30
TELEGRAM_MAX_ATTEMPTS = 3
TELEGRAM_MAX_RETRY_AFTER_SECONDS = 60
MAX_TELEGRAM_SEND_BUDGET_SECONDS = (
    TELEGRAM_MAX_ATTEMPTS * TELEGRAM_REQUEST_TIMEOUT_SECONDS
    + (TELEGRAM_MAX_ATTEMPTS - 1) * TELEGRAM_MAX_RETRY_AFTER_SECONDS
)
MAX_PAR_RUN = 50
MAX_ERREURS = 500
MAX_FILE = 1000
COLLECT_BUDGET = 180
RUN_BUDGET = 12 * 60
CHECKPOINT_EVERY = 5
MAX_METRICS_HISTORY = 12
DEFAULT_ARTICLE_MAX_AGE_HOURS = 24
FUTURE_DATE_TOLERANCE_SECONDS = 5 * 60
MAX_FUTURE_DATE_DAYS = 7
UNKNOWN_DATE_QUARANTINE_SECONDS = 24 * 3600
MAX_DATE_QUARANTINE = 500
MAX_ADMISSION_BUFFER = 1000
MAX_URL_SEEN = 5000
DEFAULT_ESTIMATED_SEND_SECONDS = 0.5


def get_telegram_interval_seconds(value=None):
    """Lit une cadence positive configurable par variable d'environnement."""
    raw = os.getenv("TELEGRAM_INTERVAL_SECONDS", str(DEFAULT_TELEGRAM_INTERVAL_SECONDS)) \
        if value is None else value
    try:
        interval = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("TELEGRAM_INTERVAL_SECONDS doit être un entier positif") from exc
    if interval <= 0:
        raise ValueError("TELEGRAM_INTERVAL_SECONDS doit être un entier positif")
    return interval


def get_article_max_age_seconds(value=None):
    """Lit la fenêtre de fraîcheur RSS configurable, en heures entières positives."""
    raw = os.getenv(
        "ARTICLE_MAX_AGE_HOURS", str(DEFAULT_ARTICLE_MAX_AGE_HOURS),
    ) if value is None else value
    try:
        hours = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("ARTICLE_MAX_AGE_HOURS doit être un entier positif") from exc
    if hours <= 0:
        raise ValueError("ARTICLE_MAX_AGE_HOURS doit être un entier positif")
    return hours * 3600
