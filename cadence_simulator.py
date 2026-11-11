"""Simulation pure d'une cadence adaptative, sans accès réseau ni publication."""
from math import ceil, floor


SIMULATED_INTERVALS_SECONDS = (30, 25, 20, 15, 10)
MIN_SIMULATED_INTERVAL_SECONDS = 10
MAX_SIMULATION_HISTORY = 12
MIN_HISTORY_RUNS = 2
HYSTERESIS_OBSERVATIONS = 2


def _number(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _valid_records(history):
    return [record for record in history if isinstance(record, dict)]


def queue_trend_per_run(history, current_depth):
    """Variation moyenne de profondeur entre exécutions, sur 5 points max."""
    records = _valid_records(history)[-(MAX_SIMULATION_HISTORY - 1):]
    depths = [
        _number(record.get("queue_final"), None)
        for record in records
    ]
    depths = [depth for depth in depths if depth is not None]
    depths.append(max(0, int(current_depth)))
    if len(depths) < 2:
        return 0.0
    deltas = [right - left for left, right in zip(depths, depths[1:])]
    return sum(deltas) / len(deltas)


def estimated_arrivals_per_minute(history):
    """Estime les nouveautés RSS par minute entre les derniers passages."""
    records = _valid_records(history)[-MAX_SIMULATION_HISTORY:]
    spans = []
    for previous, current in zip(records, records[1:]):
        before = _number(previous.get("run_started_epoch"), None)
        after = _number(current.get("run_started_epoch"), None)
        discovered = _number(current.get("articles_discovered"), None)
        if before is None or after is None or discovered is None or after <= before:
            continue
        spans.append((discovered, after - before))
    total_seconds = sum(seconds for _, seconds in spans)
    if total_seconds <= 0:
        return 0.0
    return max(0.0, sum(count for count, _ in spans) * 60.0 / total_seconds)


def estimated_send_request_seconds(history, fallback=0.5):
    """Latence moyenne par requête à partir des résumés récents disponibles."""
    durations = 0.0
    attempts = 0
    for record in _valid_records(history)[-MAX_SIMULATION_HISTORY:]:
        duration = _number(record.get("telegram_request_duration_seconds"), None)
        count = _number(record.get("telegram_attempts"), None)
        if duration is None or count is None or duration < 0 or count <= 0:
            continue
        durations += duration
        attempts += count
    if attempts == 0 or durations <= 0:
        return max(0.01, _number(fallback, 0.5))
    return min(30.0, max(0.01, durations / attempts))


def _target_interval(queue_depth, trend_per_run, oldest_age_seconds):
    """Target de simulation : les seuils ne pilotent jamais le bot réel."""
    depth = max(0, int(queue_depth))
    trend = _number(trend_per_run)
    age = _number(oldest_age_seconds, None)
    if depth >= 800 or trend >= 100 or (age is not None and age >= 86400):
        return 10
    if depth >= 600 or trend >= 50 or (age is not None and age >= 21600):
        return 15
    if depth >= 350 or trend >= 20 or (age is not None and age >= 10800):
        return 20
    if depth >= 150 or trend >= 5 or (age is not None and age >= 3600):
        return 25
    return 30


def regulate_simulated_interval(
    *, queue_depth, trend_per_run, oldest_age_seconds,
    current_interval_seconds=30, candidate_interval_seconds=None,
    candidate_observations=0, history_runs=0,
):
    """Applique une hystérésis à deux observations et un pas par changement."""
    current = min(
        SIMULATED_INTERVALS_SECONDS,
        key=lambda value: abs(value - int(current_interval_seconds)),
    )
    target = _target_interval(queue_depth, trend_per_run, oldest_age_seconds)
    if history_runs < MIN_HISTORY_RUNS:
        return {
            "interval_seconds": current,
            "target_interval_seconds": target,
            "candidate_interval_seconds": None,
            "candidate_observations": 0,
            "history_sufficient": False,
            "reason": "historique_insuffisant",
        }

    if target == current:
        return {
            "interval_seconds": current,
            "target_interval_seconds": target,
            "candidate_interval_seconds": None,
            "candidate_observations": 0,
            "history_sufficient": True,
            "reason": "pression_stable",
        }

    candidate = int(candidate_interval_seconds or 0)
    observations = int(candidate_observations or 0) + 1 if candidate == target else 1
    if observations < HYSTERESIS_OBSERVATIONS:
        return {
            "interval_seconds": current,
            "target_interval_seconds": target,
            "candidate_interval_seconds": target,
            "candidate_observations": observations,
            "history_sufficient": True,
            "reason": "hysteresis_en_cours",
        }

    intervals = list(SIMULATED_INTERVALS_SECONDS)
    current_index = intervals.index(current)
    target_index = intervals.index(target)
    step = 1 if target_index > current_index else -1
    next_interval = intervals[current_index + step]
    return {
        "interval_seconds": max(MIN_SIMULATED_INTERVAL_SECONDS, next_interval),
        "target_interval_seconds": target,
        "candidate_interval_seconds": target,
        "candidate_observations": 0,
        "history_sufficient": True,
        "reason": "hysteresis_confirmee_pas_progressif",
    }


def simulate_intervals(
    *, queue_depth, available_seconds, arrivals_per_minute=0.0,
    next_run_seconds=300.0, send_request_seconds=0.5,
    send_budget_floor_seconds=210.0, initial_wait_seconds=0.0,
    max_per_run=50, max_queue=1000,
):
    """Projette les cinq cadences avec le budget et les arrivées estimés."""
    depth = max(0, int(queue_depth))
    available = max(0.0, _number(available_seconds))
    arrivals = max(0.0, _number(arrivals_per_minute))
    next_run = max(0.0, _number(next_run_seconds, 300.0))
    request = max(0.0, _number(send_request_seconds, 0.5))
    first_wait = max(0.0, _number(initial_wait_seconds))
    budget_floor = max(0.0, _number(send_budget_floor_seconds))
    scenarios = []

    for interval in SIMULATED_INTERVALS_SECONDS:
        usable = available - first_wait - budget_floor
        if depth == 0 or usable < 0:
            capacity = 0
        else:
            capacity = min(max_per_run, 1 + floor(usable / (interval + request)))
        sent = min(depth, capacity)
        remaining = depth - sent
        incoming_before_next = arrivals * next_run / 60.0
        projected_before_next = remaining + incoming_before_next
        effective_service = sent * 60.0 / next_run if next_run else float("inf")
        grows = arrivals > effective_service and (depth > 0 or arrivals > 0)

        if projected_before_next >= max_queue:
            risk = "saturation_imminente"
        elif projected_before_next >= max_queue * 0.8 or (grows and depth >= max_queue * 0.5):
            risk = "élevé"
        elif projected_before_next >= max_queue * 0.5 or grows:
            risk = "modéré"
        else:
            risk = "faible"

        if capacity == 0:
            clearance = None if depth else 0.0
        elif depth == 0:
            clearance = 0.0
        else:
            runs = ceil(depth / capacity)
            final_batch = depth - (runs - 1) * capacity
            clearance = (
                (runs - 1) * next_run
                + first_wait
                + max(0, final_batch - 1) * interval
                + final_batch * request
            )

        scenarios.append({
            "interval_seconds": interval,
            "theoretical_articles_per_hour": 3600.0 / interval,
            "capacity_this_run": capacity,
            "estimated_published_this_run": sent,
            "queue_after_run": remaining,
            "incoming_before_next_run": round(incoming_before_next, 2),
            "projected_queue_before_next_run": round(projected_before_next, 2),
            "net_growth_before_next_run": round(projected_before_next - depth, 2),
            "estimated_clearance_seconds": round(clearance, 2) if clearance is not None else None,
            "saturation_risk": risk,
        })
    return scenarios


def build_simulation(
    *, history, queue_depth, oldest_age_seconds, available_seconds,
    run_started_epoch, articles_discovered, previous_simulation=None,
    next_run_seconds=300.0, send_request_seconds=0.5,
    send_budget_floor_seconds=210.0, initial_wait_seconds=0.0,
    max_per_run=50, max_queue=1000,
):
    """Construit une recommandation de simulation et les scénarios comparatifs."""
    prior = previous_simulation if isinstance(previous_simulation, dict) else {}
    provisional_history = list(history) + [{
        "run_started_epoch": run_started_epoch,
        "articles_discovered": articles_discovered,
        "queue_final": queue_depth,
    }]
    trend = queue_trend_per_run(history, queue_depth)
    arrivals = estimated_arrivals_per_minute(provisional_history)
    recommendation = regulate_simulated_interval(
        queue_depth=queue_depth,
        trend_per_run=trend,
        oldest_age_seconds=oldest_age_seconds,
        current_interval_seconds=prior.get("interval_seconds", 30),
        candidate_interval_seconds=prior.get("candidate_interval_seconds"),
        candidate_observations=prior.get("candidate_observations", 0),
        history_runs=len(history),
    )
    scenarios = simulate_intervals(
        queue_depth=queue_depth,
        available_seconds=available_seconds,
        arrivals_per_minute=arrivals,
        next_run_seconds=next_run_seconds,
        send_request_seconds=send_request_seconds,
        send_budget_floor_seconds=send_budget_floor_seconds,
        initial_wait_seconds=initial_wait_seconds,
        max_per_run=max_per_run,
        max_queue=max_queue,
    )
    return {
        "recommendation": recommendation,
        "queue_trend_per_run": round(trend, 2),
        "estimated_arrivals_per_minute": round(arrivals, 3),
        "assumed_send_request_seconds": round(send_request_seconds, 3),
        "assumed_next_run_seconds": round(max(0.0, _number(next_run_seconds)), 2),
        "scenarios": scenarios,
    }
