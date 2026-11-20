from __future__ import annotations

from typing import Any
from uuid import UUID

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from app.schemas import RoutingProblem, RoutingStopInput, RoutingStopType


def solve_routing_problem(
    problem: RoutingProblem,
    time_limit_seconds: int,
) -> dict[str, Any]:
    """Solve a pickup-and-delivery CVRPTW problem using supplied road matrices."""
    location_count = len(problem.locations)
    vehicle_count = len(problem.vehicles)
    manager = pywrapcp.RoutingIndexManager(
        location_count,
        vehicle_count,
        [vehicle.start_location_index for vehicle in problem.vehicles],
        [vehicle.end_location_index for vehicle in problem.vehicles],
    )
    routing = pywrapcp.RoutingModel(manager)
    stop_by_node = {stop.location_index: stop for stop in problem.stops}
    demand_grams = [0] * location_count
    for stop in problem.stops:
        quantity = int(stop.quantity_kg * 1000)
        demand_grams[stop.location_index] = quantity if stop.stop_type == RoutingStopType.PICKUP else -quantity

    def time_callback(from_index: int, to_index: int) -> int:
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        service_minutes = stop_by_node.get(from_node)
        service = service_minutes.service_minutes if service_minutes else 0
        return problem.travel_time_matrix_minutes[from_node][to_node] + service

    time_callback_index = routing.RegisterTransitCallback(time_callback)
    routing.AddDimension(
        time_callback_index,
        2880,
        2880,
        False,
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")

    for vehicle_index, vehicle in enumerate(problem.vehicles):
        start_index = routing.Start(vehicle_index)
        end_index = routing.End(vehicle_index)
        time_dimension.CumulVar(start_index).SetRange(
            vehicle.shift_start_minute,
            vehicle.shift_start_minute,
        )
        time_dimension.CumulVar(end_index).SetRange(
            vehicle.shift_start_minute,
            vehicle.shift_end_minute,
        )
        time_dimension.SetSlackCostCoefficientForVehicle(
            vehicle.cost_per_minute_cents,
            vehicle_index,
        )

        def cost_callback(from_index: int, to_index: int, vehicle_index: int = vehicle_index) -> int:
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)
            vehicle_cost = problem.vehicles[vehicle_index]
            distance_cost = (
                problem.distance_matrix_metres[from_node][to_node]
                * vehicle_cost.cost_per_km_cents
                // 1000
            )
            stop = stop_by_node.get(from_node)
            service_minutes = stop.service_minutes if stop else 0
            paid_minutes = problem.travel_time_matrix_minutes[from_node][to_node] + service_minutes
            return distance_cost + paid_minutes * vehicle_cost.cost_per_minute_cents

        cost_callback_index = routing.RegisterTransitCallback(cost_callback)
        routing.SetArcCostEvaluatorOfVehicle(cost_callback_index, vehicle_index)
        routing.SetFixedCostOfVehicle(vehicle.fixed_cost_cents, vehicle_index)

    def demand_callback(from_index: int) -> int:
        node = manager.IndexToNode(from_index)
        return demand_grams[node]

    demand_callback_index = routing.RegisterUnaryTransitCallback(demand_callback)
    capacities = [int(vehicle.capacity_kg * 1000) for vehicle in problem.vehicles]
    routing.AddDimensionWithVehicleCapacity(
        demand_callback_index,
        0,
        capacities,
        True,
        "Capacity",
    )

    for stop in problem.stops:
        index = manager.NodeToIndex(stop.location_index)
        time_dimension.CumulVar(index).SetRange(
            stop.window_start_minute,
            stop.window_end_minute,
        )

    stop_pairs: dict[UUID, dict[RoutingStopType, RoutingStopInput]] = {}
    for stop in problem.stops:
        stop_pairs.setdefault(stop.pair_id, {})[stop.stop_type] = stop
    for pair in stop_pairs.values():
        pickup = pair[RoutingStopType.PICKUP]
        delivery = pair[RoutingStopType.DELIVERY]
        pickup_index = manager.NodeToIndex(pickup.location_index)
        delivery_index = manager.NodeToIndex(delivery.location_index)
        routing.AddPickupAndDelivery(pickup_index, delivery_index)
        solver = routing.solver()
        solver.Add(routing.VehicleVar(pickup_index) == routing.VehicleVar(delivery_index))
        solver.Add(
            time_dimension.CumulVar(pickup_index)
            <= time_dimension.CumulVar(delivery_index)
        )
        if delivery.max_transit_minutes is not None:
            solver.Add(
                time_dimension.CumulVar(delivery_index)
                - time_dimension.CumulVar(pickup_index)
                <= delivery.max_transit_minutes
            )

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(time_limit_seconds)
    search_parameters.log_search = False
    assignment = routing.SolveWithParameters(search_parameters)
    if assignment is None:
        return {
            "status": "infeasible",
            "message": "No feasible route satisfies capacity, pairing, shift, time-window, and freshness constraints.",
            "routes": [],
            "objective_cost_cents": None,
        }

    routes: list[dict[str, Any]] = []
    total_distance_metres = 0
    total_drive_minutes = 0
    total_service_minutes = 0
    total_fixed_cost_cents = 0

    for vehicle_index, vehicle in enumerate(problem.vehicles):
        index = routing.Start(vehicle_index)
        sequence = 0
        route_stops: list[dict[str, Any]] = []
        route_distance = 0
        route_drive_minutes = 0
        route_service_minutes = 0
        while not routing.IsEnd(index):
            from_node = manager.IndexToNode(index)
            next_index = assignment.Value(routing.NextVar(index))
            to_node = manager.IndexToNode(next_index)
            route_distance += problem.distance_matrix_metres[from_node][to_node]
            route_drive_minutes += problem.travel_time_matrix_minutes[from_node][to_node]
            stop = stop_by_node.get(from_node)
            if stop is not None:
                arrival = assignment.Value(time_dimension.CumulVar(index))
                route_stops.append(
                    {
                        "stop_id": str(stop.stop_id),
                        "order_id": str(stop.order_id) if stop.order_id else None,
                        "stop_type": stop.stop_type.value,
                        "sequence_number": sequence,
                        "location_index": stop.location_index,
                        "arrival_minute": arrival,
                        "service_minutes": stop.service_minutes,
                    }
                )
                sequence += 1
                route_service_minutes += stop.service_minutes
            index = next_index
        used_vehicle = bool(route_stops)
        route_elapsed_minutes = (
            assignment.Value(time_dimension.CumulVar(routing.End(vehicle_index)))
            - vehicle.shift_start_minute
        )
        route_waiting_minutes = max(
            0,
            route_elapsed_minutes - route_drive_minutes - route_service_minutes,
        )
        fixed_cost = vehicle.fixed_cost_cents if used_vehicle else 0
        route_total_cost = fixed_cost
        route_total_cost += route_distance * vehicle.cost_per_km_cents // 1000
        route_total_cost += (
            route_drive_minutes + route_service_minutes + route_waiting_minutes
        ) * vehicle.cost_per_minute_cents
        if used_vehicle:
            total_fixed_cost_cents += fixed_cost
        routes.append(
            {
                "vehicle_id": str(vehicle.vehicle_id),
                "stops": route_stops,
                "distance_metres": route_distance,
                "drive_minutes": route_drive_minutes,
                "service_minutes": route_service_minutes,
                "waiting_minutes": route_waiting_minutes,
                "estimated_cost_cents": route_total_cost,
                "shift_start_minute": vehicle.shift_start_minute,
                "shift_end_minute": assignment.Value(time_dimension.CumulVar(routing.End(vehicle_index))),
            }
        )
        total_distance_metres += route_distance
        total_drive_minutes += route_drive_minutes
        total_service_minutes += route_service_minutes

    return {
        "status": "succeeded",
        "solver": "OR-Tools RoutingModel / guided local search",
        "planning_start_at": problem.planning_start_at.isoformat(),
        "travel_matrix_provider": problem.travel_matrix_provider,
        "travel_matrix_generated_at": problem.travel_matrix_generated_at.isoformat(),
        "objective_cost_cents": assignment.ObjectiveValue(),
        "cost_breakdown_cents": {
            "fixed_vehicle_cost": total_fixed_cost_cents,
            "distance_and_driver_time": assignment.ObjectiveValue() - total_fixed_cost_cents,
        },
        "total_distance_metres": total_distance_metres,
        "total_drive_minutes": total_drive_minutes,
        "total_service_minutes": total_service_minutes,
        "routes": routes,
    }