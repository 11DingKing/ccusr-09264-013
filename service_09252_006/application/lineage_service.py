"""材料谱系查询：从成品反查原始材料与中间处理。

关键规则：
- 谱系索引由 Python 维护：反查与成环检查都在应用层沿
  `list_lineage_edges_by_child` 逐环行走，不依赖递归 SQL；
- 节点（原始材料/中间产物/成品）只能经登记用例显式创建；
  来源边的上游允许指向尚未登记的节点——这是断链的唯一来源；
- 反查发现缺失一环时，结果中放置断链标记并把断点追加写入
  SQLite（lineage_breaks，同一断点只记一次），绝不凭空补造节点；
- 缺失节点事后只能由人显式登记补齐；既有断链标记保留为历史，
  查询侧以 resolved 注解呈现。
"""
from __future__ import annotations

from ..domain.enums import LineageNodeKind, Role
from ..domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ..domain.models import LineageBreak, LineageEdge, LineageNode, User
from .base import Service, require_roles

# 可跨机构反查/查看断链的角色；机构管理员限本机构节点
_TRACE_ANYWHERE = (Role.ARCHIVIST, Role.AUDITOR, Role.QUALITY_AUTHORITY)


class LineageService(Service):
    # ---------------------------------------------------------- 节点登记
    def register_node(
        self,
        actor: User,
        *,
        kind: str,
        name: str,
        detail: dict | None = None,
        node_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.INSTITUTION_SUBMITTER)
        kinds = {k.value for k in LineageNodeKind}
        if kind not in kinds:
            raise ValidationError("未知谱系节点类型", details={"kind": kind})
        if not name.strip():
            raise ValidationError("节点名称不能为空")

        def work() -> dict:
            nid = node_id or self.ids.new_id("node")
            existing = self.repo.get_lineage_node(nid)
            if existing is not None:
                # 客户端指定 id 的重复提交：回放，不报错
                return self._node_dict(existing, replayed=True)
            node = LineageNode(
                node_id=nid,
                institution_id=actor.institution_id or "",
                kind=kind,
                name=name.strip(),
                detail=detail or {},
                created_by=actor.user_id,
                created_at=self.clock.now_iso(),
            )
            self.repo.insert_lineage_node(node)
            self.audit(
                actor.user_id, "lineage.node_registered",
                institution_id=node.institution_id,
                detail={"node_id": nid, "kind": kind},
            )
            return self._node_dict(node)

        return self.idempotent(idempotency_key, work)

    # ---------------------------------------------------------- 来源登记
    def register_edge(
        self,
        actor: User,
        *,
        child_id: str,
        parent_id: str,
        process: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """登记一条来源关系：child 经 process 由 parent 得到。

        parent 允许未登记（断链来源，反查时标记）；绝不在此补造。
        """
        require_roles(actor, Role.INSTITUTION_ADMIN, Role.INSTITUTION_SUBMITTER)
        if not process.strip():
            raise ValidationError("必须说明中间处理环节")
        if child_id == parent_id:
            raise ValidationError("节点不能以自己为来源")

        def work() -> dict:
            child = self.repo.get_lineage_node(child_id)
            if child is None:
                raise NotFoundError(
                    "下游节点未登记", details={"node_id": child_id}
                )
            if child.institution_id != actor.institution_id:
                raise PermissionDeniedError("只能为本机构节点登记来源")
            if self._reachable_upstream(parent_id, child_id):
                raise ConflictError(
                    "该来源关系会形成谱系环",
                    details={"child_id": child_id, "parent_id": parent_id},
                )
            edge = LineageEdge(
                edge_id=self.ids.new_id("edge"),
                child_id=child_id,
                parent_id=parent_id,
                process=process.strip(),
                recorded_by=actor.user_id,
                recorded_at=self.clock.now_iso(),
            )
            self.repo.insert_lineage_edge(edge)
            self.audit(
                actor.user_id, "lineage.edge_registered",
                institution_id=child.institution_id,
                detail={
                    "edge_id": edge.edge_id,
                    "child_id": child_id,
                    "parent_id": parent_id,
                    "process": edge.process,
                    "parent_registered": self.repo.get_lineage_node(parent_id)
                    is not None,
                },
            )
            return self._edge_dict(edge)

        return self.idempotent(idempotency_key, work)

    # ---------------------------------------------------------- 成品反查
    def trace_origins(
        self,
        actor: User,
        *,
        product_id: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """档案员从成品反查来源：返回完整谱系树与断链清单。

        缺失一环时在树中放置断链标记并把断点写入 SQLite；
        本用例只读节点/边、只追加断链标记，绝不创建谱系节点。
        """
        require_roles(actor, Role.INSTITUTION_ADMIN, *_TRACE_ANYWHERE)

        def work() -> dict:
            root = self.repo.get_lineage_node(product_id)
            if root is None:
                # 成品本身未登记：如实报告缺失，不补造
                raise NotFoundError(
                    "成品未登记，无法反查", details={"node_id": product_id}
                )
            self._check_trace_permission(actor, root)
            breaks: list[dict] = []
            tree = self._trace_node(root, actor, path=(), breaks=breaks)
            result = {
                "product_id": product_id,
                "complete": not breaks,
                "breaks": breaks,
                "tree": tree,
            }
            self.audit(
                actor.user_id, "lineage.traced",
                institution_id=root.institution_id,
                detail={
                    "product_id": product_id,
                    "complete": not breaks,
                    "break_count": len(breaks),
                },
            )
            return result

        return self.idempotent(idempotency_key, work)

    def list_breaks(self, actor: User, *, node_id: str | None = None) -> list[dict]:
        """查看已标记的断链；resolved 表示缺失环节事后已被显式登记。"""
        require_roles(actor, Role.INSTITUTION_ADMIN, *_TRACE_ANYWHERE)
        rows = self.repo.list_lineage_breaks(node_id)
        return [self._break_dict(actor, b) for b in rows]

    # ---------------------------------------------------------- 内部
    def _trace_node(
        self,
        node: LineageNode,
        actor: User,
        *,
        path: tuple[str, ...],
        breaks: list[dict],
    ) -> dict:
        """沿来源边向上走（Python 侧索引）；缺失上游标记断链。"""
        entry = {
            "node_id": node.node_id,
            "institution_id": node.institution_id,
            "kind": node.kind,
            "name": node.name,
            "status": "registered",
        }
        sources = []
        for edge in self.repo.list_lineage_edges_by_child(node.node_id):
            parent = self.repo.get_lineage_node(edge.parent_id)
            if parent is None:
                # 缺失一环：标记断链并落库，绝不凭空补造节点
                brk = LineageBreak(
                    break_id=self.ids.new_id("break"),
                    edge_id=edge.edge_id,
                    child_id=node.node_id,
                    missing_node_id=edge.parent_id,
                    detected_by=actor.user_id,
                    detected_at=self.clock.now_iso(),
                )
                self.repo.record_lineage_break(brk)
                breaks.append(
                    {
                        "edge_id": edge.edge_id,
                        "child_id": node.node_id,
                        "missing_node_id": edge.parent_id,
                        "process": edge.process,
                    }
                )
                child_view = {
                    "node_id": edge.parent_id,
                    "kind": None,
                    "name": None,
                    "status": "broken",  # 断链标记，不是补造的节点
                }
            elif edge.parent_id in path:
                # 登记时已防环；此处防御库被绕过修改的情况
                child_view = {
                    "node_id": edge.parent_id,
                    "kind": parent.kind,
                    "name": parent.name,
                    "status": "cycle",
                }
            else:
                child_view = self._trace_node(
                    parent, actor, path=path + (node.node_id,), breaks=breaks
                )
            sources.append(
                {
                    "edge_id": edge.edge_id,
                    "process": edge.process,
                    "node": child_view,
                }
            )
        entry["sources"] = sources
        return entry

    def _reachable_upstream(self, start_id: str, target_id: str) -> bool:
        """从 start 沿来源边向上能否走到 target（成环检查，Python 侧）。"""
        stack = [start_id]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == target_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            for edge in self.repo.list_lineage_edges_by_child(current):
                stack.append(edge.parent_id)
        return False

    def _check_trace_permission(self, actor: User, root: LineageNode) -> None:
        if any(actor.has_role(r) for r in _TRACE_ANYWHERE):
            return
        if root.institution_id != actor.institution_id:
            raise PermissionDeniedError("只能反查本机构节点的谱系")

    def _break_dict(self, actor: User, brk: LineageBreak) -> dict:
        return {
            "break_id": brk.break_id,
            "edge_id": brk.edge_id,
            "child_id": brk.child_id,
            "missing_node_id": brk.missing_node_id,
            "detected_by": brk.detected_by,
            "detected_at": brk.detected_at,
            # 缺失环节事后被显式登记（人工补齐）即为已消解
            "resolved": self.repo.get_lineage_node(brk.missing_node_id)
            is not None,
        }

    # ---------------------------------------------------------- 视图
    @staticmethod
    def _node_dict(node: LineageNode, *, replayed: bool = False) -> dict:
        return {
            "node_id": node.node_id,
            "institution_id": node.institution_id,
            "kind": node.kind,
            "name": node.name,
            "detail": node.detail,
            "created_by": node.created_by,
            "created_at": node.created_at,
            "replayed": replayed,
        }

    @staticmethod
    def _edge_dict(edge: LineageEdge) -> dict:
        return {
            "edge_id": edge.edge_id,
            "child_id": edge.child_id,
            "parent_id": edge.parent_id,
            "process": edge.process,
            "recorded_by": edge.recorded_by,
            "recorded_at": edge.recorded_at,
        }
