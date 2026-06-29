from __future__ import annotations


class WorkerError(Exception):
    def __init__(self, status: int, error_code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_code = error_code
        self.message = message

    def to_response(self) -> dict[str, object]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
        }


class ValidationError(WorkerError):
    def __init__(self, message: str, error_code: str = "invalid_request", status: int = 400) -> None:
        super().__init__(status, error_code, message)
