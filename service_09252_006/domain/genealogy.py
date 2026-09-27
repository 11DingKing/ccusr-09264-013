"""材料谱系：从成品反查原始材料与中间处理的纯领域规则。

本模块只负责“谱系索引”的组装规则（节点/边/断链的判定），不接触
数据库；遍历所需的记录由应用服务逐环取出后交给这里组装。

核心规则：**缺失一环即断链**。反查途中某条引用查无实体（版本、
原始材料、前序成品），谱系中放一个 kind=BREAK 的占位节点并记录
断链标记，绝不凭空补造一份材料让链条“看起来完整”。
"""
from __future__ import annotations

from enum import Enum

from .models import BrokenLinkMark, GenealogyEdge, GenealogyNode


class NodeKind(str, Enum):
    PACKAGE = "package"    # 成品（评审包）
    VERSION = "version"    # 中间处理（材料的一次版本/加工）
    MATERIAL = "material"  # 原始材料
    BREAK = "break"        # 断链占位：缺失的一环


class EdgeRelation(str, Enum):
    PACKAGE_ENTRY = "package_entry"          # 成品收录了某版本（清单条目）
    VERSION_SUPERSEDES = "version_supersedes"  # 版本承接前一版本（中间处理链）
    VERSION_MATERIAL = "version_material"    # 版本归属原始材料
    PACKAGE_SUPERSEDES = "package_supersedes"  # 复审成品承接前序成品


# 断链原因（写入 BrokenLinkMark.reason，供检索与统计）
REASON_MISSING_VERSION = "missing_version"    # 清单条目/版本链指向的版本不存在
REASON_MISSING_MATERIAL = "missing_material"  # 版本归属的原始材料不存在
REASON_MISSING_PACKAGE = "missing_package"    # 复审链指向的前序成品不存在
REASON_CYCLE = "lineage_cycle"                # 谱系成环（数据异常，停止上溯）

# 断链占位节点 id 前缀；占位 id 由缺失引用派生，同一缺口多次反查得到同一 id
BREAK_NODE_PREFIX = "break"


def break_node_id(missing_ref: str) -> str:
    """断链占位节点 id：由缺失引用确定性地派生（可幂等重建）。"""
    return f"{BREAK_NODE_PREFIX}:{missing_ref}"


class Lineage:
    """一份材料谱系：节点索引 + 有向边 + 断链标记。

    节点按 id 去重（同一版本被多个成品收录时只出现一次）；边允许重复
    关系不同。complete 为 False 时 broken_links 给出全部缺口。
    """

    def __init__(self, root_package_id: str) -> None:
        self.root_package_id = root_package_id
        self.nodes: dict[str, GenealogyNode] = {}
        self.edges: list[GenealogyEdge] = []
        self.broken_links: list[BrokenLinkMark] = []

    @property
    def complete(self) -> bool:
        return not self.broken_links

    def add_node(self, node: GenealogyNode) -> GenealogyNode:
        return self.nodes.setdefault(node.node_id, node)

    def add_edge(self, from_id: str, to_id: str, relation: EdgeRelation) -> None:
        edge = GenealogyEdge(from_id=from_id, to_id=to_id, relation=relation.value)
        if edge not in self.edges:
            self.edges.append(edge)

    def add_break(self, mark: BrokenLinkMark) -> GenealogyNode:
        """登记一处断链：放占位节点 + 记录标记。返回占位节点。

        同一缺口（node_id 相同）在一次反查中只记一条标记——两处条目
        引用同一个缺失版本时，谱系里仍只有一个断链。
        """
        placeholder = GenealogyNode(
            node_id=mark.node_id,
            kind=NodeKind.BREAK.value,
            label=f"断链（{mark.reason}）",
            detail={
                "missing_ref": mark.missing_ref,
                "expected_kind": mark.expected_kind,
                "reason": mark.reason,
            },
        )
        self.add_node(placeholder)
        if all(b.node_id != mark.node_id for b in self.broken_links):
            self.broken_links.append(mark)
        return placeholder

    def to_dict(self) -> dict:
        return {
            "root_package_id": self.root_package_id,
            "complete": self.complete,
            "nodes": [
                {
                    "node_id": n.node_id,
                    "kind": n.kind,
                    "label": n.label,
                    "detail": n.detail,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {"from_id": e.from_id, "to_id": e.to_id, "relation": e.relation}
                for e in self.edges
            ],
            "broken_links": [
                {
                    "node_id": b.node_id,
                    "missing_ref": b.missing_ref,
                    "expected_kind": b.expected_kind,
                    "reason": b.reason,
                    "detail": b.detail,
                }
                for b in self.broken_links
            ],
        }
