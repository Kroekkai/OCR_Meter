"""
Strips settings.base_path_prefix (e.g. "/iot") from the incoming request
path before routing, if present — mirrors FastAPI's root_path mechanism
but as runtime-configurable middleware instead of a startup flag. Also
sets scope["root_path"] so /docs and /openapi.json still generate correct
self-referencing links through the proxy.

Idempotent: if the incoming path doesn't start with the prefix (e.g. the
reverse proxy already stripped it before forwarding), this does nothing —
safe to leave the prefix configured either way.
"""


class StripPathPrefixMiddleware:
    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = prefix.rstrip("/") if prefix else ""

    async def __call__(self, scope, receive, send):
        if self.prefix and scope["type"] == "http":
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self.prefix):] or "/"
                scope["root_path"] = scope.get("root_path", "") + self.prefix
        await self.app(scope, receive, send)
