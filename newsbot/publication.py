"""publication : responsabilité extraite sans changement de règle."""
import time
from dataclasses import dataclass
import requests
from .settings import DEFAULT_TELEGRAM_INTERVAL_SECONDS as DEFAULT_TELEGRAM_INTERVAL_SECONDS, MAX_INLINE_CADENCE_WAIT_SECONDS, TELEGRAM_REQUEST_TIMEOUT_SECONDS, TELEGRAM_MAX_ATTEMPTS, TELEGRAM_MAX_RETRY_AFTER_SECONDS, MAX_TELEGRAM_SEND_BUDGET_SECONDS, MAX_PAR_RUN, get_telegram_interval_seconds
from . import persistence
from .admission import _drain_admission_buffer
from .metrics import _utc_iso, _http_status_from_exception, _http_status_category, _feed_label


class PermanentTelegramError(Exception):
    """Erreur Telegram propre à un article : l’article ne pourra pas être envoyé tel quel."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


class GlobalTelegramError(Exception):
    """Erreur de configuration ou d’accès Telegram : toute la file doit être préservée."""

    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class TelegramSendResult:
    """Résultat confirmé d'un appel Telegram, sans conserver de donnée secrète."""

    success: bool
    message_id: int | str | None = None
    telegram_date: float | None = None
    attempts: int = 1
    attempt_errors: int = 0
    duration_seconds: float = 0.0
    http_errors: int = 0

    def __bool__(self):
        return self.success


def send_message(
    item, token, chat_id, http=requests, sleep=time.sleep,
    monotonic=time.monotonic, clock=time.time,
):
    attempt_errors = 0
    http_errors = 0
    request_duration = 0.0
    for attempt in range(TELEGRAM_MAX_ATTEMPTS):
        attempt_at = clock()
        request_started = monotonic()
        try:
            response = http.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": item["text"], "parse_mode": "HTML"},
                timeout=TELEGRAM_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            # Résultat ambigu : Telegram a peut-être reçu le message.
            # Ne pas réessayer automatiquement pour éviter un doublon immédiat.
            duration = max(0, monotonic() - request_started)
            request_duration += duration
            attempt_errors += 1
            status = _http_status_from_exception(exc)
            if status is not None:
                http_errors += 1
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: réseau_ambigu | HTTP:", status if status is not None else "n/a",
                "| durée_s:", round(duration, 3), "| erreur:", type(exc).__name__,
            )
            return TelegramSendResult(
                False, attempts=attempt + 1, attempt_errors=attempt_errors,
                duration_seconds=request_duration, http_errors=http_errors,
            )

        duration = max(0, monotonic() - request_started)
        request_duration += duration
        status = getattr(response, "status_code", None)
        if response.ok:
            message_id = None
            telegram_date = None
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            if isinstance(payload, dict):
                result = payload.get("result")
                if isinstance(result, dict):
                    candidate = result.get("message_id")
                    if isinstance(candidate, (int, str)) and not isinstance(candidate, bool):
                        message_id = candidate
                    candidate_date = result.get("date")
                    if (isinstance(candidate_date, (int, float))
                            and not isinstance(candidate_date, bool)):
                        telegram_date = float(candidate_date)
            confirmed_at = telegram_date if telegram_date is not None else clock()
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: succès | HTTP:", status if status is not None else "n/a",
                "| durée_s:", round(duration, 3),
                "| confirmé_utc:", _utc_iso(confirmed_at),
                "| message_id:", message_id if message_id is not None else "indisponible",
            )
            return TelegramSendResult(
                True, message_id, telegram_date, attempt + 1,
                attempt_errors, request_duration, http_errors,
            )

        if status == 429:
            attempt_errors += 1
            http_errors += 1
            try:
                wait = int(response.json()["parameters"]["retry_after"])
            except (KeyError, TypeError, ValueError):
                wait = 30
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: rate_limited | HTTP: 429",
                "| durée_s:", round(duration, 3), "| retry_after_s:", wait,
            )
            if attempt < TELEGRAM_MAX_ATTEMPTS - 1:
                sleep(min(max(wait, 1), TELEGRAM_MAX_RETRY_AFTER_SECONDS))
            continue

        message = f"HTTP {status}"
        if status is not None:
            http_errors += 1
        attempt_errors += 1
        print(
            "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
            "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
            "| résultat: échec | HTTP:", status if status is not None else "n/a",
            "| catégorie:", _http_status_category(status),
            "| durée_s:", round(duration, 3),
        )
        if status in (401, 403):
            raise GlobalTelegramError(message, http_status=status)
        if isinstance(status, int) and 400 <= status < 500:
            raise PermanentTelegramError(message, http_status=status)
        return TelegramSendResult(
            False, attempts=attempt + 1, attempt_errors=attempt_errors,
            duration_seconds=request_duration, http_errors=http_errors,
        )
    return TelegramSendResult(
        False, attempts=TELEGRAM_MAX_ATTEMPTS, attempt_errors=attempt_errors,
        duration_seconds=request_duration, http_errors=http_errors,
    )


