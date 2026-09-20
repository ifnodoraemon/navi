"""Memory regression eval gate (LoCoMo-style recall hit-rate tripwire).

Runs a fixed fixture corpus through the sync recall pipeline twice (defaults
vs candidate parameter value) in a throwaway home and reports whether the
candidate degrades retrieval quality. Self-play uses this to gate
memory-parameter promotions.

Born from the 2026-09-17 parameter-plane contamination incident: a drifted
``tf_max_extra_boost=9,643,190`` silently saturated lexical activation and
wrecked recall ordering for days. The trap fixtures below reproduce that
mechanism: distractor items with high token repetition but only partial query
overlap stay below the expected item under default parameters, yet saturate
past it when ``tf_max_extra_boost`` explodes.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .dynamic_parameters import DynamicParameterRegistry, SYSTEM_DYNAMIC_PARAMETERS
from .memory.store import MemoryStore

_GATE_EXCLUDED_PARAMETERS = frozenset({"memory_regression_gate_threshold"})

_MEMORY_PARAM_PREFIXES = (
    "cue_",
    "tf_",
    "decay_",
    "ltp_",
    "recall_",
    "memory_",
    "consolidation_",
    "graph_",
    "context_",
)

_MEMORY_PARAM_EXACT = frozenset(
    {
        "temporal_discount_factor",
        "confidence_reduction_delta",
        "hebbian_learning_rate",
        "credit_assignment_learning_rate",
        "planner_memory_item_max_chars",
    }
)

#: Every dynamic parameter that materially feeds the memory/recall pipeline.
#: Derived from SYSTEM_DYNAMIC_PARAMETERS so future recall tunables are
#: gated automatically; the gate is a no-op for pipeline params the fixture
#: cannot observe, which is harmless.
MEMORY_PARAMETER_SET: frozenset[str] = frozenset(
    name
    for name in SYSTEM_DYNAMIC_PARAMETERS
    if (name.startswith(_MEMORY_PARAM_PREFIXES) or name in _MEMORY_PARAM_EXACT)
    and name not in _GATE_EXCLUDED_PARAMETERS
)

_BENIGN_FIXTURES: tuple[tuple[str, str, str], ...] = (
    (
        "fact",
        "用户偏好深色主题与等宽字体,夜间工作为主",
        "深色主题 偏好",
    ),
    (
        "preference",
        "用户在学 Kubernetes 基础概念,偏好通俗讲解加中英术语对照",
        "k8s 讲解 术语 对照",
    ),
    (
        "fact",
        "用户的服务器集群 sudo 密码统一由管理员轮换,不在记忆中存储明文",
        "服务器 密码 轮换",
    ),
    (
        "case",
        "部署 navi 服务时先跑 uv 重建 venv 再用 systemctl --user 重启,顺序错了会残留旧进程",
        "navi 部署 顺序",
    ),
    (
        "preference",
        "用户的简历优化目标是 Agent Infra 岗位,需要突出推理服务与 K8s 经验",
        "简历 目标 岗位",
    ),
    (
        "fact",
        "weixin 出站投递失败时先查 delivery_outbox 的 ret 码再判断是否 iLink 限流",
        "weixin 投递 失败",
    ),
)

_TRAP_FIXTURES: tuple[dict[str, str], ...] = (
    {
        "query": "k8s 学习进度 下一步",
        "expected": (
            "用户的 Kubernetes 学习进度:已完成 node、namespace、pod、deployment、"
            "service、ingress 与 kubectl context,下一步计划学习存储卷、"
            "configmap、secret 和 helm 包管理"
        ),
        "distractor_term": "k8s",
    },
    {
        "query": "gpu 推理 部署",
        "expected": (
            "GPU 推理部署经验:mini-vllm 与 mini-sglang 源码部署,显存预留按"
            "桌面图形占用 2.1G 扣减,batch 与 kv cache 参数需联调"
        ),
        "distractor_term": "gpu",
    },
    {
        "query": "简历 修改 复用",
        "expected": (
            "简历优化任务的执行经验:先向用户索要原简历与 JD,再逐条改写项目"
            "经历,未拿到简历前只产出渠道建议,不凭空编造内容"
        ),
        "distractor_term": "resume",
    },
    {
        "query": "记忆 参数 污染",
        "expected": (
            "记忆参数污染事故记录:self_play_promoted_ema 曾把 100 个参数推成"
            "荒谬值,修复后需要回归评测门禁才能晋升记忆参数"
        ),
        "distractor_term": "memory",
    },
)

_TRAP_DISTRACTOR_COUNT = 6


def _distractor_content(term: str, index: int) -> str:
    return " ".join([term] * 60) + f" 旧笔记归档编号{index},与当前查询无关。"


def _seed_fixture(store: MemoryStore) -> list[tuple[str, str]]:
    queries: list[tuple[str, str]] = []
    for memory_type, content, query in _BENIGN_FIXTURES:
        item = store.add_item(
            memory_type=memory_type,
            content=content,
            source="memory_gate_fixture",
            status="active",
            reason="memory regression gate fixture",
            provenance="memory_eval:fixture",
        )
        queries.append((query, item.id))
    for trap in _TRAP_FIXTURES:
        expected = store.add_item(
            memory_type="fact",
            content=trap["expected"],
            source="memory_gate_fixture",
            status="active",
            reason="memory regression gate fixture",
            provenance="memory_eval:fixture",
        )
        for index in range(_TRAP_DISTRACTOR_COUNT):
            store.add_item(
                memory_type="fact",
                content=_distractor_content(trap["distractor_term"], index),
                source="memory_gate_fixture",
                status="active",
                reason="memory regression gate fixture",
                provenance="memory_eval:fixture",
            )
        queries.append((trap["query"], expected.id))
    return queries


def _hit_queries(store: MemoryStore, queries: list[tuple[str, str]], limit: int) -> set[str]:
    hits: set[str] = set()
    for query, expected_id in queries:
        recalls = store.recall(query, limit=limit)
        if any(recall.item.id == expected_id for recall in recalls):
            hits.add(query)
    return hits


def run_memory_regression_eval(
    home: Path,
    *,
    target_id: str,
    candidate_value: float,
    threshold: float = 0.85,
    limit: int = 5,
) -> dict[str, Any]:
    """Evaluate a candidate memory parameter against a fixed recall fixture.

    The gate fails closed in spirit but is hermetic by construction: the
    fixture corpus lives in a throwaway home, the production memory plane is
    never touched. ``home`` is accepted for interface symmetry and future
    fixture overrides; it is not read.
    """
    del home
    with tempfile.TemporaryDirectory(prefix="navi-memory-gate-") as tmp_dir:
        gate_home = Path(tmp_dir)
        baseline_registry = DynamicParameterRegistry(gate_home)
        baseline_store = MemoryStore(gate_home, registry=baseline_registry)
        queries = _seed_fixture(baseline_store)
        baseline_hits = _hit_queries(baseline_store, queries, limit)

        candidate_registry = DynamicParameterRegistry(gate_home)
        candidate_registry.set(
            target_id,
            float(candidate_value),
            reason="memory_regression_gate_candidate",
        )
        candidate_store = MemoryStore(gate_home, registry=candidate_registry)
        candidate_hits = _hit_queries(candidate_store, queries, limit)

    total = len(queries)
    hit_rate = len(candidate_hits) / total
    baseline_rate = len(baseline_hits) / total
    floor = max(float(threshold), baseline_rate - 0.05)
    failed_queries = [query for query, _expected_id in queries if query not in candidate_hits]
    return {
        "policy": "memory_regression_gate_v1",
        "target_id": target_id,
        "candidate_value": float(candidate_value),
        "hit_rate": round(hit_rate, 4),
        "baseline_hit_rate": round(baseline_rate, 4),
        "threshold": round(floor, 4),
        "hits": f"{len(candidate_hits)}/{total}",
        "failed_queries": failed_queries,
        "blocked": bool(hit_rate < floor),
    }
