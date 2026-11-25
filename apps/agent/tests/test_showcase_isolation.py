"""Production boundaries and request compatibility for optional teaching code."""

import ast
import subprocess
import zipfile
from pathlib import Path

import pytest
from compass_agent.models.errors import ModelError
from compass_agent.runtime.history import decode_command, same_request
from compass_agent.schemas import SessionSubmit
from compass_agent.tools.catalog import TOOLS
from pydantic import ValidationError

from .test_agent_run import run_command

ROOT = Path(__file__).parents[1] / "src/compass_agent"


BUSINESS_TOOLS = {
    "read_document",
    "search_documents",
    "search_semantics",
    "get_metric_definition",
    "resolve_semantics",
    "query_metric",
    "discover_semantic_candidates",
    "review_semantic_change",
    "submit_semantic_change",
}


def test_production_registry_contains_only_shipped_tools():
    assert {tool.name for tool in TOOLS if tool.category == "builtin"} == {
        "todo_write",
        "final_answer",
        "database_query",
        "read_tool_result",
        "python_analysis",
        "create_workflow",
        "sub_agent",
        "goal",
        "schedule_read",
        "schedule_write",
    }
    assert {tool.name for tool in TOOLS if tool.category == "business"} == BUSINESS_TOOLS
    for folder in (ROOT / "runtime", ROOT / "harness"):
        for path in folder.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    assert not (node.module or "").startswith(
                        (
                            "examples.",
                            "compass_agent.tools.business",
                            "compass_agent.workflows.service",
                        )
                    ), path
    assert not (ROOT / "routing/workflows.py").exists()


@pytest.mark.parametrize(
    "field",
    [
        {"showcase": "weekly_review"},
        {"constraints": {"verification": {"rules": ["require_product_evidence"]}}},
    ],
)
def test_new_requests_reject_retired_case_fields(field):
    value = run_command().model_dump(mode="json", by_alias=True)
    with pytest.raises(ValidationError):
        SessionSubmit.model_validate({**value, **field})


def test_historical_defaults_preserve_idempotency_without_new_execution():
    current = {"requestId": "stable", "question": "分析数据"}
    assert same_request({**current, "showcase": None}, current)
    assert same_request(
        {**current, "constraints": {"maxSteps": 2, "verification": None}},
        {**current, "constraints": {"maxSteps": 2}},
    )
    assert not same_request({**current, "showcase": "weekly_review"}, current)
    assert not same_request({**current, "showcase": None}, {**current, "question": "different"})
    assert decode_command(
        {**current, "showcase": None, "constraints": {"maxSteps": 2, "verification": {"rules": []}}}
    )["constraints"] == {"maxSteps": 2}
    with pytest.raises(ModelError, match="workflow_unavailable"):
        decode_command({**current, "showcase": "weekly_review"})


def test_production_wheel_excludes_showcases(tmp_path):
    workspace = ROOT.parents[3]
    subprocess.run(
        [
            "uv",
            "build",
            "--offline",
            "--package",
            "compass-agent",
            "--wheel",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    with zipfile.ZipFile(next(tmp_path.glob("*.whl"))) as wheel:
        names = wheel.namelist()
    assert not any(
        "examples/" in name
        or (
            "/tools/business/" in name
            and len(name.split("/")) > 4
            and name.split("/")[3] not in BUSINESS_TOOLS
        )
        or "weekly-retail-review" in name
        for name in names
    )
    tools = {
        name.split("/")[3]
        for name in names
        if name.startswith("compass_agent/tools/builtin/") and len(name.split("/")) > 4
    }
    assert tools == {
        "todo_write",
        "final_answer",
        "database_query",
        "read_tool_result",
        "python_analysis",
        "create_workflow",
        "sub_agent",
        "goal",
        "schedule_read",
        "schedule_write",
    }