def process_queue(
    state, seen, token, chat_id, state_path="state.json", dry_run=True,
    sender=send_message, sleep=time.sleep, max_per_run=MAX_PAR_RUN,
    interval_seconds=None, time_limit=11 * 60, clock=time.time,
    monotonic=time.monotonic, metrics=None, save=None,
):
    """Publie la FIFO avec transport, horloges et sauvegarde injectables.

    Mutations : retraits confirmés, erreurs définitives, promotions du tampon et
    métadonnées de cadence. La sauvegarde reçoit (state, seen, state_path).
    """
    if dry_run:
        print("DRY-RUN : aucune requête Telegram et aucune sauvegarde.")
        for item in state["file"]:
            print("SIMULATION |", item.get("pub") or "?", "|", _feed_label(item.get("link", "")))
        return 0

    save = persistence.save_state if save is None else save
    interval = get_telegram_interval_seconds(interval_seconds)
    state.setdefault("last_successful_send_at", None)
    state.setdefault("last_successful_message_id", None)
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    promoted_during_publication = _drain_admission_buffer(state, clock())

    # La file et les identifiants sont persistés avant toute tentative d'envoi.
    save(state, seen, state_path)
    started = monotonic()
    queue_before = len(state["file"])
    sent = 0
    cadence_wait_seconds = 0.0
    telegram_errors = 0
    telegram_send_operations = 0
    telegram_attempts = 0
    telegram_attempt_errors = 0
    telegram_http_errors = 0
    telegram_call_duration = 0.0
    telegram_request_duration = 0.0
    successful_intervals = []
    queue_max = max(queue_before, int((metrics or {}).get("queue_max", 0)))
    last_success = state.get("last_successful_send_at")
    next_allowed = last_success + interval if last_success is not None else None
    print(
        "CADENCE Telegram | intervalle_s:", interval,
        "| dernier_succès_utc:", _utc_iso(last_success) if last_success is not None else "aucun",
        "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
        "| file:", len(state["file"]),
    )

    while state["file"] and sent < max_per_run:
        elapsed = monotonic() - started
        remaining_budget = time_limit - elapsed
        if remaining_budget < MAX_TELEGRAM_SEND_BUDGET_SECONDS:
            print(
                "PUBLICATION REPORTÉE | raison: budget_insuffisant",
                "| file:", len(state["file"]),
                "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
            )
            break

        now = clock()
        last_success = state.get("last_successful_send_at")
        next_allowed = last_success + interval if last_success is not None else now
        wait_seconds = max(0, next_allowed - now)
        if wait_seconds:
            if wait_seconds > MAX_INLINE_CADENCE_WAIT_SECONDS:
                print(
                    "PUBLICATION REPORTÉE | raison: attente_trop_longue_pour_ce_workflow",
                    "| file:", len(state["file"]),
                    "| attente_s:", round(wait_seconds, 3),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break
            if wait_seconds + MAX_TELEGRAM_SEND_BUDGET_SECONDS > remaining_budget:
                print(
                    "PUBLICATION REPORTÉE | raison: budget_insuffisant_avant_prochaine_cadence",
                    "| file:", len(state["file"]),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break
            print(
                "ATTENTE CADENCE | secondes:", round(wait_seconds, 3),
                "| file:", len(state["file"]),
                "| prochaine_publication_utc:", _utc_iso(next_allowed),
            )
            sleep(wait_seconds)
            cadence_wait_seconds += max(0.0, monotonic() - (started + elapsed))
            if clock() < next_allowed:
                print(
                    "PUBLICATION REPORTÉE | raison: attente_interrompue",
                    "| file:", len(state["file"]),
                    "| prochaine_publication_utc:", _utc_iso(next_allowed),
                )
                break

        item = state["file"][0]
        call_started = monotonic()
        telegram_send_operations += 1
        try:
            outcome = sender(item, token, chat_id)
        except GlobalTelegramError as exc:
            telegram_call_duration += max(0.0, monotonic() - call_started)
            telegram_errors += 1
            telegram_attempts += 1
            telegram_attempt_errors += 1
            if getattr(exc, "http_status", None) is not None:
                telegram_http_errors += 1
            # Erreur globale d'identifiants/droits : ne jamais mettre les articles
            # en quarantaine. On arrête et conserve la file pour correction.
            print("ARRÊT DE SÉCURITÉ Telegram | file préservée |", exc)
            break
        except PermanentTelegramError as exc:
            telegram_call_duration += max(0.0, monotonic() - call_started)
            telegram_errors += 1
            telegram_attempts += 1
            telegram_attempt_errors += 1
            if getattr(exc, "http_status", None) is not None:
                telegram_http_errors += 1
            # Une erreur propre à l'article ne sera pas corrigée par une nouvelle tentative.
            # Isoler l’article défectueux pour ne pas bloquer toute la file.
            state["erreurs"].append({
                "item": item, "error": str(exc), "failed_at": int(clock()),
            })
            state["file"].pop(0)
            promoted_during_publication += _drain_admission_buffer(state, clock())
            save(state, seen, state_path)
            print("ARTICLE MIS EN QUARANTAINE |", _feed_label(item.get("link", "")), "|", exc)
            queue_max = max(queue_max, len(state["file"]))
            continue
        telegram_call_duration += max(0.0, monotonic() - call_started)
        if isinstance(outcome, TelegramSendResult):
            telegram_attempts += outcome.attempts
            telegram_attempt_errors += outcome.attempt_errors
            telegram_request_duration += outcome.duration_seconds
            telegram_http_errors += outcome.http_errors
        else:
            telegram_attempts += 1
        if not outcome:
            # Résultat ambigu ou erreur temporaire : conserver l'article en tête.
            telegram_errors += 1
            if not isinstance(outcome, TelegramSendResult) or not outcome.attempt_errors:
                telegram_attempt_errors += 1
            break

        telegram_date = outcome.telegram_date if isinstance(outcome, TelegramSendResult) else None
        message_id = outcome.message_id if isinstance(outcome, TelegramSendResult) else None
        successful_at = clock()
        if telegram_date is not None:
            # Le champ date Telegram est à la seconde : arrondir prudemment vers
            # le futur pour ne jamais raccourcir l'intervalle minimal.
            successful_at = max(successful_at, telegram_date + 1)
        previous_success = state.get("last_successful_send_at")
        if previous_success is not None and successful_at >= previous_success:
            successful_intervals.append(round(successful_at - previous_success, 3))
        state["file"].pop(0)
        sent += 1
        state["last_successful_send_at"] = successful_at
        state["last_successful_message_id"] = message_id
        promoted_during_publication += _drain_admission_buffer(state, successful_at)
        try:
            save(state, seen, state_path)
        except Exception as exc:
            print(
                "ALERTE PERSISTANCE | Telegram avait confirmé l'envoi",
                "| source:", _feed_label(item.get("link", "")),
                "| message_id:", message_id if message_id is not None else "indisponible",
                "| état_non_sauvé:", type(exc).__name__,
                "| doublon_possible_après_reprise: oui",
            )
            raise
        next_allowed = successful_at + interval
        queue_max = max(queue_max, len(state["file"]))
        print(
            "ENVOYÉ | publié:", item.get("pub") or "?",
            "| source:", _feed_label(item["link"]),
            "| confirmé_utc:", _utc_iso(successful_at),
            "| message_id:", message_id if message_id is not None else "indisponible",
            "| file:", len(state["file"]),
            "| prochaine_publication_utc:", _utc_iso(next_allowed),
        )

    save(state, seen, state_path)
    remaining = state["file"]
    publication_duration = round(max(0.0, monotonic() - started), 3)
    last_success = state.get("last_successful_send_at")
    next_allowed = last_success + interval if last_success is not None else None
    print(
        "FILE D'ATTENTE après envoi :", len(remaining), "article(s) |",
        sent, "envoyé(s) ce passage | plus ancien en attente, publié :",
        (remaining[0].get("pub") or "?") if remaining else "-",
        "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
    )
    if metrics is not None:
        metrics.update({
            "queue_before_publication": queue_before,
            "queue_final": len(remaining),
            "queue_max": max(queue_max, len(remaining)),
            "admission_buffer_final": len(state["admission_buffer"]),
            "date_quarantine_final": len(state["date_quarantine"]),
            "pending_total_final": (
                len(remaining) + len(state["admission_buffer"])
                + len(state["date_quarantine"])
            ),
            "articles_promoted_from_admission_buffer": (
                int((metrics or {}).get(
                    "articles_promoted_from_admission_buffer", 0,
                )) + promoted_during_publication
            ),
            "published": sent,
            "publication_deferred": len(remaining),
            "publication_seconds": publication_duration,
            "cadence_wait_seconds": round(cadence_wait_seconds, 3),
            "telegram_errors": telegram_errors,
            "telegram_send_operations": telegram_send_operations,
            "telegram_attempts": telegram_attempts,
            "telegram_attempt_errors": telegram_attempt_errors,
            "telegram_http_errors": telegram_http_errors,
            "telegram_call_duration_seconds": round(telegram_call_duration, 3),
            "telegram_request_duration_seconds": round(telegram_request_duration, 3),
            "intervals_seconds": successful_intervals,
            "next_publication_allowed_utc": _utc_iso(next_allowed) if next_allowed is not None else None,
        })
    return sent
