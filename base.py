import json
import inspect
import logging
from dataclasses import dataclass
from abc import abstractmethod
from typing import Type, List, Optional, Any, Callable, Union, Literal, Dict, get_origin

from pydantic import BaseModel, Extra, ValidationError
from django.urls import path
from django.http import HttpResponse, HttpRequest, Http404, JsonResponse, HttpResponseBadRequest
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from .permissions import BasePermission, BaseThrottler


class BaseResource:

    pk = 'id'

    class Create(BaseModel, extra=Extra.allow):
        pass

    class Update(Create):
        pass

    class Retrieve(Update):
        pass


@dataclass(frozen=True)
class MethodMapping(BaseModel, extra=Extra.allow):
    methods: List[str]
    detail: bool
    url_path: str
    func: Callable
    resource: Optional[Type[BaseResource]] = None

    @property
    def func_name(self) -> str:
        return self.func.__name__

    def build_methods_map(self) -> dict:
        d = {}
        for meth in self.methods:
            d[meth.lower()] = self.func_name
        return d


def action(
    methods: List[Literal['GET', 'POST', 'PUT', 'DELETE']] = None,
    detail: bool = True,
    url_path: str = None, resource: Type[BaseResource] = None
):

    def decorator(func):

        method_mapping = MethodMapping(
            methods=methods or ['GET'], detail=detail,
            url_path=url_path or func.__name__,
            func=func,
            resource=resource
        )
        return method_mapping

    return decorator


class BaseController:

    Resource: Type[BaseResource] = BaseResource
    PERMISSIONS: List[Type[BasePermission]] = []
    THROTTLERS: List[Type[BaseThrottler]] = []

    class Context(BaseModel, extra=Extra.allow):
        ...

    class Metadata(BaseModel):
        total_count: Optional[int] = None
        content_type: Optional[str] = None
        content_name: Optional[str] = None

    def __init__(self, context: Context, *args, **kwargs):
        self.context = context
        self.method: Optional[str] = None
        self.metadata = self.Metadata()

    async def check_permission(
        self, request: HttpRequest, handler: Union[Callable, MethodMapping]
    ) -> bool:
        for perm in self.PERMISSIONS:
            ok, err_msg = await perm.validate(
                user=self.context.user, request=request
            )
            if not ok:
                return False
        for th in self.THROTTLERS:
            ok, err_msg = await th.validate(
                user=self.context.user, request=request
            )
            if not ok:
                return False
        return True

    @abstractmethod
    async def get_one(self, pk: Any, **filters) -> Optional[Resource.Retrieve]:
        ...

    @abstractmethod
    async def get_many(
        self, order_by: Any = 'id', limit: int = None,
        offset: int = None,  **filters
    ) -> List[Resource.Retrieve]:
        ...


