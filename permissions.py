from abc import abstractmethod
from typing import Tuple, Optional

from django.http import HttpRequest


ERR_MSG = str


class BasePermission:

    @classmethod
    @abstractmethod
    async def validate(
        cls, user, request: HttpRequest
    ) -> Tuple[bool, Optional[ERR_MSG]]:
        ...


class BaseThrottler:

    @classmethod
    @abstractmethod
    async def validate(
        cls, user, request: HttpRequest
    ) -> Tuple[bool, Optional[ERR_MSG]]:
        ...
