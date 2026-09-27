"""材料谱系查询：成品反查、断链标记、不凭空补造、权限与 HTTP 边界。"""
import sqlite3
import unittest

from service_09252_006.api.http_api import HttpApiServer
from service_09252_006.domain.enums import LineageNodeKind, Role
from service_09252_006.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from tests.support import Harness
from tests.test_http_api import ApiClient

RAW = LineageNodeKind.RAW_MATERIAL.value
INTER = LineageNodeKind.INTERMEDIATE.value
PRODUCT = LineageNodeKind.PRODUCT.value


class LineageTraceTests(unittest.TestCase):
    """服务层：登记谱系 -> 档案员从成品反查。"""

    def setUp(self) -> None:
        self.h = Harness()
        self.admin = self.h.user("admin-a", Role.INSTITUTION_ADMIN)
        self.archivist = self.h.user(
            "arch-1", Role.ARCHIVIST, institution_id=None
        )

    def tearDown(self) -> None:
        self.h.close()

    # ------------------------------------------------------------ 辅助
    def _register(self, kind, name, **kw):
        return self.h.ctx.lineage.register_node(
            self.admin, kind=kind, name=name, **kw
        )

    def _edge(self, child_id, parent_id, process):
        return self.h.ctx.lineage.register_edge(
            self.admin, child_id=child_id, parent_id=parent_id, process=process
        )

    def _full_chain(self):
        """原料 ->(切割) 中间产物 ->(组装) 成品。"""
        raw = self._register(RAW, "钛合金棒料")
        inter = self._register(INTER, "叶片毛坯")
        product = self._register(PRODUCT, "压气机叶片")
        self._edge(inter["node_id"], raw["node_id"], "切割")
        self._edge(product["node_id"], inter["node_id"], "五轴铣削")
        return raw, inter, product

    # ------------------------------------------------------------ 完整链
    def test_trace_full_chain_from_product(self):
        raw, inter, product = self._full_chain()

        result = self.h.ctx.lineage.trace_origins(
            self.archivist, product_id=product["node_id"]
        )

        self.assertTrue(result["complete"])
        self.assertEqual(result["breaks"], [])
        tree = result["tree"]
        self.assertEqual(tree["kind"], PRODUCT)
        self.assertEqual(tree["status"], "registered")
        # 中间处理环节随边呈现
        self.assertEqual(tree["sources"][0]["process"], "五轴铣削")
        mid = tree["sources"][0]["node"]
        self.assertEqual(mid["node_id"], inter["node_id"])
        self.assertEqual(mid["kind"], INTER)
        self.assertEqual(mid["sources"][0]["process"], "切割")
        leaf = mid["sources"][0]["node"]
        self.assertEqual(leaf["node_id"], raw["node_id"])
        self.assertEqual(leaf["kind"], RAW)
        self.assertEqual(leaf["sources"], [])

    def test_trace_multiple_sources(self):
        """混料：一个中间产物有两个上游。"""
        raw_a = self._register(RAW, "铝锭")
        raw_b = self._register(RAW, "铜锭")
        alloy = self._register(INTER, "合金液")
        product = self._register(PRODUCT, "铸件")
        self._edge(alloy["node_id"], raw_a["node_id"], "投料")
        self._edge(alloy["node_id"], raw_b["node_id"], "投料")
        self._edge(product["node_id"], alloy["node_id"], "浇铸")

        result = self.h.ctx.lineage.trace_origins(
            self.archivist, product_id=product["node_id"]
        )

        self.assertTrue(result["complete"])
        mid = result["tree"]["sources"][0]["node"]
        upstream = {s["node"]["node_id"] for s in mid["sources"]}
        self.assertEqual(upstream, {raw_a["node_id"], raw_b["node_id"]})

    # ------------------------------------------------------------ 断链
    def test_missing_link_marked_broken_not_fabricated(self):
        """缺失一环：标记断链，且绝不自动补造节点/材料。"""
        inter = self._register(INTER, "叶片毛坯")
        product = self._register(PRODUCT, "压气机叶片")
        # 上游批次 lot-x 从未登记：如实记录来源关系，不补造
        self._edge(inter["node_id"], "lot-x", "切割")
        self._edge(product["node_id"], inter["node_id"], "五轴铣削")
        nodes_before = len(self.h.repo.list_lineage_nodes())

        result = self.h.ctx.lineage.trace_origins(
            self.archivist, product_id=product["node_id"]
        )

        # 结果如实标注断链
        self.assertFalse(result["complete"])
        self.assertEqual(
            result["breaks"],
            [
                {
                    "edge_id": result["breaks"][0]["edge_id"],
                    "child_id": inter["node_id"],
                    "missing_node_id": "lot-x",
                    "process": "切割",
                }
            ],
        )
        broken = result["tree"]["sources"][0]["node"]["sources"][0]["node"]
        self.assertEqual(broken["node_id"], "lot-x")
        self.assertEqual(broken["status"], "broken")
        self.assertIsNone(broken["kind"])

        # 关键断言：反查没有补造任何谱系节点
        self.assertEqual(len(self.h.repo.list_lineage_nodes()), nodes_before)
        self.assertIsNone(self.h.repo.get_lineage_node("lot-x"))
        # 也没有补造证据材料：materials 表仍为空
        conn = sqlite3.connect(self.h.db_path)
        try:
            (materials,) = conn.execute(
                "SELECT COUNT(*) FROM materials"
            ).fetchone()
            (nodes,) = conn.execute(
                "SELECT COUNT(*) FROM lineage_nodes"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(materials, 0)
        self.assertEqual(nodes, nodes_before)

    def test_break_persisted_in_sqlite(self):
        """SQLite 标记断链节点：断点落库且重复反查只记一次。"""
        product = self._register(PRODUCT, "成品")
        edge = self._edge(product["node_id"], "ghost-batch", "热处理")

        first = self.h.ctx.lineage.trace_origins(
            self.archivist, product_id=product["node_id"]
        )
        second = self.h.ctx.lineage.trace_origins(
            self.archivist, product_id=product["node_id"]
        )

        self.assertFalse(first["complete"])
        self.assertFalse(second["complete"])
        breaks = self.h.repo.list_lineage_breaks()
        self.assertEqual(len(breaks), 1)  # 同一断点只标记一次
        self.assertEqual(breaks[0].edge_id, edge["edge_id"])
        self.assertEqual(breaks[0].missing_node_id, "ghost-batch")
        self.assertEqual(breaks[0].detected_by, "arch-1")
        # 服务视图同样可见，且未消解
        listed = self.h.ctx.lineage.list_breaks(self.archivist)
        self.assertEqual(len(listed), 1)
        self.assertFalse(listed[0]["resolved"])

    def test_unregistered_product_raises_without_fabricating(self):
        """成品本身未登记：如实报缺失，不补造。"""
        with self.assertRaises(NotFoundError):
            self.h.ctx.lineage.trace_origins(
                self.archivist, product_id="no-such-product"
            )
        self.assertEqual(self.h.repo.list_lineage_nodes(), [])
        self.assertEqual(self.h.repo.list_lineage_breaks(), [])

    def test_break_resolved_only_by_explicit_registration(self):
        """断链只能由人显式登记补齐；历史断链标记保留并注解 resolved。"""
        product = self._register(PRODUCT, "成品")
        self._edge(product["node_id"], "lot-y", "热处理")
        before = self.h.ctx.lineage.trace_origins(
            self.archivist, product_id=product["node_id"]
        )
        self.assertFalse(before["complete"])

        # 显式登记缺失环节（人工行为，不是反查自动补造）
        self._register(RAW, "补登记的批次", node_id="lot-y")
        after = self.h.ctx.lineage.trace_origins(
            self.archivist, product_id=product["node_id"]
        )

        self.assertTrue(after["complete"])
        leaf = after["tree"]["sources"][0]["node"]
        self.assertEqual(leaf["status"], "registered")
        self.assertEqual(leaf["kind"], RAW)
        # 断链标记作为历史保留，但已消解
        breaks = self.h.ctx.lineage.list_breaks(self.archivist)
        self.assertEqual(len(breaks), 1)
        self.assertTrue(breaks[0]["resolved"])

    # ------------------------------------------------------------ 登记约束
    def test_cycle_rejected(self):
        a = self._register(INTER, "A")
        b = self._register(INTER, "B")
        self._edge(a["node_id"], b["node_id"], "工序1")
        with self.assertRaises(ConflictError):
            self._edge(b["node_id"], a["node_id"], "工序2")

    def test_self_loop_and_empty_process_rejected(self):
        a = self._register(INTER, "A")
        with self.assertRaises(ValidationError):
            self._edge(a["node_id"], a["node_id"], "自环")
        with self.assertRaises(ValidationError):
            self._edge(a["node_id"], "whatever", "  ")

    def test_edge_requires_registered_child(self):
        with self.assertRaises(NotFoundError):
            self._edge("no-such-child", "no-such-parent", "工序")

    def test_register_node_validates_input(self):
        with self.assertRaises(ValidationError):
            self._register("unknown-kind", "X")
        with self.assertRaises(ValidationError):
            self._register(RAW, "   ")
        # 客户端指定 id 的重复登记：回放而非报错
        first = self._register(RAW, "棒料", node_id="lot-1")
        again = self._register(RAW, "棒料", node_id="lot-1")
        self.assertEqual(first["node_id"], again["node_id"])
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.h.repo.list_lineage_nodes()), 1)

    # ------------------------------------------------------------ 权限
    def test_trace_permissions(self):
        _, _, product = self._full_chain()
        other_admin = self.h.user(
            "admin-b", Role.INSTITUTION_ADMIN, institution_id="inst-b"
        )
        submitter = self.h.user("sub-a", Role.INSTITUTION_SUBMITTER)
        auditor = self.h.user("aud", Role.AUDITOR, institution_id=None)

        # 档案员/审计可跨机构反查
        self.assertTrue(
            self.h.ctx.lineage.trace_origins(
                self.archivist, product_id=product["node_id"]
            )["complete"]
        )
        self.assertTrue(
            self.h.ctx.lineage.trace_origins(
                auditor, product_id=product["node_id"]
            )["complete"]
        )
        # 本机构管理员可查本机构成品
        self.assertTrue(
            self.h.ctx.lineage.trace_origins(
                self.admin, product_id=product["node_id"]
            )["complete"]
        )
        # 跨机构管理员与提交人无权反查
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.lineage.trace_origins(
                other_admin, product_id=product["node_id"]
            )
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.lineage.trace_origins(
                submitter, product_id=product["node_id"]
            )

    def test_register_requires_institution_role(self):
        with self.assertRaises(PermissionDeniedError):
            self.h.ctx.lineage.register_node(
                self.archivist, kind=RAW, name="档案员不能登记"
            )


