from __future__ import annotations

import csv
import ipaddress
from dataclasses import dataclass
from pathlib import Path

from .errors import WorkerError


IDENTIFIER_FIELDS = ("garage_number", "vehicle_id", "plate")
OPTIONAL_FIELDS = ("mac", "model", "branch", "updated_at", "comment")


@dataclass(frozen=True, slots=True)
class VehicleInventoryRecord:
    garage_number: str
    vehicle_id: str
    plate: str
    ip: str
    mac: str = ""
    model: str = ""
    branch: str = ""
    updated_at: str = ""
    comment: str = ""

    @property
    def number(self) -> str:
        return self.garage_number or self.vehicle_id or self.plate

    def to_vehicle(self) -> dict[str, str]:
        return {"number": self.number, "ip": self.ip}

    def to_response(self) -> dict[str, str]:
        response = {
            "number": self.number,
            "garage_number": self.garage_number,
            "vehicle_id": self.vehicle_id,
            "plate": self.plate,
            "ip": self.ip,
        }
        for field in OPTIONAL_FIELDS:
            value = getattr(self, field)
            if value:
                response[field] = value
        return response


class VehicleInventory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: list[VehicleInventoryRecord] = []
        self._loaded_mtime_ns: int | None = None

    def resolve_query(self, query: str) -> VehicleInventoryRecord:
        query = normalize_identifier(query)
        if not query:
            raise WorkerError(422, "vehicle_ip_not_found", "Vehicle query is empty")
        records = self._load_records()
        for field in IDENTIFIER_FIELDS:
            matches = [record for record in records if normalize_identifier(getattr(record, field)) == query]
            if len(matches) == 1:
                return _validate_resolved_record(matches[0])
            if len(matches) > 1:
                raise WorkerError(409, "vehicle_inventory_ambiguous", "Vehicle inventory query is ambiguous")
        raise WorkerError(422, "vehicle_ip_not_found", "Vehicle IP not found in inventory")

    def resolve_vehicle(self, vehicle: dict[str, object]) -> VehicleInventoryRecord:
        for field in IDENTIFIER_FIELDS:
            value = vehicle.get(field)
            if isinstance(value, str) and value.strip():
                return self._resolve_by_field(field, value)
        number = vehicle.get("number")
        if isinstance(number, str) and number.strip():
            return self.resolve_query(number)
        raise WorkerError(422, "vehicle_ip_not_found", "Vehicle identifier is required for inventory lookup")

    def _resolve_by_field(self, field: str, value: str) -> VehicleInventoryRecord:
        query = normalize_identifier(value)
        matches = [record for record in self._load_records() if normalize_identifier(getattr(record, field)) == query]
        if len(matches) == 1:
            return _validate_resolved_record(matches[0])
        if len(matches) > 1:
            raise WorkerError(409, "vehicle_inventory_ambiguous", "Vehicle inventory query is ambiguous")
        raise WorkerError(422, "vehicle_ip_not_found", "Vehicle IP not found in inventory")

    def _load_records(self) -> list[VehicleInventoryRecord]:
        try:
            stat = self.path.stat()
        except OSError as exc:
            raise WorkerError(422, "vehicle_ip_not_found", "Vehicle inventory is not configured") from exc
        if self._loaded_mtime_ns == stat.st_mtime_ns:
            return self._records

        records: list[VehicleInventoryRecord] = []
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row_number, row in enumerate(reader, start=2):
                record = _record_from_row(row, row_number)
                records.append(record)
        self._records = records
        self._loaded_mtime_ns = stat.st_mtime_ns
        return self._records


def normalize_plate(value: str) -> str:
    return " ".join(value.split())


def normalize_identifier(value: str) -> str:
    return normalize_plate(str(value).strip())


def _record_from_row(row: dict[str, str | None], row_number: int) -> VehicleInventoryRecord:
    values = {key: normalize_plate(str(row.get(key) or "")) for key in (*IDENTIFIER_FIELDS, "ip", *OPTIONAL_FIELDS)}
    if not values["ip"]:
        raise WorkerError(422, "vehicle_ip_not_found", f"Vehicle inventory row {row_number} has empty IP")
    if not any(values[field] for field in IDENTIFIER_FIELDS):
        raise WorkerError(400, "invalid_request", f"Vehicle inventory row {row_number} has no identifier")
    return VehicleInventoryRecord(**values)


def _validate_resolved_record(record: VehicleInventoryRecord) -> VehicleInventoryRecord:
    try:
        ipaddress.ip_address(record.ip)
    except ValueError as exc:
        raise WorkerError(422, "vehicle_ip_not_found", "Vehicle inventory record has invalid IP") from exc
    return record
