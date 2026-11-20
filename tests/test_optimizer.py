from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.optimizer import solve_routing_problem
from app.schemas import PlanningJobCreate, RoutingProblem


def routing_problem() -> RoutingProblem:
    pair_id = uuid4()
    return RoutingProblem.model_validate(
        {
            "planning_start_at": datetime.now(timezone.utc).isoformat(),
            "travel_matrix_provider": "test-road-matrix",
            "travel_matrix_generated_at": datetime.now(timezone.utc).isoformat(),
            "locations": [
                {"latitude": 49.2, "longitude": 16.6},
                {"latitude": 49.21, "longitude": 16.61},
                {"latitude": 49.22, "longitude": 16.62},
                {"latitude": 49.2, "longitude": 16.6},
            ],
            "stops": [
                {
                    "stop_id": str(uuid4()),
                    "location_index": 1,
                    "stop_type": "pickup",
                    "pair_id": str(pair_id),
                    "quantity_kg": "4.0",
                    "service_minutes": 5,
                    "window_start_minute": 30,
                    "window_end_minute": 60,
                },
                {
                    "stop_id": str(uuid4()),
                    "location_index": 2,
                    "stop_type": "delivery",
                    "pair_id": str(pair_id),
                    "quantity_kg": "4.0",
                    "service_minutes": 5,
                    "window_start_minute": 0,
                    "window_end_minute": 120,
                    "max_transit_minutes": 30,
                },
            ],
            "vehicles": [
                {
                    "vehicle_id": str(uuid4()),
                    "start_location_index": 0,
                    "end_location_index": 3,
                    "capacity_kg": "10.0",
                    "shift_start_minute": 0,
                    "shift_end_minute": 120,
                    "fixed_cost_cents": 500,
                    "cost_per_km_cents": 100,
                    "cost_per_minute_cents": 20,
                }
            ],
            "travel_time_matrix_minutes": [
                [0, 5, 20, 25],
                [5, 0, 10, 20],
                [20, 10, 0, 5],
                [25, 20, 5, 0],
            ],
            "distance_matrix_metres": [
                [0, 1000, 4000, 5000],
                [1000, 0, 2000, 4000],
                [4000, 2000, 0, 1000],
                [5000, 4000, 1000, 0],
            ],
        }
    )


def test_solver_returns_capacity_and_freshness_feasible_route() -> None:
    problem = routing_problem()
    result = solve_routing_problem(problem, time_limit_seconds=1)

    assert result["status"] == "succeeded"
    assert result["total_distance_metres"] > 0
    route = next(route for route in result["routes"] if route["stops"])
    assert [stop["stop_type"] for stop in route["stops"]] == ["pickup", "delivery"]
    assert route["waiting_minutes"] > 0
    assert route["estimated_cost_cents"] > 500


def test_solver_schema_rejects_mismatched_matrices() -> None:
    payload = routing_problem().model_dump(mode="json")
    payload["distance_matrix_metres"] = [[0, 1], [1, 0]]
    with pytest.raises(ValidationError):
        RoutingProblem.model_validate(payload)


def test_solver_schema_requires_freshness_limit_on_delivery() -> None:
    payload = routing_problem().model_dump(mode="json")
    payload["stops"][1]["max_transit_minutes"] = None
    with pytest.raises(ValidationError):
        RoutingProblem.model_validate(payload)


def test_solver_reports_capacity_violation_as_infeasible() -> None:
    problem = routing_problem().model_copy(deep=True)
    problem.vehicles[0].capacity_kg = 3

    result = solve_routing_problem(problem, time_limit_seconds=1)

    assert result["status"] == "infeasible"
    assert result["routes"] == []


def test_solver_reports_freshness_violation_as_infeasible() -> None:
    problem = routing_problem().model_copy(deep=True)
    problem.travel_time_matrix_minutes[1][2] = 45
    problem.stops[1].max_transit_minutes = 30

    result = solve_routing_problem(problem, time_limit_seconds=1)

    assert result["status"] == "infeasible"


def test_planning_job_requires_delivery_task_for_each_order() -> None:
    order_id = uuid4()
    payload = {
        "delivery_date": date.today().isoformat(),
        "zone_id": str(uuid4()),
        "order_ids": [str(order_id)],
        "problem": routing_problem().model_dump(mode="json"),
    }
    payload["problem"]["stops"][1]["order_id"] = str(order_id)

    job = PlanningJobCreate.model_validate(payload)
    assert job.problem.stops[1].order_id == order_id

    payload["order_ids"] = [str(uuid4())]
    with pytest.raises(ValidationError):
        PlanningJobCreate.model_validate(payload)
