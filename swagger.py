from __future__ import annotations

import json
import inspect
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type, Union, get_args, get_origin

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.urls import path
from django.views import View

from pydantic import BaseModel

from .base import HttpRouter, BaseAsyncHttpTransport, MethodMapping, BaseController, BaseResource


# -------------------------
# Helpers: type -> OpenAPI schema
# -------------------------

_PRIMITIVE_MAP = {
    str: {"type": "string"},
    int: {"type": "integer"},
    float: {"type": "number"},
    bool: {"type": "boolean"},
    dict: {"type": "object"},
    list: {"type": "array"},
}


def _is_pydantic_model(t: Any) -> bool:
    try:
        return isinstance(t, type) and issubclass(t, BaseModel)
    except Exception:
        return False


def _model_ref_name(model: Type[BaseModel]) -> str:
    # Можно сделать уникальнее (с модулем), если есть коллизии
    return model.__name__


def _schema_from_type(t: Any) -> Dict[str, Any]:
    """
    Мини-конвертер type hints -> OpenAPI schema.
    Поддерживает BaseModel, Optional/Union, List[T], primitives.
    """
    if t is Any or t is None:
        return {}

    # Pydantic model => $ref
    if _is_pydantic_model(t):
        return {"$ref": f"#/components/schemas/{_model_ref_name(t)}"}

    origin = get_origin(t)
    args = get_args(t)

    # Optional[T] == Union[T, None]
    if origin is Union and args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            sch = _schema_from_type(non_none[0])
            # OpenAPI 3.0 style nullable
            if "$ref" in sch:
                return {"allOf": [sch], "nullable": True}
            return {**sch, "nullable": True}
        return {"oneOf": [_schema_from_type(a) for a in non_none]}

    # List[T]
    if origin in (list, List) and args:
        return {"type": "array", "items": _schema_from_type(args[0])}

    # Dict[str, T] (упрощенно)
    if origin in (dict, Dict):
        return {"type": "object"}

    # primitive
    if t in _PRIMITIVE_MAP:
        return _PRIMITIVE_MAP[t]

    # fallback
    return {"type": "string"}


def _pydantic_components(models: List[Type[BaseModel]]) -> Dict[str, Any]:
    """
    Собираем components.schemas из Pydantic v2 моделей.
    ref_template сразу делает ссылки на #/components/schemas/...
    """
    schemas: Dict[str, Any] = {}

    for m in models:
        name = _model_ref_name(m)
        js = m.model_json_schema(ref_template="#/components/schemas/{model}")

        # Pydantic v2 кладет вложенные определения в $defs
        defs = js.pop("$defs", None) or {}
        for def_name, def_schema in defs.items():
            schemas.setdefault(def_name, def_schema)

        schemas[name] = js

    return {"schemas": schemas}


# -------------------------
# OpenAPI generator (по HttpRouter)
# -------------------------

@dataclass
class _Operation:
    method: str
    controller: Type[BaseController]
    handler_name: str
    resource: Type[BaseResource]
    detail: bool
    action: Optional[MethodMapping] = None


