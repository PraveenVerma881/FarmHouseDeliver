from __future__ import annotations

import math
from typing import Any

from app.schemas import PayQuoteRequest


def predict_acceptance_minutes(
    request: PayQuoteRequest,
    compensation_cents: int,
) -> float:
    """Predict acceptance response time using locally calibrated log-ART coefficients."""
    calibration = request.calibration
    pay_dollars = compensation_cents / 100.0
    pay_squared = pay_dollars * pay_dollars
    density = request.density_index
    unattended = request.unattended_share
    evening = 1.0 if request.is_evening else 0.0

    log_response_minutes = (
        calibration.intercept
        + calibration.pay_linear * pay_dollars
        + calibration.pay_quadratic * pay_squared
        + calibration.density_effect * density
        + calibration.unattended_effect * unattended
        + calibration.evening_effect * evening
        + calibration.pay_density * pay_dollars * density
        + calibration.pay_squared_density * pay_squared * density
        + calibration.pay_unattended * pay_dollars * unattended
        + calibration.pay_squared_unattended * pay_squared * unattended
        + calibration.pay_evening * pay_dollars * evening
        + calibration.pay_squared_evening * pay_squared * evening
    )
    return math.exp(min(20.0, max(-20.0, log_response_minutes)))


def calculate_pay_quote(request: PayQuoteRequest) -> dict[str, Any]:
    candidates = list(
        range(
            request.min_compensation_cents,
            request.max_compensation_cents + 1,
            request.step_cents,
        )
    )
    if candidates[-1] != request.max_compensation_cents:
        candidates.append(request.max_compensation_cents)

    chosen_cents = request.max_compensation_cents
    target_met = False
    predicted_minutes = predict_acceptance_minutes(request, chosen_cents)
    for compensation_cents in candidates:
        prediction = predict_acceptance_minutes(request, compensation_cents)
        if prediction <= request.target_acceptance_minutes:
            chosen_cents = compensation_cents
            predicted_minutes = prediction
            target_met = True
            break

    return {
        "task_id": str(request.task_id),
        "model_version": request.calibration.model_version,
        "training_region": request.calibration.training_region,
        "trained_at": request.calibration.trained_at.isoformat(),
        "offered_compensation_cents": chosen_cents,
        "offered_compensation_currency": round(chosen_cents / 100.0, 2),
        "predicted_acceptance_minutes": round(predicted_minutes, 2),
        "target_acceptance_minutes": request.target_acceptance_minutes,
        "target_met": target_met,
        "search": "lowest tested pay meeting target; otherwise configured maximum",
        "warning": "Prediction is only as transferable as the locally calibrated model.",
    }
