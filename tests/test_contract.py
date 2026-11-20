from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.main import app
from app.schemas import (
    DriverAssignmentResponse,
    DriverAllocationRequest,
    DriverAssignmentResponse,
    DriverEventCreate,
    OrderCreate,
    ParcelSearch,
    PayQuoteRequest,
    VendorCommitmentResponse,
)


def valid_order() -> dict[str, object]:
    start = datetime.now(timezone.utc)
    return {
        "customer_id": str(uuid4()),
        "delivery_latitude": 49.2,
        "delivery_longitude": 16.6,
        "delivery_window_start": start.isoformat(),
        "delivery_window_end": (start + timedelta(hours=2)).isoformat(),
        "lines": [
            {"product_id": str(uuid4()), "quantity": "2.5", "unit": "kg"}
        ],
    }


def test_order_accepts_valid_timezone_window() -> None:
    order = OrderCreate.model_validate(valid_order())
    assert order.lines[0].quantity == 2.5


def test_order_rejects_out_of_range_coordinates() -> None:
    payload = valid_order()
    payload["delivery_latitude"] = 91
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(payload)


def test_order_rejects_naive_delivery_window() -> None:
    payload = valid_order()
    payload["delivery_window_start"] = "2026-11-09T12:00:00"
    with pytest.raises(ValidationError):
        OrderCreate.model_validate(payload)


def test_driver_event_requires_both_coordinates() -> None:
    with pytest.raises(ValidationError):
        DriverEventCreate.model_validate(
            {
                "event_type": "arrived",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "latitude": 49.2,
            }
        )


def test_parcel_search_requires_polygon_geojson() -> None:
    with pytest.raises(ValidationError):
        ParcelSearch.model_validate({"geometry": {"type": "Point", "coordinates": [16.6, 49.2]}})


def test_vendor_confirmation_requires_ready_time() -> None:
    with pytest.raises(ValidationError):
        VendorCommitmentResponse.model_validate(
            {"status": "confirmed", "confirmed_quantity": "3.0"}
        )


def test_openapi_exposes_expected_api_routes() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/products" in paths
    assert "/v1/farms/search" in paths
    assert "/v1/parcels/search" in paths
    assert "/v1/orders" in paths
    assert "/v1/operations/vendor-commitments" in paths
    assert "/v1/vendor/commitments/{commitment_id}/response" in paths
    assert "/v1/operations/planning-jobs" in paths
    assert "/v1/driver/stops/{stop_id}/events" in paths
    assert "/v1/operations/pay-quotes" in paths
    assert "/v1/operations/driver-allocations" in paths
    assert "/v1/operations/driver-allocations/{allocation_run_id}" in paths
    assert "/v1/driver/assignments/{assignment_id}/response" in paths
    assert "/v1/operations/pay-quotes" in paths
    assert "/v1/operations/driver-allocations" in paths
    assert "/v1/operations/driver-allocations/{allocation_run_id}" in paths
    assert "/v1/driver/assignments/{assignment_id}/response" in paths


def test_pay_quote_request_requires_explicit_calibration_provenance() -> None:
    with pytest.raises(ValidationError):
        PayQuoteRequest.model_validate(
            {
                "task_id": str(uuid4()),
                "min_compensation_cents": 800,
                "max_compensation_cents": 2500,
                "target_acceptance_minutes": 20,
                "density_index": 0.4,
                "unattended_share": 0.6,
                "is_evening": True,
                "calibration": {},
            }
        )


def test_allocation_rejects_candidate_for_unknown_task_or_driver() -> None:
    with pytest.raises(ValidationError):
        DriverAllocationRequest.model_validate(
            {
                "tasks": [{"task_id": str(uuid4()), "compensation_budget_cents": 2000}],
                "drivers": [{"driver_id": str(uuid4())}],
                "candidates": [
                    {
                        "task_id": str(uuid4()),
                        "driver_id": str(uuid4()),
                        "pay_quote_id": str(uuid4()),
                        "fit_score": 80,
                    }
                ],
            }
        )


def test_driver_assignment_response_allows_only_accept_or_decline() -> None:
    accepted = DriverAssignmentResponse.model_validate({"status": "accepted"})
    assert accepted.status.value == "accepted"
    with pytest.raises(ValidationError):
        DriverAssignmentResponse.model_validate({"status": "maybe"})


def test_driver_assignment_response_limits_status_to_accept_or_decline() -> None:
    response = DriverAssignmentResponse.model_validate({"status": "accepted"})
    assert response.status.value == "accepted"
    with pytest.raises(ValidationError):
        DriverAssignmentResponse.model_validate({"status": "maybe"})