class OpenAPIGenerator:
    def __init__(self, router: HttpRouter, title: str, version: str):
        self.router = router
        self.title = title
        self.version = version

    def build(self) -> Dict[str, Any]:
        ops = self._collect_operations()

        # собрать pydantic модели, которые попадут в components
        models: List[Type[BaseModel]] = []
        for op in ops:
            # базовые модели ресурса
            models.extend([
                op.resource.Create,
                op.resource.Update,
                op.resource.Retrieve,
            ])
            # + если аннотация return/params содержит pydantic модели
            handler = getattr(op.controller, op.handler_name, None)
            if handler:
                hints = getattr(handler, "__annotations__", {}) or {}
                for ht in hints.values():
                    self._collect_models_from_type(ht, models)

        components = _pydantic_components(self._dedupe_models(models))

        paths = self._build_paths(ops)

        return {
            "openapi": "3.0.3",
            "info": {"title": self.title, "version": self.version},
            "paths": paths,
            "components": components,
        }

    def _dedupe_models(self, models: List[Type[BaseModel]]) -> List[Type[BaseModel]]:
        seen = set()
        out = []
        for m in models:
            if _is_pydantic_model(m) and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    def _collect_models_from_type(self, t: Any, out: List[Type[BaseModel]]) -> None:
        if t is Any or t is None:
            return
        if _is_pydantic_model(t):
            out.append(t)
            return
        origin = get_origin(t)
        args = get_args(t)
        if origin is Union:
            for a in args:
                if a is not type(None):
                    self._collect_models_from_type(a, out)
        elif origin in (list, List):
            if args:
                self._collect_models_from_type(args[0], out)

    def _collect_operations(self) -> List[_Operation]:
        """
        Пробегаем router._routes и извлекаем:
        - view class (Transport)
        - Controller
        - METHOD_MAP (http verb -> controller method)
        """
        ops: List[_Operation] = []
        for r in getattr(self.router, "_routes", []):
            view_cls = r.view.view_class if hasattr(r.view, "view_class") else r.view
            if not inspect.isclass(view_cls):
                continue
            if not issubclass(view_cls, BaseAsyncHttpTransport):
                continue

            controller = getattr(view_cls, "Controller", None)
            if controller is None:
                continue

            # базовый ресурс контроллера
            resource = getattr(controller, "Resource", BaseResource)

            method_map: Dict[str, str] = getattr(view_cls, "METHOD_MAP", {}) or {}
            for http_method, handler_name in method_map.items():
                # create_one/create_many приходят из ManyTransport runtime-логики,
                # но METHOD_MAP на post отсутствует. Для документации добавим POST сами.
                ops.append(_Operation(
                    method=http_method.upper(),
                    controller=controller,
                    handler_name=handler_name,
                    resource=resource,
                    detail=self._is_detail_route(r.route, resource.pk),
                ))

            # Документируем POST для Many routes (у тебя логика в _get_controller_method)
            if self._is_many_route(r.route, resource.pk):
                # POST может стать create_one или create_many — покажем как oneOf
                if hasattr(controller, "create_one") or hasattr(controller, "create_many"):
                    ops.append(_Operation(
                        method="POST",
                        controller=controller,
                        handler_name="create_one",   # для summary
                        resource=resource,
                        detail=False,
                    ))

            # Дополнительно: actions — router строит отдельные view-классы с METHOD_MAP уже заданным
            # Поэтому они попадут автоматически через method_map выше.

        # Уберем дубликаты (route+method+handler)
        uniq = {}
        for op in ops:
            key = (op.method, op.controller.__name__, op.handler_name, op.detail)
            uniq[key] = op
        return list(uniq.values())

    @staticmethod
    def _is_detail_route(route: str, pk: str) -> bool:
        # в твоем роутере detail выглядит как ".../<pk>"
        return f"/<{pk}>" in route

    @staticmethod
    def _is_many_route(route: str, pk: str) -> bool:
        return f"/<{pk}>" not in route

    def _build_paths(self, ops: List[_Operation]) -> Dict[str, Any]:
        """
        Группируем операции по пути. Django route '/api/foo/<id>' => OpenAPI '/api/foo/{id}'
        """
        by_path: Dict[str, Dict[str, _Operation]] = {}
        for r in getattr(self.router, "_routes", []):
            raw = r.route
            view_cls = r.view.view_class if hasattr(r.view, "view_class") else r.view
            if not inspect.isclass(view_cls) or not issubclass(view_cls, BaseAsyncHttpTransport):
                continue
            controller = getattr(view_cls, "Controller", None)
            if controller is None:
                continue
            resource = getattr(controller, "Resource", BaseResource)

            path_oa = raw.replace(f"/<{resource.pk}>", f"/{{{resource.pk}}}")

            # собрать ops, которые относятся к этому view_cls
            method_map: Dict[str, str] = getattr(view_cls, "METHOD_MAP", {}) or {}
            for http_method, handler_name in method_map.items():
                op = _Operation(
                    method=http_method.upper(),
                    controller=controller,
                    handler_name=handler_name,
                    resource=resource,
                    detail=self._is_detail_route(raw, resource.pk),
                )
                by_path.setdefault(path_oa, {})[op.method.lower()] = op

            # добавить POST для many-route (см. выше)
            if self._is_many_route(raw, resource.pk) and (hasattr(controller, "create_one") or hasattr(controller, "create_many")):
                by_path.setdefault(path_oa, {})["post"] = _Operation(
                    method="POST",
                    controller=controller,
                    handler_name="create_one",
                    resource=resource,
                    detail=False,
                )

        # собрать OpenAPI paths
        paths: Dict[str, Any] = {}
        for p, methods in by_path.items():
            item: Dict[str, Any] = {}
            for m, op in methods.items():
                item[m] = self._build_operation(p, op)
            api_path = p
            if not api_path.startswith('/'):
                api_path = '/' + api_path
            paths[api_path] = item

        return paths

    def _build_operation(self, path_str: str, op: _Operation) -> Dict[str, Any]:
        ctrl = op.controller
        handler = getattr(ctrl, op.handler_name, None)

        summary = f"{ctrl.__name__}.{op.handler_name}"
        tags = [ctrl.__name__]

        parameters = self._build_parameters(path_str, op, handler)
        request_body = self._build_request_body(op)
        responses = self._build_responses(op, handler)

        operation: Dict[str, Any] = {
            "summary": summary,
            "tags": tags,
            "parameters": parameters,
            "responses": responses,
        }
        if request_body:
            operation["requestBody"] = request_body

        return operation

    def _build_parameters(self, path_str: str, op: _Operation, handler: Any) -> List[Dict[str, Any]]:
        params: List[Dict[str, Any]] = []

        # path param для detail
        if op.detail and f"{{{op.resource.pk}}}" in path_str:
            params.append({
                "name": op.resource.pk,
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            })

        # query params — по сигнатуре метода контроллера (то, что реально приводится по аннотациям)
        if handler:
            if isinstance(handler, MethodMapping):
                handler = handler.func
            sig = inspect.signature(handler)
            for name, p in sig.parameters.items():
                if name in ("self", op.resource.pk, "data"):
                    continue
                if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
                    continue

                ann = p.annotation if p.annotation is not inspect._empty else str
                schema = _schema_from_type(ann)
                required = (p.default is inspect._empty)
                params.append({
                    "name": name,
                    "in": "query",
                    "required": required,
                    "schema": schema or {"type": "string"},
                })

        return params

    def _build_request_body(self, op: _Operation) -> Optional[Dict[str, Any]]:
        # PUT /detail => Update
        if op.method == "PUT":
            return {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{_model_ref_name(op.resource.Update)}"}
                    }
                }
            }

        # POST /many => oneOf(Create, [Create])
        if op.method == "POST":
            create_ref = {"$ref": f"#/components/schemas/{_model_ref_name(op.resource.Create)}"}
            return {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "oneOf": [
                                create_ref,
                                {"type": "array", "items": create_ref},
                            ]
                        }
                    }
                }
            }

        return None

    def _build_responses(self, op: _Operation, handler: Any) -> Dict[str, Any]:
        # определить ответ по аннотации return, иначе по соглашениям
        schema: Dict[str, Any] = {}

        if handler and "return" in getattr(handler, "__annotations__", {}):
            schema = _schema_from_type(handler.__annotations__["return"])
        else:
            # соглашения для основных CRUD
            if op.method == "GET":
                if op.detail:
                    schema = {"$ref": f"#/components/schemas/{_model_ref_name(op.resource.Retrieve)}"}
                else:
                    schema = {"type": "array", "items": {"$ref": f"#/components/schemas/{_model_ref_name(op.resource.Retrieve)}"}}
            elif op.method in ("POST", "PUT"):
                # обычно возвращаем Retrieve или list[Retrieve]
                if op.method == "POST":
                    schema = {
                        "oneOf": [
                            {"$ref": f"#/components/schemas/{_model_ref_name(op.resource.Retrieve)}"},
                            {"type": "array", "items": {"$ref": f"#/components/schemas/{_model_ref_name(op.resource.Retrieve)}"}},
                        ]
                    }
                else:
                    schema = {"$ref": f"#/components/schemas/{_model_ref_name(op.resource.Retrieve)}"}

        return {
            "200": {
                "description": "OK",
                "content": {"application/json": {"schema": schema or {}}},
            },
            "400": {"description": "Bad Request"},
            "403": {"description": "Forbidden"},
            "404": {"description": "Not Found"},
            "429": {"description": "Too Many Requests"},
        }