class BaseAsyncHttpTransport(View):

    Controller: Type[BaseController] = BaseController

    METHOD_MAP = {}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.controller: Optional[BaseController] = None

    @classmethod
    def create_type_for(
        cls, controller: Type[BaseController], method_map: Dict = None
    ) -> Type['BaseAsyncHttpTransport']:
        kwargs = {'Controller': controller}
        if method_map:
            kwargs['METHOD_MAP'] = method_map
        return type(cls.__name__, (cls,), {**kwargs})

    @csrf_exempt
    def dispatch(self, request: HttpRequest, *args, **kwargs):
        resp = self._get_controller_method(request)
        if isinstance(resp, HttpResponse):
            
            async def _wrapper():
                return resp
            
            return _wrapper()
        else:
            controller_method = resp
        if controller_method and controller_method in dir(self.Controller):
            context = self.Controller.Context()
            self.controller = self.Controller(context)
            self.controller.method = request.method
            controller_handler = getattr(self.controller, controller_method.lower())  # noqa
            resource = self.Controller.Resource
            kwargs['controller_handler'] = controller_handler
            if isinstance(controller_handler, MethodMapping):
                kwargs['resource'] = controller_handler.resource or resource
            else:
                kwargs['resource'] = resource
            kwargs['context'] = context
            handler = super().dispatch(
                request, *args, **kwargs
            )
            return handler
        else:
            return self.http_method_not_allowed(request, *args, **kwargs)

    async def get(
        self, request: HttpRequest, controller_handler: Callable,
        context: Controller.Context, resource: Type[BaseResource],
        *args, **kwargs
    ) -> HttpResponse:
        await self._auth(request, context)
        extra = self._extra_params(request)
        return await self.transport(
            controller_handler, resource, request, context=context,
            *args,  **dict(dict(**extra) | dict(**kwargs))
        )

    async def post(
        self, request: HttpRequest, controller_handler: Callable,
        context: Controller.Context, resource: Type[BaseResource],
        *args, **kwargs
    ) -> HttpResponse:
        await self._auth(request, context)
        resp = await self._idempotent_method(
            request, controller_handler, resource,
            context=context,
            model=resource.Create,
            *args, **kwargs
        )
        if resp.status_code == 200:
            resp.status_code = 201
        return resp

    async def put(
        self, request: HttpRequest, controller_handler: Callable,
        context: Controller.Context, resource: Type[BaseResource],
        *args, **kwargs
    ) -> HttpResponse:
        await self._auth(request, context)
        return await self._idempotent_method(
            request, controller_handler, resource,
            context=context,
            model=resource.Update,
            *args, **kwargs
        )

    async def delete(
        self, request: HttpRequest, controller_handler: Callable,
        context: Controller.Context, resource: Type[BaseResource],
        *args, **kwargs
    ) -> HttpResponse:
        await self._auth(request, context)
        resp = await self._idempotent_method(
            request, controller_handler, resource,
            context=context,
            model=None,
            *args, **kwargs
        )
        if resp.status_code == 200:
            resp.status_code = 204
        return resp

    @abstractmethod
    async def transport(
        self, handler, resource: Type[BaseResource],
        request: HttpRequest, context: Controller.Context,
        *args, **kwargs
    ) -> HttpResponse:
        ...

    def _get_controller_method(
        self, request: HttpRequest
    ) -> Optional[Union[str, HttpResponse]]:
        method = request.method.lower() if request.method else ''
        controller_method = self.METHOD_MAP.get(method)
        return controller_method

    async def _idempotent_method(
        self, request: HttpRequest, controller_handler: Callable,
        resource: Type[BaseResource],
        context: Controller.Context,
        model: Union[Type[BaseResource.Update], Type[BaseResource.Create]] = None,
        *args, **kwargs
    ) -> HttpResponse:
        if request.content_type != 'application/json' and request.method.lower() != 'delete':
            return HttpResponseBadRequest(
                f'Content Type {request.content_type} not allowed !'
            )
        if model is not None:
            if not request.body:
                return HttpResponseBadRequest('Request has empty body !')
            try:
                payload = json.loads(request.body.decode())
                if isinstance(payload, list):
                    data = [
                        model.model_validate(
                            obj=item,
                            strict=True,
                        ) for item in payload
                    ]
                else:
                    data = model.model_validate(
                        obj=payload,
                        strict=True,
                    )
                kwargs['data'] = data
            except ValueError as e:
                return HttpResponseBadRequest(str(e))
        spec = self._get_controller_handler_spec(controller_handler)
        return await self.transport(
            controller_handler, resource, request,
            context=context,
            *args, **kwargs
        )

    @classmethod
    def _extra_params(cls, request: HttpRequest) -> dict:
        return dict(request.GET)

    async def _auth(
        self, request: HttpRequest, context: Controller.Context
    ):
        # авторизация пока не реализована
        pass

    @classmethod
    def _get_controller_handler_spec(cls, controller_handler):
        if isinstance(controller_handler, MethodMapping):
            return inspect.getfullargspec(controller_handler.func)
        else:
            return inspect.getfullargspec(controller_handler)

    @classmethod
    def _restore_bound_method(cls, meth: Callable, bound_to):

        async def bound_method(*args, **kwargs):
            return await meth(bound_to, *args, **kwargs)

        return bound_method

    @classmethod
    def _clean_args(cls, handler: Callable, **kwargs) -> dict:
        spec = inspect.getfullargspec(handler)
        ret = {}
        for name, val in kwargs.items():
            if name in spec.annotations:
                typ = spec.annotations[name]
                typ_is_generic = (
                    typ is not Any
                    and get_origin(typ) is not None
                )
                if typ is not Any and isinstance(val, list) and len(val) == 1 and not typ_is_generic and not isinstance(val, typ):
                    val = typ(val[0])
                else:
                    try:
                        val = typ(val)
                    except Exception:
                        pass
                ret[name] = val
            elif spec.varkw:
                if isinstance(val, list) and len(val) == 1:
                    val = val[0]
                ret[name] = val
        return ret


