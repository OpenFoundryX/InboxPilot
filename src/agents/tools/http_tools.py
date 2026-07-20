"""Tools that let agents make outbound HTTP calls."""

from agents.tools.base import Tool


class HttpGetTool(Tool):
    name = "http_get"
    description = "Fetch a URL and return the response body."

    async def run(self, **kwargs) -> object:
        # TODO: implement with httpx.AsyncClient and allow-listing.
        raise NotImplementedError
