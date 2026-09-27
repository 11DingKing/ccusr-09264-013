"""材料谱系查询：档案员从成品（评审包）反查原始材料与中间处理。

工作方式：
- 谱系索引在 Python 中逐环遍历（成品 → 清单条目 → 版本链 → 原始材料，
  以及复审成品 → 前序成品），不依赖 SQL 递归；SQLite 只负责持久化
  断链标记（lineage_breaks 表）；
- 反查途中某环查无实体，即在该处放断链占位节点并落库一条断链标记，
  同一成品内同一缺口重复反查只标记一次（幂等）；
- 反查除断链标记与审计外不写入任何业务数据——绝不凭空补造缺失的
  材料或版本。
"""
from __future__ import annotations

from ..domain.enums import Role
from ..domain.errors import NotFoundError, PermissionDeniedError
from ..domain.genealogy import (
    EdgeRelation,
    Lineage,
    NodeKind,
    REASON_CYCLE,
    REASON_MISSING_MATERIAL,
    REASON_MISSING_PACKAGE,
    REASON_MISSING_VERSION,
    break_node_id,
)
from ..domain.models import (
    BrokenLinkMark,
    GenealogyNode,
    Material,
    MaterialVersion,
    ReviewPackage,
    User,
)
from .base import Service, require_user