class SingleResourceAsyncHttpTransport(BaseAsyncHttpTransport):

    METHOD_MAP = {
        'get': 'get_one',
        'put': 'update_one',
        'delete': 'delete_one'
    }

    async def transport(
        self, handler,
        resource: Type[BaseResource],
        request: HttpRequest, context: BaseController.Context,
        *args, **kwargs
    ) -> HttpResponse:
        if resource.pk not in kwargs:
            raise Http404('Missing retrieve resource pk')
        else:
            spec = self._get_controller_handler_spec(handler)
            if 'pk' in spec.args:
                pk_field = resource.Retrieve.model_fields.get(resource.pk)
                if not pk_field:
                    logging.critical(
                        f'{self.controller.__class__}.{request.path} '
                        f'empty pk filed metadata'
                    )
                    kwargs['pk'] = kwargs.pop(resource.pk)
                else:
                    try:
                        kwargs['pk'] = pk_field.annotation(kwargs.pop(resource.pk))
                    except ValueError:
                        raise Http404
            try:
                if not await self.controller.check_permission(
                        request, handler
                ):
                    return HttpResponse(status=403)
                if isinstance(handler, MethodMapping):
                    handler = self._restore_bound_method(handler.func, self.controller)  # noqa
                extra = self._extra_params(request)
                extra = self._clean_args(handler, **extra)
                extra.pop(resource.pk, None)
                data: Optional[BaseResource.Retrieve] = await handler(
                    **dict(dict(**extra) | dict(**kwargs))
                )
            except Exception as e:
                if isinstance(e, ValidationError):
                    return HttpResponse(
                        status=400,
                        content=str(e.errors()).encode()
                    )
                if isinstance(e, ValueError):
                    msg = str(e.args[0]) if e.args else ''
                    return HttpResponse(
                        status=400,
                        content=msg.encode()
                    )
                raise
            if data is not None:
                if isinstance(data, HttpResponse):
                    resp = data
                else:
                    if isinstance(data, list):
                        content = [
                            item.model_dump(mode='json') for item in data
                        ]
                        resp = JsonResponse(content, safe=False)
                    else:
                        content = data.model_dump(mode='json')
                        resp = JsonResponse(content)

                if self.controller.metadata.content_type:
                    resp.headers['Content-Type'] = self.controller.metadata.content_type  # noqa
                if self.controller.metadata.content_name:
                    resp.headers['Content-Disposition'] = self.controller.metadata.content_name
                return resp
            else:
                raise Http404


class ManyResourceAsyncHttpTransport(BaseAsyncHttpTransport):

    METHOD_MAP = {
        'get': 'get_many',
        'put': 'update_many',
        'delete': 'delete_many'
    }

    async def transport(
        self, handler, resource: Type[BaseResource],
        request: HttpRequest, context: BaseController.Context,
        *args, **kwargs
    ) -> HttpResponse:
        extra = self._extra_params(request)
        try:
            if not await self.controller.check_permission(
                request, handler
            ):
                return HttpResponse(status=403)
            if isinstance(handler, MethodMapping):
                handler = self._restore_bound_method(handler.func, self.controller)  # noqa
            kwargs = dict(dict(**extra) | dict(**kwargs))
            kwargs = self._clean_args(handler, **kwargs)
            data: Optional[BaseResource.Retrieve] = await handler(**kwargs)
        except Exception as e:
            if isinstance(e, ValidationError):
                return HttpResponse(
                    status=400,
                    content=str(e.errors()).encode()
                )
            if isinstance(e, ValueError):
                msg = str(e.args[0]) if e.args else ''
                return HttpResponse(
                    status=400,
                    content=msg.encode()
                )
            logging.exception('Transport Exc', stacklevel=5)
            raise
        if data is not None:
            if isinstance(data, HttpResponse):
                resp = data
            else:
                if isinstance(data, list):
                    content = [
                        item.model_dump(mode='json') for item in data
                    ]
                else:
                    content = data.model_dump(mode='json')
                resp = JsonResponse(content, safe=False)
            if self.controller.metadata.total_count:
                resp.headers['X-Total-Count'] = self.controller.metadata.total_count  # noqa
            if self.controller.metadata.content_type:
                resp.headers['Content-Type'] = self.controller.metadata.content_type  # noqa
            if self.controller.metadata.content_name:
                resp.headers['Content-Disposition'] = self.controller.metadata.content_name
            return resp
        else:
            raise Http404

    def _get_controller_method(
        self, request: HttpRequest
    ) -> Optional[Union[str, HttpResponse]]:
        if request.method.lower() == 'post':
            if request.content_type == 'application/json':
                if not request.body:
                    return HttpResponseBadRequest('Request has empty body !')
                try:
                    data = json.loads(request.body.decode())
                    controller_method = super()._get_controller_method(request)
                    if controller_method is None:
                        controller_method = 'create_one'
                    if controller_method == 'create_one' and isinstance(data, list):  # noqa
                        controller_method = 'create_many'
                    return controller_method
                except ValueError as e:
                    return HttpResponseBadRequest(str(e))
            else:
                return HttpResponseBadRequest(
                    f'Content Type {request.content_type} not allowed !'
                )
        else:
            return super()._get_controller_method(request)


