"""Tool registry: name -> (description, input schema, handler, write flag).

Handlers take ``(ctx, args)`` and return a JSON-able payload (or a string).
They raise ToolError for anything the caller should see as a failed tool call.
``write=True`` tools are refused for read-only callers before the handler runs.
"""
from dataclasses import dataclass
from typing import Callable

_TOOLS = {}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable
    write: bool = False

    def describe(self):
        return {'name': self.name, 'description': self.description,
                'inputSchema': self.input_schema}


def tool(name, description, input_schema=None, *, write=False):
    def decorator(fn):
        _TOOLS[name] = Tool(name, description,
                            input_schema or {'type': 'object', 'properties': {},
                                             'additionalProperties': False},
                            fn, write)
        return fn
    return decorator


def schema(required, props):
    return {'type': 'object', 'required': list(required), 'properties': props,
            'additionalProperties': False}


def get(name):
    _load()
    return _TOOLS.get(name)


def all_tools():
    _load()
    return list(_TOOLS.values())


def _load():
    # Importing the modules registers their tools.
    from app.services.mcp import (  # noqa: F401
        tools_general, tools_sites, tools_env, tools_data, tools_cron, tools_domains,
    )
