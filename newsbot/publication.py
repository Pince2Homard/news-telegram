"""Publication FIFO et reprise bornée des résultats incertains."""
import time
import json
import math
from dataclasses import dataclass
import requests
from .settings import DEFAULT_TELEGRAM_INTERVAL_SECONDS as DEFAULT_TELEGRAM_INTERVAL_SECONDS, MAX_INLINE_CADENCE_WAIT_SECONDS, TELEGRAM_REQUEST_TIMEOUT_SECONDS, TELEGRAM_MAX_ATTEMPTS, TELEGRAM_MAX_RETRY_AFTER_SECONDS, MAX_TELEGRAM_SEND_BUDGET_SECONDS, MAX_PAR_RUN, get_telegram_interval_seconds
from . import persistence
from .admission import _drain_admission_buffer
from .metrics import _utc_iso, _http_status_from_exception, _http_status_category, _feed_label
from .network_diagnostics import network_diagnostic
from .settings import TELEGRAM_FALLBACK_RETRY_AFTER_SECONDS
from .settings import REMOTE_CHECKPOINT_EVERY, REMOTE_CHECKPOINT_BUDGET_SECONDS


def _log_telegram_refusal(status, payload, valid_error, classification, duration, attempt):
    """Diagnostic only: bounded values, never a free description or response body."""
    try:
        reason = "invalid_response"
        if valid_error:
            reason = _http_status_category(status)
            if status == 400:
                reason = "unknown_400"
                description = str(payload.get("description", "")).lower()
                for fragment, category in (
                    ("chat not found", "chat_not_found"),
                    ("message is too long", "too_long"),
                    ("message text is empty", "empty"),
                    ("can't parse entities", "parse_entities"),
                ):
                    if fragment in description:
                        reason = category
                        break
        print("TELEGRAM_REFUSAL |", json.dumps({
            "http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "reason": reason,
            "classification": classification,
            "duration_seconds": round(min(86400, max(0, duration)), 3)
            if math.isfinite(duration) else None,
            "attempt": min(TELEGRAM_MAX_ATTEMPTS, max(1, attempt)),
        }, sort_keys=True, separators=(",", ":")))
    except Exception:
        # A diagnostic failure must not change a publication decision.
        pass


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

    disposition: str = "uncertain"
    retry_after: int = 0

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
                allow_redirects=False,
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
            try:
                diagnostic = network_diagnostic(exc)
            except Exception:
                # Le diagnostic ne doit jamais empêcher l'arrêt prudent.
                diagnostic = {
                    "category": "diagnostic_unavailable", "phase": "unknown",
                    "http_response_attached": None, "http_response_received": None,
                }
            print(
                "TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                "| tentative:", f"{attempt + 1}/{TELEGRAM_MAX_ATTEMPTS}",
                "| résultat: réseau_ambigu | HTTP:", status if status is not None else "n/a",
                "| durée_s:", round(duration, 3), "| erreur:", type(exc).__name__,
                "| catégorie_réseau:", diagnostic["category"],
                "| phase_identifiable:", diagnostic["phase"],
                "| réponse_HTTP_attachée:", diagnostic["http_response_attached"],
                "| réponse_HTTP_reçue:", (
                    "oui" if diagnostic["http_response_received"] else "inconnue"
                ),
            )
            return TelegramSendResult(
                False, attempts=attempt + 1, attempt_errors=attempt_errors,
                duration_seconds=request_duration, http_errors=http_errors,
            )

        duration = max(0, monotonic() - request_started)
        request_duration += duration
        status = getattr(response, "status_code", None)
        try:
            payload = response.json()
        except (ValueError, TypeError):
            payload = None
        valid_error = (isinstance(payload, dict) and payload.get("ok") is False
                       and type(payload.get("error_code")) is int
                       and payload["error_code"] == status)
        if status == 200:
            result = payload.get("result") if isinstance(payload, dict) else None
            mid = result.get("message_id") if isinstance(result, dict) else None
            date = result.get("date") if isinstance(result, dict) else None
            chat = result.get("chat") if isinstance(result, dict) else None
            target = str(chat_id)
            destination_ok = isinstance(chat, dict) and type(chat.get("id")) is int and (
                (target.lstrip("-").isdigit() and type(chat.get("id")) is int
                 and str(chat["id"]) == target)
                or (target.startswith("@") and isinstance(chat.get("username"), str)
                    and chat["username"].lower() == target[1:].lower())
            )
            if isinstance(chat, dict) and type(chat.get("id")) is int and not destination_ok:
                raise GlobalTelegramError("Unexpected Telegram destination", http_status=status)
            valid = (isinstance(payload, dict) and payload.get("ok") is True
                     and type(mid) is int and mid > 0
                     and type(date) is int and 0 < date <= clock() + 300
                     and destination_ok)
            if not valid:
                _log_telegram_refusal(status, payload, False, "uncertain", duration, attempt + 1)
                return TelegramSendResult(False, attempt_errors=1)
            print("TELEGRAM tentative | début_utc:", _utc_iso(attempt_at),
                  "| résultat: succès | durée_s:", round(duration, 3), "| message_id:", mid)
            return TelegramSendResult(True, mid, date, attempt + 1, attempt_errors,
                                      request_duration, http_errors, disposition="confirmed")
        if status == 429 and valid_error:
            _log_telegram_refusal(status, payload, True, "rejected", duration, attempt + 1)
            parameters = payload.get("parameters")
            wait = parameters.get("retry_after") if isinstance(parameters, dict) else None
            if type(wait) is not int or wait <= 0:
                return TelegramSendResult(
                    False, attempts=attempt + 1, disposition="rejected",
                    retry_after=TELEGRAM_FALLBACK_RETRY_AFTER_SECONDS,
                )
            if wait > TELEGRAM_MAX_RETRY_AFTER_SECONDS or attempt == TELEGRAM_MAX_ATTEMPTS - 1:
                return TelegramSendResult(False, attempts=attempt + 1,
                                          disposition="rejected", retry_after=wait)
            attempt_errors += 1
            http_errors += 1
            sleep(wait)
            continue
        if status in (401, 403):
            # Even an invalid error envelope must not exhaust every article
            # when credentials/permissions are suspect. This is a global stop,
            # not a claim that the malformed response proves non-acceptance.
            _log_telegram_refusal(status, payload, valid_error, "global", duration, attempt + 1)
            raise GlobalTelegramError(f"HTTP {status}", http_status=status)
        if not valid_error:
            _log_telegram_refusal(status, payload, False, "uncertain", duration, attempt + 1)
            return TelegramSendResult(False, attempt_errors=1)

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
        if status in (401, 403) or (status == 400 and
                "chat not found" in str(payload.get("description", "")).lower()):
            _log_telegram_refusal(status, payload, True, "global", duration, attempt + 1)
            raise GlobalTelegramError(message, http_status=status)
        if isinstance(status, int) and 400 <= status < 500:
            description = str(payload.get("description", "")).lower()
            if status == 400 and any(reason in description for reason in (
                    "message is too long", "message text is empty", "can't parse entities")):
                _log_telegram_refusal(status, payload, True, "permanent", duration, attempt + 1)
                raise PermanentTelegramError(message, http_status=status)
            _log_telegram_refusal(status, payload, True, "global", duration, attempt + 1)
            raise GlobalTelegramError(message, http_status=status)
        _log_telegram_refusal(status, payload, True, "uncertain", duration, attempt + 1)
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
    remote_checkpoint=None,
):
    """Publie la FIFO avec transport, horloges et sauvegarde injectables.

    Mutations : retraits confirmés, erreurs définitives, promotions du tampon et
    métadonnées de cadence. La sauvegarde reçoit (state, seen, state_path).
    remote_checkpoint() doit retourner seulement après vérification distante,
    sinon lever une exception. Le contrôleur de production le fournit toujours.
    """
    if dry_run:
        print("DRY-RUN : aucune requête Telegram et aucune sauvegarde.")
        for item in state["file"]:
            print("SIMULATION |", item.get("pub") or "?", "|", _feed_label(item.get("link", "")))
        return 0

    started = monotonic()
    save = persistence.save_state if save is None else save
    interval = get_telegram_interval_seconds(interval_seconds)
    state.setdefault("last_successful_send_at", None)
    state.setdefault("last_successful_message_id", None)
    state.setdefault("admission_buffer", [])
    state.setdefault("date_quarantine", [])
    promoted_during_publication = _drain_admission_buffer(state, clock())

    # La file et les identifiants sont persistés avant toute tentative d'envoi.
    save(state, seen, state_path)
    queue_before = len(state["file"])
    sent = 0
    uncheckpointed = 0
    max_uncheckpointed = 0
    remote_checkpoints = 0
    remote_checkpoint_seconds = 0.0
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

    while state["file"] and telegram_send_operations < max_per_run:
        head = state["file"][0]
        if head.get("delivery_retry", {}).get("uncertain_count", 0) >= 3:
            state["erreurs"].append({"item": head, "error": "retry_exhausted_possible_delivery",
                                     "failed_at": int(clock()), "possible_delivery": True})
            state["file"].pop(0)
            save(state, seen, state_path)
            print("ARTICLE MIS EN QUARANTAINE | retry_exhausted_possible_delivery")
            break
        elapsed = monotonic() - started
        remaining_budget = time_limit - elapsed
        checkpoint_due = remote_checkpoint is not None and uncheckpointed + 1 >= REMOTE_CHECKPOINT_EVERY
        operation_reserve = MAX_TELEGRAM_SEND_BUDGET_SECONDS + (
            REMOTE_CHECKPOINT_BUDGET_SECONDS if checkpoint_due else 0)
        if remaining_budget < operation_reserve:
            print(
                "PUBLICATION REPORTÉE | raison: budget_insuffisant",
                "| file:", len(state["file"]),
                "| prochaine_publication_utc:", _utc_iso(next_allowed) if next_allowed is not None else "immédiate",
            )
            break

        now = clock()
        if state["file"][0].get("delivery_retry", {}).get("not_before", 0) > now:
            break
        last_success = state.get("last_successful_send_at")
        next_allowed = max(last_success + interval if last_success is not None else now,
                           state.get("last_attempt_at", 0) + interval)
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
            if wait_seconds + operation_reserve > remaining_budget:
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

        # A sleep or a local checkpoint may overrun its estimated duration.
        # Never start a new POST based on the pre-sleep budget snapshot.
        if time_limit - (monotonic() - started) < operation_reserve:
            print("PUBLICATION REPORTÉE | raison: budget_après_attente")
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
            if metrics is not None:
                metrics["publication_failed"] = True
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
            retry = item.get("delivery_retry", {})
            if isinstance(outcome, TelegramSendResult) and outcome.disposition == "rejected":
                item["delivery_retry"] = {
                    "uncertain_count": retry.get("uncertain_count", 0),
                    "last_attempt_at": clock(), "not_before": clock() + outcome.retry_after,
                }
            else:
                state["last_attempt_at"] = clock()
                count = retry.get("uncertain_count", 0) + 1
                item["delivery_retry"] = {
                    "uncertain_count": count, "last_attempt_at": clock(),
                    "not_before": clock() + (900 if count == 1 else 1800),
                }
                if metrics is not None:
                    metrics["publication_failed"] = True
                if count >= 3:
                    state["erreurs"].append({
                        "item": item, "error": "retry_exhausted_possible_delivery",
                        "failed_at": int(clock()), "possible_delivery": True,
                    })
                    state["file"].pop(0)
                    print("ARTICLE MIS EN QUARANTAINE | retry_exhausted_possible_delivery")
            save(state, seen, state_path)
            # No immediate retry: a later scheduled run may duplicate delivery.
            telegram_errors += 1
            if not isinstance(outcome, TelegramSendResult) or not outcome.attempt_errors:
                telegram_attempt_errors += 1
            print(
                "PUBLICATION INTERROMPUE | raison: resultat_non_confirme",
                "| écoulé_avant_opération_s:", round(max(0.0, call_started - started), 3),
                "| budget_avant_opération_s:", round(time_limit - (call_started - started), 3),
                "| réserve_opération_s:", MAX_TELEGRAM_SEND_BUDGET_SECONDS,
                "| publiés_confirmés:", sent,
            )
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
        uncheckpointed += 1
        max_uncheckpointed = max(max_uncheckpointed, uncheckpointed)
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
        if remote_checkpoint is not None and uncheckpointed >= REMOTE_CHECKPOINT_EVERY:
            checkpoint_started = monotonic()
            try:
                remote_checkpoint()
            except Exception as exc:
                print("ARRÊT CHECKPOINT DISTANT | confirmations_non_synchronisées:",
                      uncheckpointed, "| erreur:", type(exc).__name__)
                raise RuntimeError("Checkpoint distant non vérifié : publication arrêtée") from None
            remote_checkpoint_seconds += max(0.0, monotonic() - checkpoint_started)
            remote_checkpoints += 1
            uncheckpointed = 0
            print("CHECKPOINT DISTANT VÉRIFIÉ | publiés_confirmés:", sent,
                  "| durée_s:", round(monotonic() - checkpoint_started, 3))
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
            "remote_checkpoints": remote_checkpoints,
            "remote_checkpoint_seconds": round(remote_checkpoint_seconds, 3),
            "confirmed_since_remote_checkpoint": uncheckpointed,
            "max_confirmed_since_remote_checkpoint": max_uncheckpointed,
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
