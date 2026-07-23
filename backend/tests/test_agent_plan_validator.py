from __future__ import annotations

from app.services.agent_plan_validator import validate_agent_plan


CARDS = [
    {"name": "contracts", "source_document": "architecture.md#events"},
    {"name": "order", "source_document": "services.md#order"},
    {"name": "notification", "source_document": "services.md#notification"},
]


def test_valid_plan_produces_parallel_execution_layers():
    plan = {
        "nodes": [
            {"id": "contract", "service": "contracts", "stage": 0, "depends_on": [], "evidence": ["architecture.md#events"], "files": ["events/order.json"]},
            {"id": "producer", "service": "order", "stage": 1, "depends_on": ["contract"], "evidence": ["services.md#order"], "files": ["outbox/publisher.py"]},
            {"id": "consumer", "service": "notification", "stage": 1, "depends_on": ["contract"], "evidence": ["services.md#notification"], "files": ["consumers/order.py"]},
        ]
    }
    result = validate_agent_plan(plan, CARDS)
    assert result.valid is True
    assert result.execution_layers == [["contract"], ["consumer", "producer"]]


def test_plan_rejects_invented_service_cycle_and_parallel_write_collision():
    plan = {
        "nodes": [
            {"id": "a", "service": "order", "stage": 1, "depends_on": ["b"], "evidence": [], "files": ["src/**"]},
            {"id": "b", "service": "order", "stage": 1, "depends_on": ["a"], "evidence": ["x"], "files": ["src/api.py"]},
            {"id": "c", "service": "invented", "stage": 1, "depends_on": [], "evidence": ["x"], "files": []},
        ]
    }
    result = validate_agent_plan(plan, CARDS)
    assert result.valid is False
    assert any("未知服务" in error for error in result.errors)
    assert any("write-set 冲突" in error for error in result.errors)
    assert any("存在环" in error for error in result.errors)
