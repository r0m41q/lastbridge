"""Typed access to the application's single composition container."""

from typing import TYPE_CHECKING, cast

from starlette.requests import Request

if TYPE_CHECKING:
    from lastbridge.bootstrap import Services


def services(request: Request) -> Services:
    return cast("Services", request.app.state.services)
