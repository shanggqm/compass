"""Goal failures are isolated from worker readiness and owner health."""

import asyncio
import logging

from compass_agent.goal.contracts import GOAL_LOOP_INTERVAL
from compass_agent.goal.controller import GoalController

logger = logging.getLogger(__name__)


async def goal_loop(runtime):
    controller = GoalController(runtime.store)
    while not runtime.closing:
        try:
            if runtime.owner_healthy and runtime.store.ready:
                await asyncio.to_thread(controller.tick)
        except Exception:
            logger.exception("goal.tick_failed")
        await asyncio.sleep(GOAL_LOOP_INTERVAL)
