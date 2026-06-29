from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ValidationError


@dataclass(frozen=True, slots=True)
class OperationSpec:
    name: str
    title: str
    risk: str
    requires: tuple[str, ...]
    timeout_sec_default: int
    roles_allowed: tuple[str, ...]
    state_changing: bool = False
    params_schema: dict[str, Any] | None = None

    def to_capability(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "risk": self.risk,
            "requires": list(self.requires),
            "timeout_sec_default": self.timeout_sec_default,
            "params_schema": self.params_schema or {},
            "roles_allowed": list(self.roles_allowed),
        }


MVP_OPERATIONS: dict[str, OperationSpec] = {
    "vehicle_reachability": OperationSpec(
        name="vehicle_reachability",
        title="Проверить доступность",
        risk="read_only",
        requires=("vpn",),
        timeout_sec_default=60,
        roles_allowed=("engineer", "admin"),
    ),
    "basic_status": OperationSpec(
        name="basic_status",
        title="Состояние ТС",
        risk="read_only",
        requires=("vpn", "ssh"),
        timeout_sec_default=90,
        roles_allowed=("engineer", "admin"),
    ),
    "validators_status": OperationSpec(
        name="validators_status",
        title="Валидаторы",
        risk="read_only",
        requires=("vpn", "ssh"),
        timeout_sec_default=120,
        roles_allowed=("engineer", "admin"),
    ),
    "collect_bundle_light": OperationSpec(
        name="collect_bundle_light",
        title="Лёгкий диагностический пакет",
        risk="read_only",
        requires=("vpn", "ssh"),
        timeout_sec_default=180,
        roles_allowed=("engineer", "admin"),
    ),
}


def get_operation(name: str) -> OperationSpec:
    try:
        return MVP_OPERATIONS[name]
    except KeyError as exc:
        raise ValidationError("Операция недоступна в MVP", "operation_not_allowed", 403) from exc


def capabilities_response() -> dict[str, object]:
    return {
        "api_version": "1.0",
        "capabilities_version": "2026-06-29",
        "operations": [spec.to_capability() for spec in MVP_OPERATIONS.values()],
    }
