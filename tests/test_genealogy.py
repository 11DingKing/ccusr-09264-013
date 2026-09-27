"""材料谱系查询：从成品反查原始材料与中间处理；断链标记而非补造。

覆盖：
- 完整谱系：成品 -> 清单条目 -> 版本链（中间处理）-> 原始材料；
- 缺失一环（版本链断、条目指向的版本丢失、原始材料丢失、前序成品
  丢失）时标记断链并落库 SQLite，谱系中放占位节点；
- 反查不会自动补造任何材料/版本/字节（表行数不变，缺失引用仍缺失）；
- 断链标记幂等（重复反查不重复落库）；
- 权限：档案员/审计/权威机构/本机构管理员可查，其余角色拒绝；
- HTTP 端到端。
"""
import sqlite3
import unittest

from service_09252_006.api.http_api import HttpApiServer
from service_09252_006.domain.enums import Decision, Role
from service_09252_006.domain.errors import NotFoundError, PermissionDeniedError
from tests.flow import complete_review, seal_new_package, upload_material
from tests.support import Harness
from tests.test_http_api import ApiClient


def table_counts(db_path):
    """直接读库统计各业务表行数（绕过服务层）。"""
    conn = sqlite3.connect(db_path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("materials", "versions", "blobs", "packages", "entries")
        }
    finally:
        conn.close()