# -------------------------
# Django views: /openapi.json + /docs
# -------------------------

class OpenAPIJsonView(View):
    generator: OpenAPIGenerator

    async def get(self, request: HttpRequest) -> JsonResponse:
        schema = self.generator.build()
        return JsonResponse(schema, safe=True)


class SwaggerUIView(View):
    """
    Простая Swagger UI страница через CDN.
    """
    schema_url: str

    async def get(self, request: HttpRequest) -> HttpResponse:
        html = f"""<!DOCTYPE html>
        <html>
            <head>
                <meta charset="utf-8"/>
                <title>Swagger UI</title>
                <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
            </head>
            <body>
                <div id="swagger-ui"></div>
                <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
                <script>
                    window.onload = () => {{
                    SwaggerUIBundle({{
                        url: "{self.schema_url}",
                        dom_id: '#swagger-ui',
                        presets: [SwaggerUIBundle.presets.apis],
                        layout: "BaseLayout"
                    }});
                    }};
                </script>
            </body>
        </html>
        """
        return HttpResponse(html, content_type="text/html; charset=utf-8")


class SwaggerRouter:
    def __init__(self, router: HttpRouter, title: str = "API", version: str = "0.1.0", base_url: str = ""):
        self.router = router
        self.title = title
        self.version = version
        self.base_url = base_url.rstrip("/")

    @property
    def urls(self):
        gen = OpenAPIGenerator(self.router, title=self.title, version=self.version)

        openapi_view = OpenAPIJsonView.as_view()
        openapi_view.view_class.generator = gen  # attach generator

        docs_view = SwaggerUIView.as_view()
        docs_view.view_class.schema_url = f"{self.base_url}/openapi.json" if self.base_url else "/openapi.json"

        return [
            path("openapi.json", openapi_view, name="openapi-json"),
            path("docs", docs_view, name="swagger-ui"),
        ]