class GenealogyService(Service):
    """材料谱系查询用例。"""

    # 跨机构只读角色：档案员 / 审计 / 质量权威机构
    GLOBAL_READERS = (Role.ARCHIVIST, Role.AUDITOR, Role.QUALITY_AUTHORITY)

    # ---------------------------------------------------------- 谱系反查
    def trace_package(self, actor: User, *, package_id: str) -> dict:
        """从成品反查材料谱系；缺失环节标记断链并落库，不补造。"""
        require_user(actor)
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError(
                "评审包不存在", details={"package_id": package_id}
            )
        self._check_read_permission(actor, package)

        lineage = Lineage(root_package_id=package.package_id)
        with self.repo.transaction():
            self._walk_package(actor, package, lineage, visited=set())
            newly_marked = 0
            for mark in lineage.broken_links:
                if self.repo.mark_broken_link(mark):
                    newly_marked += 1
            self.audit(
                actor.user_id, "lineage.traced",
                package_id=package.package_id,
                institution_id=package.institution_id,
                detail={
                    "node_count": len(lineage.nodes),
                    "broken_links": len(lineage.broken_links),
                    "newly_marked": newly_marked,
                },
            )

        result = lineage.to_dict()
        result["traced_by"] = actor.user_id
        result["traced_at"] = self.clock.now_iso()
        result["newly_marked"] = newly_marked
        return result

    def list_broken_links(self, actor: User, *, package_id: str) -> list[dict]:
        """某成品已登记的全部断链标记（档案员据此跟进补证）。"""
        require_user(actor)
        package = self.repo.get_package(package_id)
        if package is None:
            raise NotFoundError(
                "评审包不存在", details={"package_id": package_id}
            )
        self._check_read_permission(actor, package)
        return [
            {
                "break_id": m.break_id,
                "package_id": m.package_id,
                "node_id": m.node_id,
                "missing_ref": m.missing_ref,
                "expected_kind": m.expected_kind,
                "reason": m.reason,
                "marked_by": m.marked_by,
                "marked_at": m.marked_at,
                "detail": m.detail,
            }
            for m in self.repo.list_broken_links(package.package_id)
        ]

    # ---------------------------------------------------------- 权限
    def _check_read_permission(self, actor: User, package: ReviewPackage) -> None:
        if any(actor.has_role(r) for r in self.GLOBAL_READERS):
            return
        if (
            actor.has_role(Role.INSTITUTION_ADMIN)
            and actor.institution_id is not None
            and actor.institution_id == package.institution_id
        ):
            return
        raise PermissionDeniedError("无权反查该评审包的材料谱系")

    # ---------------------------------------------------------- 遍历（Python 维护谱系索引）
    def _walk_package(
        self,
        actor: User,
        package: ReviewPackage,
        lineage: Lineage,
        *,
        visited: set[str],
    ) -> None:
        if package.package_id in visited:
            self._register_break(
                actor, lineage,
                missing_ref=package.package_id,
                expected_kind=NodeKind.PACKAGE.value,
                reason=REASON_CYCLE,
                detail={"note": "复审链成环，停止向上追溯"},
            )
            return
        visited = visited | {package.package_id}

        lineage.add_node(
            GenealogyNode(
                node_id=package.package_id,
                kind=NodeKind.PACKAGE.value,
                label=package.title,
                detail={
                    "status": package.status,
                    "institution_id": package.institution_id,
                    "sealed_at": package.sealed_at,
                    "decision": package.decision,
                },
            )
        )

        # 成品收录的版本（清单条目即“成品由哪些中间处理产物构成”）
        for entry in package.entries:
            version = self.repo.get_version(entry.version_id)
            if version is None:
                placeholder = self._register_break(
                    actor, lineage,
                    missing_ref=entry.version_id,
                    expected_kind=NodeKind.VERSION.value,
                    reason=REASON_MISSING_VERSION,
                    detail={"entry_id": entry.entry_id},
                )
                lineage.add_edge(
                    package.package_id, placeholder.node_id,
                    EdgeRelation.PACKAGE_ENTRY,
                )
                continue
            self._walk_version(actor, version, lineage, visited_versions=set())
            lineage.add_edge(
                package.package_id, version.version_id, EdgeRelation.PACKAGE_ENTRY
            )

        # 复审成品承接前序成品
        if package.supersedes_package_id:
            predecessor = self.repo.get_package(package.supersedes_package_id)
            if predecessor is None:
                placeholder = self._register_break(
                    actor, lineage,
                    missing_ref=package.supersedes_package_id,
                    expected_kind=NodeKind.PACKAGE.value,
                    reason=REASON_MISSING_PACKAGE,
                    detail={"note": "前序评审包缺失"},
                )
                lineage.add_edge(
                    package.package_id, placeholder.node_id,
                    EdgeRelation.PACKAGE_SUPERSEDES,
                )
            else:
                self._walk_package(actor, predecessor, lineage, visited=visited)
                lineage.add_edge(
                    package.package_id, predecessor.package_id,
                    EdgeRelation.PACKAGE_SUPERSEDES,
                )

    def _walk_version(
        self,
        actor: User,
        version: MaterialVersion,
        lineage: Lineage,
        *,
        visited_versions: set[str],
    ) -> None:
        if version.version_id in visited_versions:
            self._register_break(
                actor, lineage,
                missing_ref=version.version_id,
                expected_kind=NodeKind.VERSION.value,
                reason=REASON_CYCLE,
                detail={"note": "版本链成环，停止向上追溯"},
            )
            return
        visited_versions = visited_versions | {version.version_id}

        lineage.add_node(
            GenealogyNode(
                node_id=version.version_id,
                kind=NodeKind.VERSION.value,
                label=f"v{version.version_no}",
                detail={
                    "material_id": version.material_id,
                    "version_no": version.version_no,
                    "sha256": version.sha256,
                    "withdrawn": version.withdrawn,
                    "is_origin": version.supersedes_version_id is None,
                    "created_by": version.created_by,
                    "created_at": version.created_at,
                },
            )
        )

        # 版本归属的原始材料
        material = self.repo.get_material(version.material_id)
        if material is None:
            placeholder = self._register_break(
                actor, lineage,
                missing_ref=version.material_id,
                expected_kind=NodeKind.MATERIAL.value,
                reason=REASON_MISSING_MATERIAL,
                detail={"version_id": version.version_id},
            )
            lineage.add_edge(
                version.version_id, placeholder.node_id,
                EdgeRelation.VERSION_MATERIAL,
            )
        else:
            self._add_material_node(lineage, material)
            lineage.add_edge(
                version.version_id, material.material_id,
                EdgeRelation.VERSION_MATERIAL,
            )

        # 中间处理链：本版本承接的前一版本
        if version.supersedes_version_id:
            previous = self.repo.get_version(version.supersedes_version_id)
            if previous is None:
                placeholder = self._register_break(
                    actor, lineage,
                    missing_ref=version.supersedes_version_id,
                    expected_kind=NodeKind.VERSION.value,
                    reason=REASON_MISSING_VERSION,
                    detail={"version_id": version.version_id},
                )
                lineage.add_edge(
                    version.version_id, placeholder.node_id,
                    EdgeRelation.VERSION_SUPERSEDES,
                )
            else:
                self._walk_version(
                    actor, previous, lineage,
                    visited_versions=visited_versions,
                )
                lineage.add_edge(
                    version.version_id, previous.version_id,
                    EdgeRelation.VERSION_SUPERSEDES,
                )

    @staticmethod
    def _add_material_node(lineage: Lineage, material: Material) -> None:
        lineage.add_node(
            GenealogyNode(
                node_id=material.material_id,
                kind=NodeKind.MATERIAL.value,
                label=material.title,
                detail={
                    "kind": material.kind,
                    "sensitivity": material.sensitivity,
                    "withdrawn": material.withdrawn,
                    "created_at": material.created_at,
                },
            )
        )

    # ---------------------------------------------------------- 断链登记
    def _register_break(
        self,
        actor: User,
        lineage: Lineage,
        *,
        missing_ref: str,
        expected_kind: str,
        reason: str,
        detail: dict | None = None,
    ):
        """在谱系中登记一处断链占位；只标记缺口，不补造任何实体。"""
        mark = BrokenLinkMark(
            break_id=self.ids.new_id("brk"),
            package_id=lineage.root_package_id,
            node_id=break_node_id(missing_ref),
            missing_ref=missing_ref,
            expected_kind=expected_kind,
            reason=reason,
            marked_by=actor.user_id,
            marked_at=self.clock.now_iso(),
            detail=detail or {},
        )
        return lineage.add_break(mark)
