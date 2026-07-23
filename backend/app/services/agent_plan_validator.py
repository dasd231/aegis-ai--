from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlanValidation:
    valid: bool
    errors: list[str]
    execution_layers: list[list[str]]


def _normalized_write(service: str, value: str) -> tuple[str, str]:
    path = value.replace("\\", "/").strip().removesuffix("/**").rstrip("/")
    return service, path


def _writes_conflict(left: tuple[str, str], right: tuple[str, str]) -> bool:
    if left[0] != right[0]:
        return False
    left_path, right_path = left[1], right[1]
    return left_path == right_path or left_path.startswith(right_path + "/") or right_path.startswith(left_path + "/")


def validate_agent_plan(plan: dict[str, Any], service_cards: list[dict[str, Any]]) -> PlanValidation:
    """Compile an LLM-proposed change plan into deterministic safety checks.

    The LLM may propose nodes, but it cannot waive evidence, invent services,
    introduce dependency cycles, or parallelize overlapping write sets.
    """
    errors: list[str] = []
    cards = {str(card.get("name", "")): card for card in service_cards if card.get("name")}
    nodes_list = plan.get("nodes")
    if not isinstance(nodes_list, list) or not nodes_list:
        return PlanValidation(False, ["plan.nodes 必须是非空数组"], [])

    nodes: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(nodes_list):
        if not isinstance(node, dict):
            errors.append(f"nodes[{index}] 必须是对象")
            continue
        node_id = str(node.get("id", "")).strip()
        if not node_id:
            errors.append(f"nodes[{index}] 缺少 id")
            continue
        if node_id in nodes:
            errors.append(f"节点 id 重复: {node_id}")
            continue
        nodes[node_id] = node

    for node_id, node in nodes.items():
        service = str(node.get("service", ""))
        if service not in cards:
            errors.append(f"{node_id}: 未知服务 {service or '[empty]'}")
        evidence = node.get("evidence")
        if not isinstance(evidence, list) or not any(str(item).strip() for item in evidence):
            errors.append(f"{node_id}: 缺少可追溯 evidence")
        tests = node.get("tests")
        if node.get("risk") in {"high", "critical"} and not tests:
            errors.append(f"{node_id}: 高风险节点必须声明 tests")
        if node.get("risk") in {"high", "critical"} and not node.get("rollback"):
            errors.append(f"{node_id}: 高风险节点必须声明 rollback")
        for dependency in node.get("depends_on", []):
            if dependency not in nodes:
                errors.append(f"{node_id}: 依赖不存在的节点 {dependency}")
            elif dependency == node_id:
                errors.append(f"{node_id}: 不能依赖自身")
            elif int(node.get("stage", 0)) <= int(nodes[dependency].get("stage", 0)):
                errors.append(f"{node_id}: stage 必须晚于依赖 {dependency}")

    same_stage: dict[int, list[tuple[str, tuple[str, str]]]] = defaultdict(list)
    for node_id, node in nodes.items():
        service = str(node.get("service", ""))
        for path in node.get("files", []):
            same_stage[int(node.get("stage", 0))].append((node_id, _normalized_write(service, str(path))))
    for stage, writes in same_stage.items():
        for index, (left_id, left_write) in enumerate(writes):
            for right_id, right_write in writes[index + 1 :]:
                if left_id != right_id and _writes_conflict(left_write, right_write):
                    errors.append(
                        f"stage {stage}: {left_id} 与 {right_id} 的 write-set 冲突 ({left_write[1]})"
                    )

    indegree = {node_id: 0 for node_id in nodes}
    children: dict[str, list[str]] = defaultdict(list)
    for node_id, node in nodes.items():
        for dependency in node.get("depends_on", []):
            if dependency in nodes and dependency != node_id:
                indegree[node_id] += 1
                children[dependency].append(node_id)
    frontier = deque(sorted(node_id for node_id, count in indegree.items() if count == 0))
    layers: list[list[str]] = []
    visited = 0
    while frontier:
        layer = list(frontier)
        frontier.clear()
        layers.append(layer)
        for node_id in layer:
            visited += 1
            for child in children[node_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    frontier.append(child)
        frontier = deque(sorted(frontier))
    if visited != len(nodes):
        errors.append("依赖图存在环，禁止执行")

    return PlanValidation(not errors, errors, layers if visited == len(nodes) else [])
