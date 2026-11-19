"""API error type; the app factory turns it into {"error": message}."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def bad_request(msg: str) -> ApiError:
    return ApiError(400, msg)


def forbidden(msg: str = "forbidden") -> ApiError:
    return ApiError(403, msg)


def not_found(msg: str = "not found") -> ApiError:
    return ApiError(404, msg)


def conflict(msg: str) -> ApiError:
    return ApiError(409, msg)