def break_count(db_path, package_id):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM lineage_breaks WHERE package_id = ?",
            (package_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def tamper(db_path, sql, params=()):
    """模拟历史数据缺失：直接改库（新连接默认关闭外键约束）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def find_node(report, node_id):
    for node in report["nodes"]:
        if node["node_id"] == node_id:
            return node
    return None


def has_edge(report, from_id, to_id, relation):
    return {
        "from_id": from_id,
        "to_id": to_id,
        "relation": relation,
    } in report["edges"]


class GenealogyTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.archivist = self.h.user(
            "arch-1", Role.ARCHIVIST, institution_id=None
        )
        self.auditor = self.h.user("aud-1", Role.AUDITOR, institution_id=None)
        self.authority = self.h.user(
            "auth-1", Role.QUALITY_AUTHORITY, institution_id=None
        )
        self.submitter = self.h.user("sub-a", Role.INSTITUTION_SUBMITTER)
        self.reviewer = self.h.user(
            "rev-1", Role.REVIEWER, institution_id="inst-ext"
        )

    def tearDown(self) -> None:
        self.h.close()

    def _sealed_package_with_chain(self):
        """造一个成品：原始材料经 v1 -> v2 两道中间处理后被封存。"""
        up = upload_material(self.h, self.admin, data="大纲 v1".encode("utf-8"))
        v2 = self.h.ctx.evidence.upload_version(
            self.admin, material_id=up.material["material_id"],
            data="大纲 v2（修订）".encode("utf-8"),
        )
        pkg = self.h.ctx.packages.create_package(self.admin, title="成品包")
        self.h.ctx.packages.add_entry(
            self.admin, package_id=pkg["package_id"],
            version_id=v2["version_id"],
        )
        self.h.ctx.packages.seal_package(self.admin, package_id=pkg["package_id"])
        return up, v2, pkg["package_id"]

    # ---------------------------------------------------------- 完整谱系
    def test_complete_lineage_from_finished_package(self):
        up, v2, pid = self._sealed_package_with_chain()

        report = self.h.ctx.genealogy.trace_package(self.archivist, package_id=pid)

        self.assertTrue(report["complete"])
        self.assertEqual(report["broken_links"], [])
        self.assertEqual(report["root_package_id"], pid)
        self.assertEqual(report["traced_by"], self.archivist.user_id)

        kinds = {n["node_id"]: n["kind"] for n in report["nodes"]}
        self.assertEqual(kinds[pid], "package")
        self.assertEqual(kinds[v2["version_id"]], "version")
        self.assertEqual(kinds[up.version["version_id"]], "version")
        self.assertEqual(kinds[up.material["material_id"]], "material")

        # 成品收录版本；版本承接前版（中间处理链）；版本归属原始材料
        self.assertTrue(
            has_edge(report, pid, v2["version_id"], "package_entry")
        )
        self.assertTrue(
            has_edge(
                report, v2["version_id"], up.version["version_id"],
                "version_supersedes",
            )
        )
        self.assertTrue(
            has_edge(
                report, up.version["version_id"],
                up.material["material_id"], "version_material",
            )
        )
        # 链首版本标记为原始版本
        origin = find_node(report, up.version["version_id"])
        self.assertTrue(origin["detail"]["is_origin"])
        self.assertFalse(
            find_node(report, v2["version_id"])["detail"]["is_origin"]
        )
        # 完整谱系不产生任何断链标记
        self.assertEqual(break_count(self.h.db_path, pid), 0)

    def test_review_package_chain_links_predecessor(self):
        sealed = seal_new_package(self.h, self.admin)
        complete_review(self.h, self.authority, self.reviewer, sealed.package_id)
        self.h.ctx.reviews.issue_decision(
            self.authority, package_id=sealed.package_id,
            decision=Decision.APPROVED.value,
        )
        follow = self.h.ctx.packages.create_package(
            self.admin, title="复审包",
            supersedes_package_id=sealed.package_id,
        )

        report = self.h.ctx.genealogy.trace_package(
            self.archivist, package_id=follow["package_id"]
        )

        self.assertTrue(report["complete"])
        self.assertTrue(
            has_edge(
                report, follow["package_id"], sealed.package_id,
                "package_supersedes",
            )
        )
        self.assertIsNotNone(find_node(report, sealed.package_id))

    # ---------------------------------------------------------- 断链标记
    def test_broken_version_chain_marks_break_without_fabricating(self):
        _, v2, pid = self._sealed_package_with_chain()
        # 历史数据缺失：v2 的前一版本引用查无实体
        tamper(
            self.h.db_path,
            "UPDATE versions SET supersedes_version_id = 'ver_lost'"
            " WHERE version_id = ?",
            (v2["version_id"],),
        )

        before = table_counts(self.h.db_path)
        report = self.h.ctx.genealogy.trace_package(self.archivist, package_id=pid)
        after = table_counts(self.h.db_path)

        self.assertFalse(report["complete"])
        self.assertEqual(len(report["broken_links"]), 1)
        brk = report["broken_links"][0]
        self.assertEqual(brk["missing_ref"], "ver_lost")
        self.assertEqual(brk["expected_kind"], "version")
        self.assertEqual(brk["reason"], "missing_version")

        # 谱系中是断链占位节点，而不是补造的版本
        placeholder = find_node(report, "break:ver_lost")
        self.assertIsNotNone(placeholder)
        self.assertEqual(placeholder["kind"], "break")
        self.assertEqual(placeholder["detail"]["missing_ref"], "ver_lost")
        self.assertIsNone(find_node(report, "ver_lost"))
        self.assertTrue(
            has_edge(report, v2["version_id"], "break:ver_lost",
                     "version_supersedes")
        )

        # 关键：反查没有自动补造任何材料/版本/字节
        self.assertEqual(before, after)
        self.assertIsNone(self.h.repo.get_version("ver_lost"))

        # 断链标记已落 SQLite
        marks = self.h.repo.list_broken_links(pid)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0].missing_ref, "ver_lost")
        self.assertEqual(marks[0].reason, "missing_version")
        self.assertEqual(marks[0].marked_by, self.archivist.user_id)
        self.assertEqual(report["newly_marked"], 1)

    def test_missing_entry_version_marks_break(self):
        up = upload_material(self.h, self.admin, data=b"v1")
        pkg = self.h.ctx.packages.create_package(self.admin, title="成品包")
        self.h.ctx.packages.add_entry(
            self.admin, package_id=pkg["package_id"],
            version_id=up.version["version_id"],
        )
        self.h.ctx.packages.seal_package(
            self.admin, package_id=pkg["package_id"]
        )
        pid = pkg["package_id"]
        tamper(
            self.h.db_path,
            "DELETE FROM versions WHERE version_id = ?",
            (up.version["version_id"],),
        )

        before = table_counts(self.h.db_path)
        report = self.h.ctx.genealogy.trace_package(self.archivist, package_id=pid)
        after = table_counts(self.h.db_path)

        self.assertFalse(report["complete"])
        brk = report["broken_links"][0]
        self.assertEqual(brk["reason"], "missing_version")
        self.assertEqual(brk["missing_ref"], up.version["version_id"])
        self.assertEqual(before, after)  # 没有补造版本
        self.assertEqual(break_count(self.h.db_path, pid), 1)

    def test_missing_material_marks_break_without_fabricating(self):
        up = upload_material(self.h, self.admin, data=b"v1")
        pkg = self.h.ctx.packages.create_package(self.admin, title="成品包")
        self.h.ctx.packages.add_entry(
            self.admin, package_id=pkg["package_id"],
            version_id=up.version["version_id"],
        )
        self.h.ctx.packages.seal_package(
            self.admin, package_id=pkg["package_id"]
        )
        pid = pkg["package_id"]
        material_id = up.material["material_id"]
        tamper(
            self.h.db_path,
            "DELETE FROM materials WHERE material_id = ?",
            (material_id,),
        )

        before = table_counts(self.h.db_path)
        report = self.h.ctx.genealogy.trace_package(self.archivist, package_id=pid)
        after = table_counts(self.h.db_path)

        self.assertFalse(report["complete"])
        brk = report["broken_links"][0]
        self.assertEqual(brk["reason"], "missing_material")
        self.assertEqual(brk["expected_kind"], "material")
        self.assertEqual(brk["missing_ref"], material_id)
        placeholder = find_node(report, f"break:{material_id}")
        self.assertEqual(placeholder["kind"], "break")
        # 反查没有补造原始材料
        self.assertEqual(before, after)
        self.assertIsNone(self.h.repo.get_material(material_id))

    def test_missing_predecessor_package_marks_break(self):
        sealed = seal_new_package(self.h, self.admin)
        complete_review(self.h, self.authority, self.reviewer, sealed.package_id)
        self.h.ctx.reviews.issue_decision(
            self.authority, package_id=sealed.package_id,
            decision=Decision.APPROVED.value,
        )
        follow = self.h.ctx.packages.create_package(
            self.admin, title="复审包",
            supersedes_package_id=sealed.package_id,
        )
        tamper(
            self.h.db_path,
            "DELETE FROM packages WHERE package_id = ?",
            (sealed.package_id,),
        )

        before = table_counts(self.h.db_path)
        report = self.h.ctx.genealogy.trace_package(
            self.archivist, package_id=follow["package_id"]
        )
        after = table_counts(self.h.db_path)

        self.assertFalse(report["complete"])
        brk = report["broken_links"][0]
        self.assertEqual(brk["reason"], "missing_package")
        self.assertEqual(brk["expected_kind"], "package")
        self.assertEqual(brk["missing_ref"], sealed.package_id)
        self.assertEqual(before, after)

    # ---------------------------------------------------------- 幂等
    def test_retrace_does_not_duplicate_break_marks(self):
        _, v2, pid = self._sealed_package_with_chain()
        tamper(
            self.h.db_path,
            "UPDATE versions SET supersedes_version_id = 'ver_lost'"
            " WHERE version_id = ?",
            (v2["version_id"],),
        )

        first = self.h.ctx.genealogy.trace_package(self.archivist, package_id=pid)
        second = self.h.ctx.genealogy.trace_package(self.auditor, package_id=pid)

        self.assertEqual(first["newly_marked"], 1)
        self.assertEqual(second["newly_marked"], 0)
        self.assertFalse(second["complete"])
        self.assertEqual(len(second["broken_links"]), 1)
        # SQLite 中仍只有一条断链标记
        self.assertEqual(break_count(self.h.db_path, pid), 1)
        # 档案员可列出已登记的断链
        listed = self.h.ctx.genealogy.list_broken_links(
            self.archivist, package_id=pid
        )
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["missing_ref"], "ver_lost")
        self.assertEqual(listed[0]["marked_by"], self.archivist.user_id)

    # ---------------------------------------------------------- 权限与边界
    def test_trace_permission_matrix(self):
        sealed = seal_new_package(self.h, self.admin)

        for allowed in (self.archivist, self.auditor, self.authority, self.admin):
            report = self.h.ctx.genealogy.trace_package(
                allowed, package_id=sealed.package_id
            )
            self.assertTrue(report["complete"], allowed.user_id)

        foreign_admin = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )
        for denied in (self.submitter, self.reviewer, foreign_admin):
            with self.assertRaises(PermissionDeniedError):
                self.h.ctx.genealogy.trace_package(
                    denied, package_id=sealed.package_id
                )
            with self.assertRaises(PermissionDeniedError):
                self.h.ctx.genealogy.list_broken_links(
                    denied, package_id=sealed.package_id
                )

    def test_trace_unknown_package_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.h.ctx.genealogy.trace_package(
                self.archivist, package_id="pkg_none"
            )
        with self.assertRaises(NotFoundError):
            self.h.ctx.genealogy.list_broken_links(
                self.archivist, package_id="pkg_none"
            )


class GenealogyHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.server = HttpApiServer(
            self.h.ctx, host="127.0.0.1", port=0, bootstrap_token="boot-secret"
        )
        self.server.start()
        host, port = self.server.address
        self.base = f"http://{host}:{port}"
        self.boot = ApiClient(self.base, bootstrap="boot-secret")

    def tearDown(self) -> None:
        self.server.stop()
        self.h.close()

    def _user(self, user_id, roles, institution_id=None, token=None):
        status, body = self.boot.request(
            "POST", "/v1/admin/users",
            {"user_id": user_id, "roles": roles,
             "institution_id": institution_id},
        )
        self.assertEqual(status, 201, body)
        status, body = self.boot.request(
            "POST", "/v1/admin/tokens",
            {"user_id": user_id, "token": token or f"tok-{user_id}"},
        )
        self.assertEqual(status, 201, body)
        return ApiClient(self.base, token=token or f"tok-{user_id}")

    def test_lineage_endpoints_over_http(self):
        admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        archivist = self._user("arch-1", ["archivist"])
        submitter = self._user("sub-a", ["institution_submitter"],
                               institution_id="inst-a")

        up = upload_material(self.h, admin, data=b"v1")
        v2 = self.h.ctx.evidence.upload_version(
            admin, material_id=up.material["material_id"], data=b"v2"
        )
        pkg = self.h.ctx.packages.create_package(admin, title="成品包")
        self.h.ctx.packages.add_entry(
            admin, package_id=pkg["package_id"], version_id=v2["version_id"]
        )
        self.h.ctx.packages.seal_package(admin, package_id=pkg["package_id"])
        pid = pkg["package_id"]

        # 完整谱系
        status, body = archivist.request("GET", f"/v1/packages/{pid}/lineage")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["complete"])
        self.assertEqual(body["broken_links"], [])

        # 制造断链后再查：标记断链而不是补造
        tamper(
            self.h.db_path,
            "UPDATE versions SET supersedes_version_id = 'ver_lost'"
            " WHERE version_id = ?",
            (v2["version_id"],),
        )
        before = table_counts(self.h.db_path)
        status, body = archivist.request("GET", f"/v1/packages/{pid}/lineage")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["complete"])
        self.assertEqual(body["broken_links"][0]["missing_ref"], "ver_lost")
        self.assertEqual(before, table_counts(self.h.db_path))

        # 断链清单端点
        status, body = archivist.request(
            "GET", f"/v1/packages/{pid}/lineage/breaks"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["broken_links"]), 1)
        self.assertEqual(body["broken_links"][0]["reason"], "missing_version")

        # 无权限角色与匿名访问被拒绝
        status, _ = submitter.request("GET", f"/v1/packages/{pid}/lineage")
        self.assertEqual(status, 403)
        status, _ = ApiClient(self.base).request(
            "GET", f"/v1/packages/{pid}/lineage"
        )
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
