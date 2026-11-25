"""Production services with only the explicitly named fixture credential rerouted."""

import httpx

from . import context_e2e_app as host


class SelectiveModelTransport(host.LocalModelTransport):
    async def handle_async_request(self, request):
        if request.headers.get("authorization") == "Bearer isolated-fixture":
            return await super().handle_async_request(request)
        return await httpx.AsyncHTTPTransport.handle_async_request(self, request)


host.LocalModelTransport = SelectiveModelTransport
app = host.app