class HttpRouter:

    @dataclass
    class PathConfig:
        route: str
        view: Any
        name: str
        kwargs: dict = None

        def build(self):
            return path(
                self.route, self.view,
                kwargs=self.kwargs, name=self.name
            )

    def __init__(
        self, base_url: str,
        single_transport: Type[SingleResourceAsyncHttpTransport] = None,
        many_transport: Type[ManyResourceAsyncHttpTransport] = None
    ):
        if base_url.startswith('/'):
            base_url = base_url[1:]
        self._base_url = base_url
        self._routes: List[HttpRouter.PathConfig] = []
        self._single_transport = single_transport or SingleResourceAsyncHttpTransport  # noqa
        self._many_transport = many_transport or ManyResourceAsyncHttpTransport  # noqa

    @property
    def paths(self) -> list:
        return [r.build() for r in self._routes]

    def append(self, router: 'HttpRouter'):
        for sub_route in router._routes:
            self._routes.append(
                self.PathConfig(
                    route=self._base_url + '/' + sub_route.route,
                    view=sub_route.view,
                    name=self._base_url + '-' + sub_route.name
                )
            )

    def register(self, url: str, controller: Type[BaseController]):
        if url.startswith('/'):
            url = url[1:]
        pk = controller.Resource.pk
        mappings = []
        for attr in dir(controller):
            if not attr.startswith('__'):
                val = getattr(controller, attr)
                if isinstance(val, MethodMapping):
                    mappings.append(val)
        retrieve_pk_url = f'/<{pk}>'
        retrieve_transport_cls = self._single_transport.create_type_for(
            controller
        )
        many_transport_cls = self._many_transport.create_type_for(
            controller
        )
        # actions
        for act_map in mappings:
            if act_map.detail:
                retrieve_action_transport_cls = self._single_transport.create_type_for(  # noqa
                    controller, method_map=act_map.build_methods_map()
                )
                self._routes.append(
                    self.PathConfig(
                        route=self._base_url + '/' + url + retrieve_pk_url + '/' + act_map.url_path,  # noqa
                        view=retrieve_action_transport_cls.as_view(),
                        name=f'{url}-retrieve-{act_map.func_name}'
                    )
                )
            else:
                many_action_transport_cls = self._many_transport.create_type_for( # noqa
                    controller, method_map=act_map.build_methods_map()
                )
                self._routes.append(
                    self.PathConfig(
                        route=self._base_url + '/' + url + '/' + act_map.url_path,
                        view=many_action_transport_cls.as_view(),
                        name=f'{url}-many-{act_map.func_name}'
                    )
                )
        # basic routes
        self._routes.append(
            self.PathConfig(
                route=self._base_url + '/' + url + retrieve_pk_url,
                view=retrieve_transport_cls.as_view(),
                name=f'{url}-retrieve'
            )
        )
        self._routes.append(
            self.PathConfig(
                route=self._base_url + '/' + url,
                view=many_transport_cls.as_view(),
                name=f'{url}-many'
            )
        )
