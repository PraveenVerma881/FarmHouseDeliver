from __future__ import annotations

from typing import Any
from uuid import UUID

from ortools.sat.python import cp_model

from app.schemas import DriverAllocationRequest


def solve_fair_allocation(
    request: DriverAllocationRequest,
    pay_quotes: dict[UUID, dict[str, Any]],
    prior_accepted_tasks_7d: dict[UUID, int] | None = None,
) -> dict[str, Any]:
    """Maximize covered tasks, then fit/response/cost while regularizing recent load."""
    task_by_id = {task.task_id: task for task in request.tasks}
    recent_load = prior_accepted_tasks_7d or {}
    eligible_edges: list[tuple[Any, dict[str, Any], int]] = []

    for candidate in request.candidates:
        if not candidate.available:
            continue
        quote = pay_quotes[candidate.pay_quote_id]
        if quote["task_id"] != candidate.task_id:
            raise ValueError("Pay quote task does not match allocation candidate task")
        if not quote["target_met"] and not task_by_id[candidate.task_id].allow_target_miss:
            continue
        compensation = int(quote["offered_compensation_cents"])
        if compensation > task_by_id[candidate.task_id].compensation_budget_cents:
            continue
        predicted_art = int(round(float(quote["predicted_acceptance_minutes"])))
        score = (
            candidate.fit_score * request.weights.fit
            - compensation * request.weights.compensation
            - predicted_art * request.weights.response_time
        )
        eligible_edges.append((candidate, quote, score))

    if not eligible_edges:
        return {
            "status": "optimal",
            "solver": "OR-Tools CP-SAT",
            "assigned_count": 0,
            "unassigned_task_ids": [str(task.task_id) for task in request.tasks],
            "assignments": [],
            "driver_loads": [
                {
                    "driver_id": str(driver.driver_id),
                    "prior_accepted_tasks_7d": recent_load.get(driver.driver_id, 0),
                    "assigned_this_round": 0,
                }
                for driver in request.drivers
            ],
            "objective_note": "No available candidate fits the task budgets.",
        }

    model = cp_model.CpModel()
    edge_variables: dict[tuple[UUID, UUID], cp_model.IntVar] = {}
    edge_data: dict[tuple[UUID, UUID], tuple[Any, dict[str, Any], int]] = {}
    for candidate, quote, score in eligible_edges:
        key = (candidate.task_id, candidate.driver_id)
        edge_variables[key] = model.NewBoolVar(f"assign_{candidate.task_id}_{candidate.driver_id}")
        edge_data[key] = (candidate, quote, score)

    for task in request.tasks:
        task_edges = [
            variable
            for (task_id, _), variable in edge_variables.items()
            if task_id == task.task_id
        ]
        if task_edges:
            model.Add(sum(task_edges) <= 1)

    slot_variables: dict[UUID, list[cp_model.IntVar]] = {}
    fairness_penalty_terms: list[Any] = []
    for driver in request.drivers:
        driver_edges = [
            variable
            for (_, driver_id), variable in edge_variables.items()
            if driver_id == driver.driver_id
        ]
        capacity = min(driver.max_tasks_this_round, len(driver_edges))
        slots = [model.NewBoolVar(f"load_{driver.driver_id}_{slot}") for slot in range(capacity)]
        slot_variables[driver.driver_id] = slots
        if slots:
            model.Add(sum(driver_edges) == sum(slots))
            for slot in range(1, len(slots)):
                model.Add(slots[slot] <= slots[slot - 1])
            prior = recent_load.get(driver.driver_id, 0)
            for slot, variable in enumerate(slots, start=1):
                marginal_square_load = 2 * prior + 2 * slot - 1
                fairness_penalty_terms.append(
                    marginal_square_load * request.weights.fairness * variable
                )

    maximum_edge_abs_score = max(abs(score) for _, _, score in eligible_edges)
    maximum_fairness_penalty = sum(
        request.weights.fairness
        * sum(
            2 * recent_load.get(driver_id, 0) + 2 * slot - 1
            for slot in range(1, len(slots) + 1)
        )
        for driver_id, slots in slot_variables.items()
    )
    coverage_reward = (
        2 * len(request.tasks) * maximum_edge_abs_score
        + maximum_fairness_penalty
        + 1
    )

    assignment_terms = [
        (coverage_reward + score) * edge_variables[key]
        for key, (_, _, score) in edge_data.items()
    ]
    model.Maximize(sum(assignment_terms) - sum(fairness_penalty_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = request.solver_time_limit_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 17
    status_code = solver.Solve(model)
    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "status": "failed",
            "solver": "OR-Tools CP-SAT",
            "assigned_count": 0,
            "unassigned_task_ids": [str(task.task_id) for task in request.tasks],
            "assignments": [],
            "driver_loads": [],
            "objective_note": "Solver stopped before finding a feasible allocation.",
        }

    assignments: list[dict[str, Any]] = []
    assigned_task_ids: set[UUID] = set()
    assigned_by_driver: dict[UUID, int] = {driver.driver_id: 0 for driver in request.drivers}
    for key, variable in edge_variables.items():
        if solver.Value(variable) != 1:
            continue
        candidate, quote, score = edge_data[key]
        assigned_task_ids.add(candidate.task_id)
        assigned_by_driver[candidate.driver_id] += 1
        assignments.append(
            {
                "task_id": str(candidate.task_id),
                "driver_id": str(candidate.driver_id),
                "pay_quote_id": str(candidate.pay_quote_id),
                "compensation_cents": int(quote["offered_compensation_cents"]),
                "predicted_acceptance_minutes": round(float(quote["predicted_acceptance_minutes"]), 2),
                "pay_target_met": bool(quote["target_met"]),
                "fit_score": candidate.fit_score,
                "edge_score": score,
            }
        )

    loads = [
        {
            "driver_id": str(driver.driver_id),
            "prior_accepted_tasks_7d": recent_load.get(driver.driver_id, 0),
            "assigned_this_round": assigned_by_driver[driver.driver_id],
            "combined_load": recent_load.get(driver.driver_id, 0) + assigned_by_driver[driver.driver_id],
        }
        for driver in request.drivers
    ]
    combined_loads = [load["combined_load"] for load in loads]
    return {
        "status": "optimal" if status_code == cp_model.OPTIMAL else "feasible_time_limited",
        "solver": "OR-Tools CP-SAT",
        "assigned_count": len(assignments),
        "unassigned_task_ids": [
            str(task.task_id) for task in request.tasks if task.task_id not in assigned_task_ids
        ],
        "assignments": assignments,
        "driver_loads": loads,
        "load_range_after_allocation": max(combined_loads) - min(combined_loads),
        "objective_note": (
            "Task coverage is lexicographically prioritized; among maximum-coverage solutions, "
            "the model trades fit, predicted response time, compensation cost, and recent workload balance."
        ),
        "best_objective_value": solver.ObjectiveValue(),
    }
