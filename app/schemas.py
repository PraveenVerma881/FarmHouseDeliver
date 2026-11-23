from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrderLineInput(BaseModel):
    product_id: UUID
    quantity: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
    unit: Annotated[str, Field(min_length=1, max_length=24)]
    allow_substitution: bool = False


class OrderCreate(BaseModel):
    customer_id: UUID
    delivery_latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    delivery_longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    delivery_address: Annotated[str, Field(max_length=500)] | None = None
    delivery_window_start: datetime | None = None
    delivery_window_end: datetime | None = None
    lines: Annotated[list[OrderLineInput], Field(min_length=1, max_length=40)]

    @model_validator(mode="after")
    def validate_delivery_window(self) -> OrderCreate:
        start = self.delivery_window_start
        end = self.delivery_window_end
        if (self.delivery_latitude is None) != (self.delivery_longitude is None):
            raise ValueError("Delivery latitude and longitude must be supplied together")
        if self.delivery_latitude is None and not (self.delivery_address and self.delivery_address.strip()):
            raise ValueError("Provide a delivery address or a location pin")
        if (start is None) != (end is None):
            raise ValueError("Provide both delivery-window times or neither")
        if start is not None and end is not None:
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("Delivery-window timestamps must include a timezone")
            if end <= start:
                raise ValueError("Delivery-window end must be after its start")
        return self


class FarmSearchResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    farm_id: UUID
    farm_name: str
    offer_id: UUID
    product_id: UUID
    available_quantity: Decimal
    unit: str
    harvest_date: date
    quality_grade: str | None
    distance_metres: float


class LocationInput(BaseModel):
    latitude: Annotated[float, Field(ge=-90, le=90)]
    longitude: Annotated[float, Field(ge=-180, le=180)]


class RoutingStopType(StrEnum):
    PICKUP = "pickup"
    DELIVERY = "delivery"


class RoutingStopInput(BaseModel):
    stop_id: UUID
    order_id: UUID | None = None
    location_index: int = Field(ge=0)
    stop_type: RoutingStopType
    pair_id: UUID
    quantity_kg: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
    service_minutes: Annotated[int, Field(ge=0, le=240)] = 5
    window_start_minute: Annotated[int, Field(ge=0, le=2880)]
    window_end_minute: Annotated[int, Field(ge=0, le=2880)]
    max_transit_minutes: Annotated[int, Field(ge=1, le=1440)] | None = None

    @model_validator(mode="after")
    def validate_window_and_freshness(self) -> RoutingStopInput:
        if self.window_end_minute < self.window_start_minute:
            raise ValueError("Stop window must end after it starts")
        if self.stop_type == RoutingStopType.PICKUP and self.max_transit_minutes is not None:
            raise ValueError("max_transit_minutes belongs on the delivery stop")
        if self.stop_type == RoutingStopType.DELIVERY and self.max_transit_minutes is None:
            raise ValueError("Delivery stops require a max_transit_minutes freshness limit")
        return self


class VehicleInput(BaseModel):
    vehicle_id: UUID
    start_location_index: Annotated[int, Field(ge=0)]
    end_location_index: Annotated[int, Field(ge=0)]
    capacity_kg: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
    shift_start_minute: Annotated[int, Field(ge=0, le=2880)]
    shift_end_minute: Annotated[int, Field(ge=1, le=2880)]
    fixed_cost_cents: Annotated[int, Field(ge=0, le=10_000_000)] = 0
    cost_per_km_cents: Annotated[int, Field(ge=0, le=100_000)] = 0
    cost_per_minute_cents: Annotated[int, Field(ge=0, le=100_000)] = 0

    @model_validator(mode="after")
    def validate_shift(self) -> VehicleInput:
        if self.shift_end_minute <= self.shift_start_minute:
            raise ValueError("Vehicle shift end must be after its start")
        return self


