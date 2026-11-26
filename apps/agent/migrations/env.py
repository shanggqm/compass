from alembic import context
from compass_agent.context_ops import entities as context_ops_entities  # noqa: F401
from compass_agent.data_connections.entities import DataConnection  # noqa: F401
from compass_agent.database import Base
from compass_agent.models import entities  # noqa: F401
from compass_agent.promptops import entities as prompt_entities  # noqa: F401
from compass_agent.runtime import entities as runtime_entities  # noqa: F401
from compass_agent.scheduler import entities as schedule_entities  # noqa: F401
from compass_agent.semantic import entities as semantic_entities  # noqa: F401
from compass_agent.settings import get_settings
from compass_agent.tools.mcp.entities import McpConnection  # noqa: F401
from compass_agent.tools.sandbox.entities import SandboxAllocation  # noqa: F401
from compass_agent.workflows.assets import WorkflowAsset  # noqa: F401
from sqlalchemy import create_engine

config = context.config
if context.is_offline_mode():
    context.configure(
        url=config.get_main_option("sqlalchemy.url") or "postgresql+psycopg://",
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied = config.attributes.get("connection")
    if supplied is not None:
        context.configure(connection=supplied, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    else:
        url = config.get_main_option("sqlalchemy.url") or get_settings().database_url
        with create_engine(url).connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
