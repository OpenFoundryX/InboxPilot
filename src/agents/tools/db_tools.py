"""Tools that let agents query the database."""

from agents.tools.base import Tool


class DbQueryTool(Tool):
    name = "db_query"
    description = "Run a read-only query against the application database."

    async def run(self, **kwargs) -> object:
        # TODO: implement using a scoped, read-only session.
        raise NotImplementedError