class RoutingProblem(BaseModel):
    planning_start_at: datetime
    travel_matrix_provider: Annotated[str, Field(min_length=1, max_length=80)]
    travel_matrix_generated_at: datetime
    locations: Annotated[list[LocationInput], Field(min_length=2, max_length=300)]
    stops: Annotated[list[RoutingStopInput], Field(min_length=2, max_length=250)]
    vehicles: Annotated[list[VehicleInput], Field(min_length=1, max_length=50)]
    travel_time_matrix_minutes: list[list[Annotated[int, Field(ge=0, le=2880)]]]
    distance_matrix_metres: list[list[Annotated[int, Field(ge=0, le=2_000_000)]]]

    @model_validator(mode="after")
    def validate_routing_problem(self) -> RoutingProblem:
        if self.planning_start_at.tzinfo is None or self.travel_matrix_generated_at.tzinfo is None:
            raise ValueError("Planning and matrix timestamps must include a timezone")
        location_count = len(self.locations)
        for matrix_name, matrix in (
            ("travel_time_matrix_minutes", self.travel_time_matrix_minutes),
            ("distance_matrix_metres", self.distance_matrix_metres),
        ):
            if len(matrix) != location_count or any(len(row) != location_count for row in matrix):
                raise ValueError(f"{matrix_name} must be a square matrix matching locations")
            if any(matrix[index][index] != 0 for index in range(location_count)):
                raise ValueError(f"{matrix_name} diagonal must be zero")

        stop_nodes = [stop.location_index for stop in self.stops]
        if any(node >= location_count for node in stop_nodes):
            raise ValueError("Every stop location_index must refer to a location")
        if len(set(stop_nodes)) != len(stop_nodes):
            raise ValueError("Each routing stop must have a unique location_index")

        depot_nodes: set[int] = set()
        for vehicle in self.vehicles:
            if vehicle.start_location_index >= location_count or vehicle.end_location_index >= location_count:
                raise ValueError("Vehicle depot index must refer to a location")
            depot_nodes.update((vehicle.start_location_index, vehicle.end_location_index))
        if depot_nodes.intersection(stop_nodes):
            raise ValueError("Vehicle start/end locations must be separate from stop locations")

        pairs: dict[UUID, list[RoutingStopInput]] = {}
        for stop in self.stops:
            pairs.setdefault(stop.pair_id, []).append(stop)
        for pair in pairs.values():
            if len(pair) != 2 or {stop.stop_type for stop in pair} != {
                RoutingStopType.PICKUP,
                RoutingStopType.DELIVERY,
            }:
                raise ValueError("Each pair_id must have exactly one pickup and one delivery")
            pickup = next(stop for stop in pair if stop.stop_type == RoutingStopType.PICKUP)
            delivery = next(stop for stop in pair if stop.stop_type == RoutingStopType.DELIVERY)
            if pickup.quantity_kg != delivery.quantity_kg:
                raise ValueError("Pickup and delivery quantities for a pair must match")

        covered_nodes = set(stop_nodes) | depot_nodes
        if covered_nodes != set(range(location_count)):
            raise ValueError("Every matrix location must be a stop or a vehicle depot")
        return self


class PlanningJobCreate(BaseModel):
    delivery_date: date
    zone_id: UUID
    order_ids: Annotated[list[UUID], Field(min_length=1, max_length=500)]
    solver_time_limit_seconds: Annotated[int, Field(ge=1, le=120)] = 20
    problem: RoutingProblem

    @model_validator(mode="after")
    def validate_order_tasks(self) -> PlanningJobCreate:
        order_ids = set(self.order_ids)
        task_orders = {stop.order_id for stop in self.problem.stops if stop.order_id is not None}
        if not task_orders.issubset(order_ids):
            raise ValueError("Every routing-stop order_id must appear in order_ids")
        delivered_orders = {
            stop.order_id
            for stop in self.problem.stops
            if stop.order_id is not None and stop.stop_type == RoutingStopType.DELIVERY
        }
        if not order_ids.issubset(delivered_orders):
            raise ValueError("Every order_id must have at least one delivery stop")
        return self


class AcceptanceResponseCalibration(BaseModel):
    model_version: Annotated[str, Field(min_length=1, max_length=80)]
    training_region: Annotated[str, Field(min_length=1, max_length=100)]
    trained_at: datetime
    intercept: float = Field(allow_inf_nan=False)
    pay_linear: float = Field(allow_inf_nan=False)
    pay_quadratic: float = Field(allow_inf_nan=False)
    density_effect: float = Field(allow_inf_nan=False)
    unattended_effect: float = Field(allow_inf_nan=False)
    evening_effect: float = Field(allow_inf_nan=False)
    pay_density: float = Field(allow_inf_nan=False)
    pay_squared_density: float = Field(allow_inf_nan=False)
    pay_unattended: float = Field(allow_inf_nan=False)
    pay_squared_unattended: float = Field(allow_inf_nan=False)
    pay_evening: float = Field(allow_inf_nan=False)
    pay_squared_evening: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_model_timestamp(self) -> AcceptanceResponseCalibration:
        if self.trained_at.tzinfo is None:
            raise ValueError("trained_at must include a timezone")
        return self


class PayQuoteRequest(BaseModel):
    task_id: UUID
    min_compensation_cents: Annotated[int, Field(ge=0, le=100_000)]
    max_compensation_cents: Annotated[int, Field(ge=1, le=100_000)]
    step_cents: Annotated[int, Field(ge=1, le=5_000)] = 25
    target_acceptance_minutes: Annotated[float, Field(gt=0, le=1440, allow_inf_nan=False)]
    density_index: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    unattended_share: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    is_evening: bool
    calibration: AcceptanceResponseCalibration

    @model_validator(mode="after")
    def validate_pay_search(self) -> PayQuoteRequest:
        if self.max_compensation_cents < self.min_compensation_cents:
            raise ValueError("max_compensation_cents must be at least min_compensation_cents")
        if (self.max_compensation_cents - self.min_compensation_cents) // self.step_cents > 5000:
            raise ValueError("Pay search may contain at most 5,001 candidate values")
        return self


