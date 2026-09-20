"""读取并检查共享领域资料。"""

import json
from pathlib import Path

REQUIRED = {"domain", "version", "sample_id", "record_types", "workflow_states", "facts", "sample"}

def load_domain(path: Path) -> dict:
    """返回字段完整且具有流程状态的领域资料。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not REQUIRED.issubset(value):
        raise ValueError("领域资料缺少必要字段")
    if value["version"] < 1 or len(value["record_types"]) < 3 or len(value["workflow_states"]) < 3:
        raise ValueError("领域资料内容不完整")
    return value