class LineageHttpTests(unittest.TestCase):
    """HTTP 边界：登记 -> 反查 -> 断链清单。"""

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
            {"user_id": user_id, "token": token},
        )
        self.assertEqual(status, 201, body)
        return ApiClient(self.base, token=token)

    def test_trace_over_http(self):
        admin = self._user(
            "admin-a", ["institution_admin"], institution_id="inst-a",
            token="tok-admin",
        )
        archivist = self._user("arch-1", ["archivist"], token="tok-arch")

        status, raw = admin.request(
            "POST", "/v1/lineage/nodes", {"kind": RAW, "name": "棒料"}
        )
        self.assertEqual(status, 201, raw)
        status, product = admin.request(
            "POST", "/v1/lineage/nodes", {"kind": PRODUCT, "name": "叶片"}
        )
        self.assertEqual(status, 201, product)
        status, edge = admin.request(
            "POST", "/v1/lineage/edges",
            {"child_id": product["node_id"], "parent_id": "ghost",
             "process": "热处理"},
        )
        self.assertEqual(status, 201, edge)
        status, edge2 = admin.request(
            "POST", "/v1/lineage/edges",
            {"child_id": product["node_id"],
             "parent_id": raw["node_id"], "process": "下料"},
        )
        self.assertEqual(status, 201, edge2)

        status, trace = archivist.request(
            "POST", "/v1/lineage/trace", {"product_id": product["node_id"]}
        )
        self.assertEqual(status, 200, trace)
        self.assertFalse(trace["complete"])
        self.assertEqual(len(trace["breaks"]), 1)
        statuses = {
            s["node"]["status"] for s in trace["tree"]["sources"]
        }
        self.assertEqual(statuses, {"registered", "broken"})

        status, body = archivist.request(
            "GET", f"/v1/lineage/nodes/{product['node_id']}/breaks"
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["breaks"]), 1)
        self.assertEqual(body["breaks"][0]["missing_node_id"], "ghost")

        # 未登记成品：404，且谱系节点数不变
        status, body = archivist.request(
            "POST", "/v1/lineage/trace", {"product_id": "no-such"}
        )
        self.assertEqual(status, 404, body)
        self.assertEqual(len(self.h.repo.list_lineage_nodes()), 2)


if __name__ == "__main__":
    unittest.main()
