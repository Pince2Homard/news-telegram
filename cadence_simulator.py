"""Simulation pure d'une cadence adaptative, sans accès réseau ni publication."""
from math import ceil, floor, isfinite
from statistics import median


SIMULATED_INTERVALS_SECONDS = (30, 25, 20, 15, 10)
MIN_SIMULATED_INTERVAL_SECONDS = 10
MAX_SIMULATION_HISTORY = 12
MIN_HISTORY_RUNS = 2
HYSTERESIS_OBSERVATIONS = 2


def _number(value, default=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if isfinite(number) else default


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
    """Estime les articles admissibles par minute entre les derniers passages."""
    records = _valid_records(history)[-MAX_SIMULATION_HISTORY:]
    spans = []
    for previous, current in zip(records, records[1:]):
        before = _number(previous.get("run_started_epoch"), None)
        after = _number(current.get("run_started_epoch"), None)
        admissible = _number(
            current.get("articles_admissible", current.get("articles_discovered")), None,
        )
        if before is None or after is None or admissible is None or after <= before:
            continue
        spans.append((admissible, after - before))
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


def estimated_send_operation_seconds(history, fallback=0.5):
    """Durée moyenne d'un appel d'envoi, y compris ses éventuelles reprises."""
    durations = 0.0
    operations = 0
    for record in _valid_records(history)[-MAX_SIMULATION_HISTORY:]:
        duration = _number(record.get("telegram_call_duration_seconds"), None)
        count = _number(record.get("telegram_send_operations"), None)
        if duration is None or count is None or duration < 0 or count <= 0:
            continue
        durations += duration
        operations += count
    if operations == 0 or durations <= 0:
        return max(0.01, _number(fallback, 0.5))
    return min(210.0, max(0.01, durations / operations))


def estimate_workflow_interval(history, fallback_seconds=300.0):
    """Médiane des démarrages observés, ou repli sur le cron de cinq minutes."""
    starts = [
        _number(record.get("run_started_epoch"), None)
        for record in _valid_records(history)
    ]
    starts = [value for value in starts if value is not None][-6:]
    gaps = [right - left for left, right in zip(starts, starts[1:]) if right > left]
    if not gaps:
        fallback = max(1.0, _number(fallback_seconds, 300.0))
        return {
            "seconds": fallback,
            "source": "cron_fallback_no_observed_gap",
            "samples": 0,
            "minimum_observed_seconds": None,
            "maximum_observed_seconds": None,
            "spread_seconds": None,
            "guaranteed": False,
        }
    value = median(gaps)
    return {
        "seconds": value,
        "source": "median_of_recent_observed_start_gaps",
        "samples": len(gaps),
        "minimum_observed_seconds": min(gaps),
        "maximum_observed_seconds": max(gaps),
        "spread_seconds": max(gaps) - min(gaps),
        "guaranteed": False,
    }


def estimated_nonpublishing_seconds(history):
    """Médiane du temps historique hors collecte et hors diffusion."""
    estimates = []
    for record in _valid_records(history)[-MAX_SIMULATION_HISTORY:]:
        total = _number(record.get("total_seconds"), None)
        collection = _number(record.get("collection_seconds"), None)
        publication = _number(record.get("publication_seconds"), None)
        if total is None or collection is None or publication is None:
            continue
        remainder = total - collection - publication
        if remainder >= 0:
            estimates.append(remainder)
    return median(estimates) if estimates else 0.0


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


def _first_wait_seconds(interval_seconds, seconds_since_last_success):
    if seconds_since_last_success is None:
        return 0.0
    elapsed = max(0.0, _number(seconds_since_last_success))
    return max(0.0, interval_seconds - elapsed)


def _capacity_for_run(
    *, queue_depth, available_seconds, interval_seconds, send_operation_seconds,
    send_budget_floor_seconds, first_wait_seconds, max_per_run,
):
    """Mirrors process_queue's reserve, first wait, then success-gap loop."""
    if queue_depth <= 0 or max_per_run <= 0:
        return 0
    available = max(0.0, _number(available_seconds))
    wait = max(0.0, _number(first_wait_seconds))
    operation = max(0.0, _number(send_operation_seconds))
    reserve = max(0.0, _number(send_budget_floor_seconds))
    required_before_first_send = max(reserve, operation)
    remaining_after_first_slot = available - wait - required_before_first_send
    if remaining_after_first_slot < -1e-9:
        return 0

    # process_queue checks that the next cadence wait plus the send reserve fits
    # after each sender call; the sender latency elapses before that wait begins.
    start_to_start = interval_seconds + operation
    further_slots = floor(max(0.0, remaining_after_first_slot) / start_to_start + 1e-9)
    return min(max_per_run, 1 + further_slots)


def simulate_intervals(
    *, queue_depth, available_seconds, arrivals_per_minute=0.0,
    next_run_seconds=300.0, send_request_seconds=0.5,
    send_operation_seconds=None, workflow_nonpublishing_seconds=0.0,
    seconds_since_last_success=None, send_budget_floor_seconds=210.0,
    max_per_run=50, max_queue=1000,
):
    """Compare cadence scenarios against the bot's bounded-send loop.

    Clearance values are conditional model estimates. GitHub schedule starts are
    not guaranteed; workflow overlap lengthens the modeled cycle and lowers
    forecast quality rather than reusing an impossible cron-only cycle.
    """
    depth = max(0, int(queue_depth))
    available = max(0.0, _number(available_seconds))
    arrivals = max(0.0, _number(arrivals_per_minute))
    nominal_next_run = max(0.0, _number(next_run_seconds, 300.0))
    request = max(0.01, _number(send_request_seconds, 0.5))
    operation = max(
        0.01,
        _number(send_operation_seconds, request) if send_operation_seconds is not None else request,
    )
    nonpublishing = max(0.0, _number(workflow_nonpublishing_seconds))
    budget_floor = max(0.0, _number(send_budget_floor_seconds))
    limit = max(0, int(max_per_run))
    queue_limit = max(1, int(max_queue))
    scenarios = []

    for interval in SIMULATED_INTERVALS_SECONDS:
        first_wait = _first_wait_seconds(interval, seconds_since_last_success)
        capacity = _capacity_for_run(
            queue_depth=depth,
            available_seconds=available,
            interval_seconds=interval,
            send_operation_seconds=operation,
            send_budget_floor_seconds=budget_floor,
            first_wait_seconds=first_wait,
            max_per_run=limit,
        )
        sent = min(depth, capacity)
        remaining = depth - sent
        batch_seconds = (
            first_wait + sent * operation + max(0, sent - 1) * interval
            if sent else 0.0
        )
        workflow_seconds = nonpublishing + batch_seconds
        schedule_overlap = workflow_seconds > nominal_next_run
        effective_cycle = max(nominal_next_run, workflow_seconds)

        # The next run has normally had at least one full workflow cycle since
        # its previous successful send. Use the remaining wait if the modeled
        # cycle is shorter than the selected interval.
        elapsed_since_last_at_next_run = (
            max(0.0, effective_cycle - workflow_seconds)
            if sent else (
                max(0.0, _number(seconds_since_last_success))
                + max(0.0, effective_cycle - nonpublishing)
                if seconds_since_last_success is not None else None
            )
        )
        next_wait = _first_wait_seconds(interval, elapsed_since_last_at_next_run)
        next_capacity = _capacity_for_run(
            queue_depth=max(1, remaining),
            available_seconds=available,
            interval_seconds=interval,
            send_operation_seconds=operation,
            send_budget_floor_seconds=budget_floor,
            first_wait_seconds=next_wait,
            max_per_run=limit,
        )
        incoming_before_next = arrivals * effective_cycle / 60.0
        projected_before_next = remaining + incoming_before_next
        projected_after_next = max(0.0, projected_before_next - next_capacity)
        net_drain_per_cycle = next_capacity - incoming_before_next
        grows = net_drain_per_cycle < 0 and (remaining > 0 or incoming_before_next > 0)

        if projected_before_next >= queue_limit:
            risk = "saturation_imminente"
        elif projected_before_next >= queue_limit * 0.8 or (grows and remaining >= queue_limit * 0.5):
            risk = "élevé"
        elif projected_before_next >= queue_limit * 0.5 or grows:
            risk = "modéré"
        else:
            risk = "faible"

        clearance = None
        if depth == 0:
            clearance_status = "queue_empty"
            clearance = 0.0
        elif capacity == 0:
            clearance_status = "budget_insufficient"
        elif remaining == 0:
            clearance_status = "cleared_this_run"
            clearance = batch_seconds
        elif next_capacity == 0:
            clearance_status = "no_future_capacity"
        elif net_drain_per_cycle <= 0:
            clearance_status = "arrivals_at_or_above_service"
        else:
            clearance_status = (
                "conditional_schedule_overlap" if schedule_overlap
                else "conditional_estimate"
            )
            future_cycles = ceil(remaining / net_drain_per_cycle)
            clearance = batch_seconds + future_cycles * effective_cycle

        scenarios.append({
            "interval_seconds": interval,
            "cadence_only_articles_per_hour": 3600.0 / interval,
            "latency_adjusted_articles_per_hour": 3600.0 / (interval + operation),
            "first_wait_seconds": round(first_wait, 3),
            "capacity_this_run": capacity,
            "estimated_published_this_run": sent,
            "queue_after_run": remaining,
            "estimated_send_operation_seconds": round(operation, 3),
            "estimated_http_request_seconds": round(request, 3),
            "publication_batch_seconds": round(batch_seconds, 3),
            "estimated_workflow_seconds": round(workflow_seconds, 3),
            "assumed_start_interval_seconds": round(nominal_next_run, 3),
            "effective_cycle_seconds": round(effective_cycle, 3),
            "workflow_duration_exceeds_assumption": schedule_overlap,
            "first_wait_next_run_seconds": round(next_wait, 3),
            "capacity_next_run": next_capacity,
            "incoming_before_next_run": round(incoming_before_next, 2),
            "projected_queue_before_next_run": round(projected_before_next, 2),
            "projected_queue_after_next_run": round(projected_after_next, 2),
            "net_growth_before_next_run": round(projected_before_next - depth, 2),
            "estimated_clearance_seconds": round(clearance, 2) if clearance is not None else None,
            "clearance_status": clearance_status,
            "clearance_forecast_reliable": False,
            "clearance_forecast_quality": "faible_chevauchement" if schedule_overlap else "indicatif_seulement",
            "saturation_risk": risk,
        })
    return scenarios


def build_simulation(
    *, history, queue_depth, oldest_age_seconds, available_seconds,
    run_started_epoch, articles_discovered=0, articles_admissible=None,
    previous_simulation=None, next_run_seconds=300.0, send_request_seconds=0.5,
    send_operation_seconds=None, workflow_nonpublishing_seconds=None,
    seconds_since_last_success=None, send_budget_floor_seconds=210.0,
    max_per_run=50, max_queue=1000,
):
    """Construit une recommandation de simulation et les scénarios comparatifs."""
    prior = previous_simulation if isinstance(previous_simulation, dict) else {}
    admissible = articles_discovered if articles_admissible is None else articles_admissible
    workflow_interval = estimate_workflow_interval(history, next_run_seconds)
    nonpublishing = (
        estimated_nonpublishing_seconds(history)
        if workflow_nonpublishing_seconds is None
        else max(0.0, _number(workflow_nonpublishing_seconds))
    )
    operation_seconds = (
        estimated_send_operation_seconds(history, fallback=send_request_seconds)
        if send_operation_seconds is None else max(0.01, _number(send_operation_seconds))
    )
    provisional_history = list(history) + [{
        "run_started_epoch": run_started_epoch,
        "articles_discovered": articles_discovered,
        "articles_admissible": admissible,
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
        next_run_seconds=workflow_interval["seconds"],
        send_request_seconds=send_request_seconds,
        send_operation_seconds=operation_seconds,
        workflow_nonpublishing_seconds=nonpublishing,
        seconds_since_last_success=seconds_since_last_success,
        send_budget_floor_seconds=send_budget_floor_seconds,
        max_per_run=max_per_run,
        max_queue=max_queue,
    )
    return {
        "recommendation": recommendation,
        "queue_trend_per_run": round(trend, 2),
        "estimated_arrivals_per_minute": round(arrivals, 3),
        "assumed_send_operation_seconds": round(operation_seconds, 3),
        "assumed_http_request_seconds": round(send_request_seconds, 3),
        "assumed_workflow_nonpublishing_seconds": round(nonpublishing, 3),
        "workflow_interval_assumption": workflow_interval,
        "workflow_start_interval_guaranteed": False,
        "schedule_uncertainty_note": "GitHub Actions peut retarder ou regrouper les déclenchements; cette projection n'est pas une garantie.",
        "scenarios": scenarios,
    }
