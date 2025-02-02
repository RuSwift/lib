from typing import Any, List, Optional

from .base import BaseController, BaseResource, action
from .mixins import MixinCreateOne, MixinUpdateOne, MixinDeleteOne, \
    MixinCreateMany, MixinDeleteMany


class SampleResource(BaseResource):

    class Create(BaseResource.Create):
        x: int
        y: str

    class Update(Create):
        pass

    class Retrieve(Update):
        id: int
        comment: Optional[str] = None


class AlterResource(BaseResource):

    class Create(BaseResource.Create):
        z: str

    class Update(Create):
        pass

    class Retrieve(Update):
        id: int


class SampleController(
    MixinUpdateOne, MixinCreateOne, MixinDeleteOne,
    MixinCreateMany, MixinDeleteMany, BaseController
):

    Resource = SampleResource

    storage = {
        1: Resource.Retrieve(
            id=1,
            x=123,
            y='value-1'
        ),
        2: Resource.Retrieve(
            id=2,
            x=321,
            y='value-2'
        )
    }

    async def get_one(self, pk: int, **filters) -> Optional[Resource.Retrieve]:
        resource = self.storage.get(pk)
        return resource

    async def create_one(
        self, data: Resource.Create, **extra
    ) -> Resource.Retrieve:
        max_id = max(self.storage.keys())
        new_id = max_id + 1
        new_resource = self.Resource.Retrieve(
            id=new_id,
            **data.model_dump(mode='python')
        )
        self.storage[new_id] = new_resource
        return new_resource

    async def create_many(self, data: List[BaseResource.Create], **extra) -> List[BaseResource.Retrieve]:
        res = []
        for rec in data:
            res.append(await self.create_one(rec, **extra))
        return res

    async def update_one(
        self, pk: int, data: Resource.Update, **extra
    ) -> Optional[Resource.Retrieve]:
        resource = self.storage.get(pk)
        if resource:
            updated = resource.model_copy(update=data.model_dump())
            self.storage[pk] = updated
            return updated
        else:
            return None

    @action(detail=True)
    async def action_detail(self, pk, **extra) -> Optional[BaseResource.Retrieve]:
        return self.Resource.Retrieve(
            x=0,
            y='0',
            id=pk,
            comment=f'detailed-{pk}-{extra}'
        )

    @action(detail=True, url_path='all-methods', methods=['GET', 'POST'])
    async def action_detail_all_methods(self, pk, **extra) -> Optional[BaseResource.Retrieve]:
        return self.Resource.Retrieve(
            x=0,
            y='0',
            id=pk,
            comment=f'detailed-{pk}-{extra}'
        )

    @action(detail=True, url_path='alter-resource', resource=AlterResource)
    async def action_alter_resource(self, pk, **extra) -> Optional[AlterResource.Retrieve]:
        return AlterResource.Retrieve(
            id=1, z='alter'
        )

    @action(detail=False, url_path='action-many')
    async def action_detail_many(self, order_by: Any = 'id', limit: int = None, offset: Any = None, **filters) -> List[
        Resource.Retrieve]:
        return [self.Resource.Retrieve(
            x=0,
            y='0',
            id=1
        )]

    async def delete_one(self, pk: Any, **extra) -> Optional[BaseResource.Retrieve]:
        resource = self.storage.get(pk)
        if resource:
            del self.storage[pk]
            return resource
        else:
            return None

    async def get_many(
        self, order_by: Any = 'id', limit: int = None, offset: Any = None, **filters
    ) -> List[Resource.Retrieve]:
        values = [r for r in self.storage.values()]
        self.metadata.total_count = len(values)
        return values

    async def delete_many(self, id: List[int] = None, **extra) -> List[BaseResource.Retrieve]:
        if id:
            ret = []
            for pk in id:
                resource = await self.delete_one(int(pk))
                if resource:
                    ret.append(resource)
            return ret
        else:
            return []
