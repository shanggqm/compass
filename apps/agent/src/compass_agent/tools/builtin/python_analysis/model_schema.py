"""Model-facing contract; execution retains the full internal domain model."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "source_query_id": {
            "type": "string",
            "description": "上游查询回执结果 ID（数据 <=128KiB）。",
        },
        "code": {
            "type": "string",
            "description": (
                'Python：读 data={"columns":[],"rows":[[]]}，结果赋给 result；'
                "图存 chart_path 或相对路径 chart.png。output_dir 为本次目录，"
                "shared_dir 为同用户同租户工作区共享目录。并行任务勿覆盖共享同名文件。"
            ),
        },
    },
    "required": ["source_query_id", "code"],
}
