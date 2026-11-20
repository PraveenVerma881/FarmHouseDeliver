from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.pricing import calculate_pay_quote
from app.schemas import AcceptanceResponseCalibration, PayQuoteRequest


def calibration() -> AcceptanceResponseCalibration:
    return AcceptanceResponseCalibration(
        model_version="local-demo-v1",
        training_region="demo-region",
        trained_at=datetime.now(timezone.utc),
        intercept=4.094344562,
        pay_linear=-0.08,
        pay_quadratic=0.001,
        density_effect=0,
        unattended_effect=0,
        evening_effect=0,
        pay_density=0,
        pay_squared_density=0,
        pay_unattended=0,
        pay_squared_unattended=0,
        pay_evening=0,
        pay_squared_evening=0,
    )


def test_pay_quote_uses_lowest_tested_pay_meeting_target() -> None:
    request = PayQuoteRequest(
        task_id=uuid4(),
        min_compensation_cents=500,
        max_compensation_cents=3000,
        step_cents=100,
        target_acceptance_minutes=20,
        density_index=0.5,
        unattended_share=0.5,
        is_evening=False,
        calibration=calibration(),
    )

    result = calculate_pay_quote(request)

    assert result["target_met"] is True
    assert result["offered_compensation_cents"] == 1800
    assert result["predicted_acceptance_minutes"] <= 20


def test_pay_quote_returns_budget_max_when_target_cannot_be_met() -> None:
    request = PayQuoteRequest(
        task_id=uuid4(),
        min_compensation_cents=0,
        max_compensation_cents=100,
        step_cents=25,
        target_acceptance_minutes=1,
        density_index=0,
        unattended_share=0,
        is_evening=True,
        calibration=calibration(),
    )

    result = calculate_pay_quote(request)

    assert result["target_met"] is False
    assert result["offered_compensation_cents"] == 100


def test_pay_quote_rejects_unreasonable_search_size() -> None:
    with pytest.raises(ValidationError):
        PayQuoteRequest(
            task_id=uuid4(),
            min_compensation_cents=0,
            max_compensation_cents=100_000,
            step_cents=1,
            target_acceptance_minutes=20,
            density_index=0,
            unattended_share=0,
            is_evening=False,
            calibration=calibration(),
        )