class AllocationTaskInput(BaseModel):
    task_id: UUID
    compensation_budget_cents: Annotated[int, Field(ge=0, le=100_000)]
    allow_target_miss: bool = False


class AllocationDriverInput(BaseModel):
    driver_id: UUID
    max_tasks_this_round: Annotated[int, Field(ge=1, le=20)] = 1


class AllocationCandidateInput(BaseModel):
    task_id: UUID
    driver_id: UUID
    pay_quote_id: UUID
    fit_score: Annotated[int, Field(ge=0, le=100)]
    available: bool = True


class AllocationWeights(BaseModel):
    fit: Annotated[int, Field(ge=0, le=10_000)] = 100
    compensation: Annotated[int, Field(ge=0, le=100)] = 1
    response_time: Annotated[int, Field(ge=0, le=10_000)] = 10
    fairness: Annotated[int, Field(ge=0, le=10_000)] = 50


class DriverAllocationRequest(BaseModel):
    tasks: Annotated[list[AllocationTaskInput], Field(min_length=1, max_length=100)]
    drivers: Annotated[list[AllocationDriverInput], Field(min_length=1, max_length=100)]
    candidates: Annotated[list[AllocationCandidateInput], Field(max_length=5000)]
    weights: AllocationWeights = Field(default_factory=AllocationWeights)
    solver_time_limit_seconds: Annotated[int, Field(ge=1, le=10)] = 3

    @model_validator(mode="after")
    def validate_candidate_references(self) -> DriverAllocationRequest:
        task_ids = [task.task_id for task in self.tasks]
        driver_ids = [driver.driver_id for driver in self.drivers]
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("task IDs must be unique")
        if len(set(driver_ids)) != len(driver_ids):
            raise ValueError("driver IDs must be unique")
        task_set = set(task_ids)
        driver_set = set(driver_ids)
        candidate_pairs: set[tuple[UUID, UUID]] = set()
        for candidate in self.candidates:
            if candidate.task_id not in task_set or candidate.driver_id not in driver_set:
                raise ValueError("Every candidate must reference a submitted task and driver")
            key = (candidate.task_id, candidate.driver_id)
            if key in candidate_pairs:
                raise ValueError("Duplicate task-driver candidate")
            candidate_pairs.add(key)
        return self


class DriverAssignmentResponseStatus(StrEnum):
    ACCEPTED = "accepted"
    DECLINED = "declined"


class DriverAssignmentResponse(BaseModel):
    status: DriverAssignmentResponseStatus
    note: Annotated[str, Field(max_length=500)] | None = None


class ParcelSearch(BaseModel):
    geometry: dict[str, Any]
    minimum_area_sq_m: Annotated[float, Field(gt=0)] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    offset: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_geojson_type(self) -> ParcelSearch:
        if self.geometry.get("type") not in {"Polygon", "MultiPolygon"}:
            raise ValueError("geometry must be a GeoJSON Polygon or MultiPolygon")
        if "coordinates" not in self.geometry:
            raise ValueError("geometry must include GeoJSON coordinates")
        return self

class VendorCommitmentCreate(BaseModel):
    farm_id: UUID
    product_id: UUID
    delivery_date: date
    requested_quantity: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=3)]
    unit: Annotated[str, Field(min_length=1, max_length=24)]
    ready_by: datetime

    @model_validator(mode="after")
    def validate_ready_by(self) -> VendorCommitmentCreate:
        if self.ready_by.tzinfo is None:
            raise ValueError("ready_by must include a timezone")
        return self


class VendorCommitmentStatus(StrEnum):
    CONFIRMED = "confirmed"
    SHORTFALL = "shortfall"
    REJECTED = "rejected"


class VendorCommitmentResponse(BaseModel):
    status: VendorCommitmentStatus
    confirmed_quantity: Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=3)] | None = None
    ready_at: datetime | None = None
    note: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_response(self) -> VendorCommitmentResponse:
        if self.status in {VendorCommitmentStatus.CONFIRMED, VendorCommitmentStatus.SHORTFALL}:
            if self.confirmed_quantity is None or self.ready_at is None:
                raise ValueError("confirmed quantity and timezone-aware ready_at are required")
            if self.ready_at.tzinfo is None:
                raise ValueError("ready_at must include a timezone")
        return self


class DriverEventType(StrEnum):
    ARRIVED = "arrived"
    PICKED_UP = "picked_up"
    DELIVERED = "delivered"
    FAILED = "failed"
    QUALITY_REJECTED = "quality_rejected"


class DriverEventCreate(BaseModel):
    event_type: DriverEventType
    occurred_at: datetime
    latitude: Annotated[float, Field(ge=-90, le=90)] | None = None
    longitude: Annotated[float, Field(ge=-180, le=180)] | None = None
    note: Annotated[str, Field(max_length=500)] | None = None

    @model_validator(mode="after")
    def validate_event(self) -> DriverEventCreate:
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be supplied together")
        return self


def canonical_order_payload(payload: OrderCreate) -> str:
    normalized = payload.model_dump(mode="json")
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))
