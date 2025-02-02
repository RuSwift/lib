from abc import abstractmethod
from typing import Any, Optional, List

from .base import BaseResource


class MixinCreateOne:

    @abstractmethod
    async def create_one(
        self, data: BaseResource.Create, **extra
    ) -> BaseResource.Retrieve:
        ...


class MixinUpdateOne:

    @abstractmethod
    async def update_one(
        self, pk: Any, data: BaseResource.Update, **extra
    ) -> Optional[BaseResource.Retrieve]:
        ...


class MixinDeleteOne:

    @abstractmethod
    async def delete_one(
        self, pk: Any, **extra
    ) -> Optional[BaseResource.Retrieve]:
        ...


class MixinCreateMany:

    @abstractmethod
    async def create_many(
        self, data: List[BaseResource.Create], **extra
    ) -> List[BaseResource.Retrieve]:
        ...


class MixinUpdateMany:

    @abstractmethod
    async def update_many(
        self, data: List[BaseResource.Update], **extra
    ) -> List[BaseResource.Retrieve]:
        ...


class MixinDeleteMany:

    @abstractmethod
    async def delete_many(self, **extra) -> List[BaseResource.Retrieve]:
        ...
