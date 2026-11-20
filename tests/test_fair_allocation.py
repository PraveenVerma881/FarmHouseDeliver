from __future__ import annotations

from uuid import uuid4

from app.allocation import solve_fair_allocation
from app.schemas import (
    AllocationCandidateInput,
    AllocationDriverInput,
    AllocationTaskInput,
    AllocationWeights,
    DriverAllocationRequest,
)


def test_allocator_maximizes_coverage_then_balances_recent_load() -> None:
    task_ids = [uuid4(), uuid4()]
    driver_ids = [uuid4(), uuid4()]
    quote_ids = [uuid4(), uuid4()]
    request = DriverAllocationRequest(
        tasks=[
            AllocationTaskInput(task_id=task_ids[index], compensation_budget_cents=2000)
            for index in range(2)
        ],
        drivers=[
            AllocationDriverInput(driver_id=driver_ids[0], max_tasks_this_round=2),
            AllocationDriverInput(driver_id=driver_ids[1], max_tasks_this_round=2),
        ],
        candidates=[
            AllocationCandidateInput(
                task_id=task_ids[task_index],
                driver_id=driver_ids[driver_index],
                pay_quote_id=quote_ids[task_index],
                fit_score=80,
            )
            for task_index in range(2)
            for driver_index in range(2)
        ],
        weights=AllocationWeights(fit=0, compensation=0, response_time=0, fairness=1000),
        solver_time_limit_seconds=2,
    )
    quotes = {
        quote_ids[index]: {
            "task_id": task_ids[index],
            "offered_compensation_cents": 1200,
            "predicted_acceptance_minutes": 12.0,
            "target_met": True,
        }
        for index in range(2)
    }

    result = solve_fair_allocation(request, quotes, {driver_ids[0]: 8, driver_ids[1]: 0})

    assert result["status"] == "optimal"
    assert result["assigned_count"] == 2
    assert len(result["unassigned_task_ids"]) == 0
    assert sorted(load["assigned_this_round"] for load in result["driver_loads"]) == [0, 2]
    assert result["driver_loads"][1]["combined_load"] == 2


def test_allocator_never_exceeds_driver_capacity() -> None:
    task_ids = [uuid4(), uuid4()]
    driver_id = uuid4()
    quote_ids = [uuid4(), uuid4()]
    request = DriverAllocationRequest(
        tasks=[AllocationTaskInput(task_id=task_id, compensation_budget_cents=2000) for task_id in task_ids],
        drivers=[AllocationDriverInput(driver_id=driver_id, max_tasks_this_round=1)],
        candidates=[
            AllocationCandidateInput(task_id=task_id, driver_id=driver_id, pay_quote_id=quote_id, fit_score=100)
            for task_id, quote_id in zip(task_ids, quote_ids, strict=True)
        ],
        solver_time_limit_seconds=2,
    )
    quotes = {
        quote_id: {
            "task_id": task_id,
            "offered_compensation_cents": 1000,
            "predicted_acceptance_minutes": 10.0,
            "target_met": True,
        }
        for task_id, quote_id in zip(task_ids, quote_ids, strict=True)
    }

    result = solve_fair_allocation(request, quotes)

    assert result["assigned_count"] == 1
    assert len(result["unassigned_task_ids"]) == 1


def test_unmet_quote_target_is_not_allocated_without_opt_in() -> None:
    task_id = uuid4()
    driver_id = uuid4()
    quote_id = uuid4()
    request = DriverAllocationRequest(
        tasks=[AllocationTaskInput(task_id=task_id, compensation_budget_cents=2000)],
        drivers=[AllocationDriverInput(driver_id=driver_id)],
        candidates=[
            AllocationCandidateInput(
                task_id=task_id,
                driver_id=driver_id,
                pay_quote_id=quote_id,
                fit_score=100,
            )
        ],
    )
    quote = {
        quote_id: {
            "task_id": task_id,
            "offered_compensation_cents": 1500,
            "predicted_acceptance_minutes": 60.0,
            "target_met": False,
        }
    }

    result = solve_fair_allocation(request, quote)
    assert result["assigned_count"] == 0

    allowed_request = request.model_copy(
        update={"tasks": [request.tasks[0].model_copy(update={"allow_target_miss": True})]}
    )
    allowed_result = solve_fair_allocation(allowed_request, quote)
    assert allowed_result["assigned_count"] == 1
