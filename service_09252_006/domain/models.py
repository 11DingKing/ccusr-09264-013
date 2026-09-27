"""领域实体（贫血数据载体，业务规则在领域服务/应用服务中）。

时间一律以带时区的 UTC ISO-8601 字符串存储；截止时间同时保存原始
IANA 时区用于展示，比较时统一换化为 UTC 时刻，从而正确处理跨时区截止。
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

from .enums import (
    Decision,
    MaterialKind,
    PackageStatus,
    RequestStatus,
    Role,
    Sensitivity,
    Verdict,
)


@dataclass
class User:
    user_id: str
    institution_id: Optional[str]  # 机构用户非空；权威机构/审计可为空
    roles: tuple[str, ...]
    display_name: str = ""

    def has_role(self, role: Role | str) -> bool:
        wanted = role.value if isinstance(role, Role) else role
        return wanted in self.roles


@dataclass
class Material:
    """逻辑材料（课程大纲、师资、考核、企业反馈中的某一份）。"""

    material_id: str
    institution_id: str
    kind: str                      # MaterialKind
    sensitivity: str               # Sensitivity
    title: str
    current_version_id: Optional[str]
    withdrawn: bool
    created_at: str


@dataclass
class MaterialVersion:
    """材料的一次不可变版本。字节内容按 sha256 内容寻址、去重存储。"""

    version_id: str
    material_id: str
    institution_id: str
    sha256: str
    size: int
    media_type: str
    version_no: int
    supersedes_version_id: Optional[str]
    created_by: str
    created_at: str
    withdrawn: bool                # 该版本是否已撤回


@dataclass
class PackageEntry:
    """评审包对材料【具体版本】的固定引用。"""

    entry_id: str
    package_id: str
    material_id: str
    version_id: str
    sha256: str
    kind: str
    sensitivity: str
    added_at: str


@dataclass
class ReviewPackage:
    package_id: str
    institution_id: str
    title: str
    status: str                    # PackageStatus
    created_by: str
    created_at: str
    sealed_at: Optional[str]
    manifest_fingerprint: Optional[str]
    decided_at: Optional[str]
    decision: Optional[str]        # Decision
    decision_note: Optional[str]
    review_fingerprint: Optional[str]
    supersedes_package_id: Optional[str]  # 后补材料触发的复审包指向前序包
    entries: list[PackageEntry] = field(default_factory=list)

    def is_mutable(self) -> bool:
        return self.status == PackageStatus.DRAFT.value


@dataclass
class ReviewRequest:
    request_id: str
    package_id: str
    institution_id: str
    reviewer_id: str
    status: str                    # RequestStatus
    assigned_by: str
    assigned_at: str
    responded_at: Optional[str]
    completed_at: Optional[str]
    verdict: Optional[str]         # Verdict
    comment: Optional[str]
    deadline_at_utc: Optional[str]  # 截止时刻（UTC）
    deadline_timezone: Optional[str]  # 原始 IANA 时区，仅展示用


@dataclass
class Objection:
    objection_id: str
    request_id: str
    package_id: str
    institution_id: str
    reviewer_id: str
    category: str
    detail: str
    created_at: str


@dataclass
class Blob:
    sha256: str
    data: bytes
    media_type: str
    created_at: str


@dataclass
class AuditEntry:
    audit_id: str
    package_id: Optional[str]
    institution_id: Optional[str]
    actor_id: str
    action: str
    at: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GenealogyNode:
    """谱系中的一个环节（成品 / 中间处理 / 原始材料）。

    kind 取值见 domain.genealogy.NodeKind。node_id 是该环节在谱系内的
    稳定标识（包 id / 版本 id / 断链占位 id）。
    """

    node_id: str
    kind: str
    label: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GenealogyEdge:
    """一条溯源关系：from_id 处的记录引用了 to_id 处的上游环节。

    方向与数据库引用一致（成品 → 版本 → 前版/原始材料），档案员反查时
    沿 from → to 逐环向上游走。
    """

    from_id: str     # 下游（更接近成品）持有的引用
    to_id: str       # 上游（更接近原始材料）被引用的环节
    relation: str    # EdgeRelation


@dataclass
class BrokenLinkMark:
    """谱系断链标记：缺失的一环，只记录缺口本身，绝不凭空补齐。

    标记持久化在 SQLite（lineage_breaks 表），反复查询同一缺口只追加一次。
    missing_ref 是断链指向却找不到实体的引用（如缺失的版本 id / 材料 id）。
    """

    break_id: str
    package_id: str
    node_id: str                 # 谱系内占位节点 id
    missing_ref: str             # 断链处引用但查无实体的标识
    expected_kind: str           # 期望缺失环节的类型（NodeKind）
    reason: str                  # 断链原因（lineage_reason 常量）
    marked_by: str
    marked_at: str
    detail: dict = field(default_factory=dict)


def asdict(obj) -> dict:
    return dataclasses.asdict(obj)
