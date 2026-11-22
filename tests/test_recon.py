"""端到端业务测试（Flask test client + 临时 SQLite）。

覆盖：
1. 三类 CSV 上传、补传与按来源区分编号；
2. 同杆确认的传递性分组；
3. 冲突组方案选择、复算撤销、整组入仓清单（已核对单归属不变）；
4. 补传后仍可撤销，与补传后条件不符须重选的情况；
5. 无解提示；
6. 条件不符/写入失败时原关系与归属保留。
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="pole-recon-test-")
os.environ["POLE_RECON_DB"] = os.path.join(_tmp, "test.db")

from app import services  # noqa: E402
from app.db import get_db, init_db  # noqa: E402
from app.routes import app  # noqa: E402


def csv_bytes(header: str, rows: list[tuple]) -> bytes:
    lines = [header] + [",".join(str(c) for c in r) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


ASSET_CSV = csv_bytes(
    "asset_no,pole_source,pole_no",
    [
        ("AST-001", "LEDGER_A", "P1"),
        ("AST-002", "LEDGER_B", "P1"),
        ("AST-003", "LEDGER_A", "P2"),
    ],
)
# 先传两张单：A/P1 与 B/P1 各一张，两个资产号 -> 撤销后可各得唯一归属
TICKET_CSV_1 = csv_bytes(
    "ticket_no,source,pole_source,pole_no",
    [
        ("T100", "LEDGER_A", "LEDGER_A", "P1"),
        ("T200", "LEDGER_B", "LEDGER_B", "P1"),
    ],
)
# 同杆确认两条，传递后 A/P1 - B/P1 - A/P2 并为一组
CONFIRM_CSV = csv_bytes(
    "source_a,pole_no_a,source_b,pole_no_b,confirmed_by",
    [
        ("LEDGER_A", "P1", "LEDGER_B", "P1", "巡检甲"),
        ("LEDGER_A", "P2", "LEDGER_B", "P1", "巡检乙"),
    ],
)
# 补传第三张单挂在 A/P2
TICKET_CSV_2 = csv_bytes(
    "ticket_no,source,pole_source,pole_no",
    [("T300", "LEDGER_A", "LEDGER_A", "P2")],
)
# 再补一张单，使整组 4 张单抢 3 个资产号，任意单条撤销都无解
TICKET_CSV_3 = csv_bytes(
    "ticket_no,source,pole_source,pole_no",
    [("T301", "LEDGER_A", "LEDGER_A", "P2")],
)


class ReconFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = get_db()
        init_db(cls.conn)
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        conn = get_db()
        init_db(conn)  # 保证新表（asset_version 等）已建再清表
        for table in (
            "warehouse_item", "warehouse", "undo_plan", "verified",
            "confirmation", "ticket", "asset", "asset_version", "batch",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()

    def upload(self, kind: str, content: bytes, name: str = "f.csv"):
        return self.client.post(
            "/upload",
            data={"kind": kind, "file": (io.BytesIO(content), name)},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    # ------------------------------------------------------------------ #
    def test_01_uploads_and_transitive_group(self):
        self.upload("asset", ASSET_CSV)
        r = self.upload("ticket", TICKET_CSV_1)
        self.assertIn("新增 2 张", r.get_data(as_text=True))
        r = self.upload("confirm", CONFIRM_CSV)
        self.assertIn("新增 2 条", r.get_data(as_text=True))

        conn = get_db()
        groups = services.list_groups(conn)
        conn.close()
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(set(g["poles"]), {"LEDGER_A/P1", "LEDGER_B/P1", "LEDGER_A/P2"})
        self.assertEqual(len(g["ticket_ids"]), 2)
        self.assertEqual(len(g["asset_ids"]), 3)
        self.assertTrue(g["feasible"])
        self.group_key = g["group_key"]

    def test_02_options_and_apply_undo(self):
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        g = services.list_groups(conn)[0]
        key = g["group_key"]
        detail = services.get_group_detail(conn, key)
        conn.close()
        # 两条确认都可作为单条撤销方案（撤销后每张单都有唯一资产号）
        self.assertEqual(len(detail["options"]), 2)

        # 选第一条方案执行
        opt = detail["options"][0]
        r = self.client.post(
            f"/groups/{key}/apply",
            data={"confirmation_id": opt["confirmation_id"]},
            follow_redirects=True,
        )
        text = r.get_data(as_text=True)
        self.assertIn("整组入仓清单", text)
        self.assertIn("AST-001", text)
        self.assertIn("AST-002", text)

        conn = get_db()
        # 确认已撤销
        self.assertEqual(
            conn.execute(
                "SELECT active FROM confirmation WHERE id=?",
                (opt["confirmation_id"],),
            ).fetchone()["active"],
            0,
        )
        # 两张单都已核对，归属是各自灯杆上的资产号
        rows = {
            (r["source"], r["ticket_no"]): r["asset_no"]
            for r in conn.execute(
                "SELECT t.source, t.ticket_no, a.asset_no "
                "FROM verified v JOIN ticket t ON t.id=v.ticket_id "
                "JOIN asset a ON a.id=v.asset_id"
            ).fetchall()
        }
        self.assertEqual(rows[("LEDGER_A", "T100")], "AST-001")
        self.assertEqual(rows[("LEDGER_B", "T200")], "AST-002")
        # 入仓清单含两张单
        wh = conn.execute("SELECT id FROM warehouse WHERE group_key=?", (key,)).fetchone()
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) c FROM warehouse_item WHERE warehouse_id=?",
                (wh["id"],),
            ).fetchone()["c"],
            2,
        )
        # 撤销后原三杆连通组被拆开：不再存在同时含 A/P1 与 B/P1 的组
        remaining = services.list_groups(conn)
        for g in remaining:
            self.assertFalse(
                {"LEDGER_A/P1", "LEDGER_B/P1"} <= set(g["poles"]),
                "撤销后两根灯杆不应仍在同一冲突组",
            )
        # 已核对单即使仍在剩余冲突组中，也带锁定标记
        verified_ids = {
            r["ticket_id"]
            for r in conn.execute("SELECT ticket_id FROM verified").fetchall()
        }
        for g in remaining:
            for tid in g["ticket_ids"]:
                if tid in verified_ids:
                    self.assertIn(tid, g["verified_ticket_ids"])
        conn.close()

    def test_03_supplement_then_undo_still_works_locks_kept(self):
        """补传后再次成组：已核对单归属不变，新单也有唯一归属 -> 可撤销。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        first_edge = services.get_group_detail(conn, key)["options"][0]["confirmation_id"]
        services.apply_plan(conn, key, first_edge)
        conn.close()

        # 补传：把被撤销的确认重新确认（模拟新资料），再补第三张单
        reconfirm = csv_bytes(
            "source_a,pole_no_a,source_b,pole_no_b",
            [("LEDGER_A", "P1", "LEDGER_B", "P1")],
        )
        r = self.upload("confirm", reconfirm)
        self.assertIn("重新确认 1 条", r.get_data(as_text=True))
        r = self.upload("ticket", TICKET_CSV_2)
        self.assertIn("新增 1 张", r.get_data(as_text=True))

        conn = get_db()
        groups = services.list_groups(conn)
        self.assertEqual(len(groups), 1)
        key2 = groups[0]["group_key"]
        self.assertEqual(len(groups[0]["verified_ticket_ids"]), 2)  # 两张已核对单锁定
        detail = services.get_group_detail(conn, key2)
        self.assertTrue(detail["options"])  # 仍有可行方案

        # 执行任一可行方案
        result = services.apply_plan(conn, key2, detail["options"][0]["confirmation_id"])
        # 校验：已核对单归属不变
        kept = {
            item["ticket"]: item["asset_no"]
            for part in result["partitions"]
            for item in part["items"]
        }
        self.assertEqual(kept["LEDGER_A/T100"], "AST-001")
        self.assertEqual(kept["LEDGER_B/T200"], "AST-002")
        self.assertEqual(kept["LEDGER_A/T300"], "AST-003")  # 新单唯一归属
        # 三张单全部在入仓清单/verified 中
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM verified").fetchone()["c"], 3
        )
        conn.close()

    def test_04_supplement_makes_no_solution(self):
        """补传后 4 张单抢 3 个资产号：任意单条撤销都无解，页面提示且不动数据。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)

        r = self.upload("ticket", TICKET_CSV_2)
        self.assertIn("新增 1 张", r.get_data(as_text=True))
        r = self.upload("ticket", TICKET_CSV_3)
        self.assertIn("新增 1 张", r.get_data(as_text=True))

        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        detail = services.get_group_detail(conn, key)
        self.assertEqual(detail["options"], [])  # 无解
        self.assertFalse(detail["feasible"])

        # 直接尝试撤销组内任意确认，应被拒绝，且确认仍活动
        edge_id = detail["edge_ids"][0]
        with self.assertRaises(ValueError):
            services.apply_plan(conn, key, edge_id)
        self.assertEqual(
            conn.execute("SELECT active FROM confirmation WHERE id=?", (edge_id,)).fetchone()["active"],
            1,
        )
        conn.close()

        # 网页路径同样提示
        r = self.client.post(
            f"/groups/{key}/apply",
            data={"confirmation_id": edge_id},
            follow_redirects=True,
        )
        self.assertIn("未能撤销", r.get_data(as_text=True))

    def test_05_stale_selection_must_reselect(self):
        """补传确认改变组结构后，旧方案的确认已不在新组 -> 须重选。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        edges = {
            e["id"]: (e["source_a"], e["pole_no_a"], e["source_b"], e["pole_no_b"])
            for e in conn.execute("SELECT * FROM confirmation").fetchall()
        }
        conn.close()

        # 先撤销其中一条
        conn = get_db()
        detail = services.get_group_detail(conn, key)
        gone = detail["options"][0]["confirmation_id"]
        services.apply_plan(conn, key, gone)
        conn.close()

        # 补传新确认，把另一根灯杆也并进 A/P1，形成新组
        extra = csv_bytes(
            "source_a,pole_no_a,source_b,pole_no_b",
            [("LEDGER_A", "P1", "LEDGER_A", "P2")],
        )
        self.upload("confirm", extra)
        conn = get_db()
        groups = services.list_groups(conn)
        self.assertTrue(groups)
        new_key = groups[0]["group_key"]
        # 旧确认仍处于撤销状态，对新组执行应报“须重新选择”
        with self.assertRaises(ValueError) as ctx:
            services.apply_plan(conn, new_key, gone)
        self.assertIn("重新选择", str(ctx.exception))
        # 数据保持原样
        self.assertEqual(
            conn.execute("SELECT active FROM confirmation WHERE id=?", (gone,)).fetchone()["active"],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM undo_plan").fetchone()["c"], 1
        )
        conn.close()

    def test_06_duplicate_source_numbers_are_distinct(self):
        """编号按来源区分：不同台账相同灯杆号/单号互不覆盖。"""
        assets = csv_bytes(
            "asset_no,pole_source,pole_no",
            [("AST-1", "LEDGER_A", "X1"), ("AST-2", "LEDGER_B", "X1")],
        )
        tickets = csv_bytes(
            "ticket_no,source,pole_source,pole_no",
            [
                ("T1", "LEDGER_A", "LEDGER_A", "X1"),
                ("T1", "LEDGER_B", "LEDGER_B", "X1"),  # 同号不同来源
            ],
        )
        confirms = csv_bytes(
            "source_a,pole_no_a,source_b,pole_no_b",
            [("LEDGER_A", "X1", "LEDGER_B", "X1")],
        )
        self.upload("asset", assets)
        r = self.upload("ticket", tickets)
        self.assertIn("新增 2 张", r.get_data(as_text=True))
        self.upload("confirm", confirms)
        conn = get_db()
        groups = services.list_groups(conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["poles"]), {"LEDGER_A/X1", "LEDGER_B/X1"})
        self.assertEqual(len(groups[0]["ticket_ids"]), 2)
        conn.close()

    def test_07_ticket_supplement_upsert_and_bad_csv(self):
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        # 补传单号已存在 -> 更新而不是新增
        upd = csv_bytes(
            "ticket_no,source,pole_source,pole_no",
            [("T100", "LEDGER_A", "LEDGER_A", "P1")],
        )
        r = self.upload("ticket", upd)
        self.assertIn("更新 1 张", r.get_data(as_text=True))
        # 缺列文件整体拒收
        bad = csv_bytes("ticket_no,source", [("X", "LEDGER_A")])
        r = self.upload("ticket", bad)
        self.assertIn("上传失败", r.get_data(as_text=True))
        conn = get_db()
        # 坏文件没有写入任何东西
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM ticket").fetchone()["c"], 2
        )
        conn.close()

    def test_08_write_failure_keeps_original_relations(self):
        """模拟写入失败：事务回滚，确认仍活动、无入仓清单。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        edge = services.get_group_detail(conn, key)["options"][0]["confirmation_id"]

        # 让事务中的写入抛错（触发器），验证回滚
        conn.execute(
            "CREATE TRIGGER fail_demo BEFORE INSERT ON undo_plan "
            "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END"
        )
        with self.assertRaises(ValueError) as ctx:
            services.apply_plan(conn, key, edge)
        self.assertIn("写入数据库失败", str(ctx.exception))
        # 原关系和归属保留
        self.assertEqual(
            conn.execute("SELECT active FROM confirmation WHERE id=?", (edge,)).fetchone()["active"],
            1,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM verified").fetchone()["c"], 0
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM warehouse").fetchone()["c"], 0
        )
        conn.execute("DROP TRIGGER fail_demo")
        conn.close()

    def test_09_idempotent_confirm_upload(self):
        self.upload("confirm", CONFIRM_CSV)
        r = self.upload("confirm", CONFIRM_CSV)
        self.assertIn("已存在跳过 2 条", r.get_data(as_text=True))
        conn = get_db()
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM confirmation").fetchone()["c"], 2
        )
        conn.close()

    # ------------------------------------------------------------------ #
    # 整版换表：停用 / 移号 / 占号 / 旧入仓回查 / 交错提交 / 旧库升级
    # ------------------------------------------------------------------ #
    def _setup_one_warehoused_group(self):
        """上传样例并撤销一次，得到两张已核对单 T100->AST-001、T200->AST-002。"""
        self.upload("asset", ASSET_CSV)
        self.upload("ticket", TICKET_CSV_1)
        self.upload("confirm", CONFIRM_CSV)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        edge = services.get_group_detail(conn, key)["options"][0]["confirmation_id"]
        services.apply_plan(conn, key, edge)
        conn.close()
        return key

    def test_10_replace_table_deactivates_and_moves_assets(self):
        """换表：新版缺失的号停用；同号改挂别杆；旧入仓记录照旧回查原号。"""
        key = self._setup_one_warehoused_group()

        # 新版清单：AST-001 停用；AST-002 从 LEDGER_B/P1 改挂到 LEDGER_A/P2；
        # AST-003 不变；新增 AST-004 挂 LEDGER_B/P1
        new_assets = csv_bytes(
            "asset_no,pole_source,pole_no",
            [
                ("AST-002", "LEDGER_A", "P2"),
                ("AST-003", "LEDGER_A", "P2"),
                ("AST-004", "LEDGER_B", "P1"),
            ],
        )
        r = self.upload("asset", new_assets, "assets-v2.csv")
        text = r.get_data(as_text=True)
        self.assertIn("整版换表", text)
        self.assertIn("停用 1 个", text)
        self.assertIn("改挂灯杆 1 个", text)
        self.assertIn("新增 1 个", text)

        conn = get_db()
        # 停用资产行保留而不是删除，旧 verified / warehouse_item 外键与回查完好
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM asset").fetchone()["c"], 4
        )
        rows = {r["asset_no"]: r for r in conn.execute("SELECT * FROM asset")}
        self.assertEqual(rows["AST-001"]["active"], 0)
        self.assertEqual(
            (rows["AST-002"]["pole_source"], rows["AST-002"]["pole_no"]),
            ("LEDGER_A", "P2"),
        )
        self.assertEqual(rows["AST-002"]["active"], 1)
        # 已核对归属原样保留
        locked = {
            r["ticket_no"]: r["asset_no"]
            for r in conn.execute(
                "SELECT t.ticket_no, a.asset_no FROM verified v "
                "JOIN ticket t ON t.id=v.ticket_id "
                "JOIN asset a ON a.id=v.asset_id"
            )
        }
        self.assertEqual(locked["T100"], "AST-001")
        self.assertEqual(locked["T200"], "AST-002")
        # 版本号记录：换表生成新版本；历史入仓单保留生成时的第 1 版，
        # 只有再次撤销复算才会把入仓单刷新到当时生效的版本
        versions = [r["id"] for r in conn.execute("SELECT id FROM asset_version")]
        self.assertEqual(len(versions), 2)  # 首次上传第 1 版，本次换表第 2 版
        ver = versions[-1]
        self.assertEqual(
            conn.execute(
                "SELECT asset_version_id FROM warehouse WHERE group_key=?", (key,)
            ).fetchone()["asset_version_id"],
            versions[0],
        )
        self.assertEqual(
            conn.execute(
                "SELECT asset_version_id FROM undo_plan WHERE group_key=? "
                "ORDER BY id DESC LIMIT 1",
                (key,),
            ).fetchone()["asset_version_id"],
            versions[0],
        )
        conn.close()

        # 入仓清单页面仍回查原资产号，并标注停用/改挂
        conn = get_db()
        wid = conn.execute(
            "SELECT id FROM warehouse WHERE group_key=?", (key,)
        ).fetchone()["id"]
        conn.close()
        page = self.client.get(f"/warehouse/{wid}").get_data(as_text=True)
        self.assertIn("AST-001", page)
        self.assertIn("AST-002", page)
        self.assertIn("已停用", page)
        self.assertIn("已改挂", page)

    def test_11_mixed_old_and_new_tickets_share_group_after_move(self):
        """移号后新旧单混组：已核对单占住停用/移走号，待核对单只能选同组空余号。"""
        self._setup_one_warehoused_group()
        # 重新确认把三杆再次并成一组，补一张待核对单挂在 LEDGER_B/P1
        self.upload(
            "confirm",
            csv_bytes(
                "source_a,pole_no_a,source_b,pole_no_b",
                [("LEDGER_A", "P1", "LEDGER_B", "P1")],
            ),
        )
        self.upload(
            "ticket",
            csv_bytes(
                "ticket_no,source,pole_source,pole_no",
                [("T400", "LEDGER_B", "LEDGER_B", "P1")],
            ),
        )
        # 换表：AST-001 停用（T100 占号），AST-002 移走（T200 占号），
        # B/P1 只剩空余活动号 AST-005
        new_assets = csv_bytes(
            "asset_no,pole_source,pole_no",
            [
                ("AST-002", "LEDGER_A", "P9"),   # 改挂到组外灯杆
                ("AST-003", "LEDGER_A", "P2"),
                ("AST-005", "LEDGER_B", "P1"),
            ],
        )
        self.upload("asset", new_assets)

        conn = get_db()
        groups = services.list_groups(conn)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        arows = g["arows"]
        no_by_id = {aid: arows[aid]["asset_no"] for aid in g["asset_ids"]}
        # AST-001 停用、AST-002 移走，但仍被已核对单占在组内
        self.assertEqual(g["asset_status"][
            next(i for i, no in no_by_id.items() if no == "AST-001")
        ], "locked")
        ast2 = next(i for i, no in no_by_id.items() if no == "AST-002")
        self.assertEqual(g["asset_status"][ast2], "locked")
        # 待核对单的同组空余活动资产：AST-005（B/P1）与 AST-003（A/P2），
        # 被占号的 AST-001/AST-002 不在空余集合里
        spare = {no_by_id[i] for i in g["spare_asset_ids"]}
        self.assertEqual(spare, {"AST-003", "AST-005"})

        detail = services.get_group_detail(conn, g["group_key"])
        # 待核对单只能落到空余号（AST-003/AST-005 之一），两张已核对单归属不变，
        # 占号 AST-001（停用）/AST-002（移走）绝不分给新单
        result = services.apply_plan(
            conn, g["group_key"], detail["options"][0]["confirmation_id"]
        )
        assigned = {
            item["ticket"]: item["asset_no"]
            for part in result["partitions"]
            for item in part["items"]
        }
        self.assertEqual(assigned["LEDGER_A/T100"], "AST-001")  # 停用号仍占住
        self.assertEqual(assigned["LEDGER_B/T200"], "AST-002")  # 移走号仍占住
        self.assertIn(assigned["LEDGER_B/T400"], {"AST-003", "AST-005"})
        self.assertNotIn(assigned["LEDGER_B/T400"], {"AST-001", "AST-002"})
        conn.close()

    def test_12_no_spare_asset_means_no_solution_keeps_everything(self):
        """待核对单无同组空余资产号：无解提示重选，原关系和入仓记录保留。"""
        self._setup_one_warehoused_group()
        self.upload(
            "confirm",
            csv_bytes(
                "source_a,pole_no_a,source_b,pole_no_b",
                [("LEDGER_A", "P1", "LEDGER_B", "P1")],
            ),
        )
        # 两张待核对单都挂 B/P1
        self.upload(
            "ticket",
            csv_bytes(
                "ticket_no,source,pole_source,pole_no",
                [
                    ("T400", "LEDGER_B", "LEDGER_B", "P1"),
                    ("T401", "LEDGER_B", "LEDGER_B", "P1"),
                ],
            ),
        )
        # 换表：B/P1 只有一个空余活动号 AST-005（AST-002 被 T200 占住），
        # 同时让 AST-003 停用。拆组后含 B/P1 的子组有 3 张单（T200 占 AST-002、
        # T400/T401 两张待核对）却只有 AST-005 一个空余号 -> 任意撤销都无解
        new_assets = csv_bytes(
            "asset_no,pole_source,pole_no",
            [
                ("AST-001", "LEDGER_A", "P1"),
                ("AST-002", "LEDGER_B", "P1"),
                ("AST-005", "LEDGER_B", "P1"),
            ],
        )
        self.upload("asset", new_assets)
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        detail = services.get_group_detail(conn, key)
        self.assertEqual(detail["options"], [])  # 无解
        edge_id = detail["edge_ids"][0]
        with self.assertRaises(ValueError) as ctx:
            services.apply_plan(conn, key, edge_id)
        self.assertIn("重选", str(ctx.exception))
        # 确认仍活动、已核对归属与入仓记录原样保留
        self.assertEqual(
            conn.execute(
                "SELECT active FROM confirmation WHERE id=?", (edge_id,)
            ).fetchone()["active"],
            1,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM verified").fetchone()["c"], 2
        )
        self.assertEqual(
            conn.execute(
                "SELECT a.asset_no FROM verified v JOIN asset a ON a.id=v.asset_id "
                "JOIN ticket t ON t.id=v.ticket_id WHERE t.ticket_no='T100'"
            ).fetchone()["asset_no"],
            "AST-001",
        )
        conn.close()

    def test_13_interleaved_replace_and_undo_uses_submission_version(self):
        """换表与撤销交错：按撤销提交时生效的整版清单判定。"""
        self._setup_one_warehoused_group()
        self.upload(
            "confirm",
            csv_bytes(
                "source_a,pole_no_a,source_b,pole_no_b",
                [("LEDGER_A", "P1", "LEDGER_B", "P1")],
            ),
        )
        self.upload(
            "ticket",
            csv_bytes(
                "ticket_no,source,pole_source,pole_no",
                [("T400", "LEDGER_B", "LEDGER_B", "P1")],
            ),
        )
        # 第 2 版：B/P1 有 AST-005 -> 此时撤销可行
        self.upload(
            "asset",
            csv_bytes(
                "asset_no,pole_source,pole_no",
                [
                    ("AST-001", "LEDGER_A", "P1"),
                    ("AST-002", "LEDGER_B", "P1"),
                    ("AST-003", "LEDGER_A", "P2"),
                    ("AST-005", "LEDGER_B", "P1"),
                ],
            ),
            "v2.csv",
        )
        conn = get_db()
        key = services.list_groups(conn)[0]["group_key"]
        detail = services.get_group_detail(conn, key)
        edge = detail["options"][0]["confirmation_id"]
        conn.close()

        # 第 3 版：AST-005 消失（停用）、AST-003 也停用，拆组后 B/P1 子组的
        # 待核对单 T400 无任何同组空余号 -> 同一撤销此刻判定无解
        self.upload(
            "asset",
            csv_bytes(
                "asset_no,pole_source,pole_no",
                [
                    ("AST-001", "LEDGER_A", "P1"),
                    ("AST-002", "LEDGER_B", "P1"),
                ],
            ),
            "v3.csv",
        )
        conn = get_db()
        with self.assertRaises(ValueError):
            services.apply_plan(conn, key, edge)
        conn.close()

        # 再传第 4 版把空余号补回来（AST-003 继续停用，B/P1 仅 AST-006 空余）
        # -> 同样的撤销按新版又可行，且记录第 4 版。
        # 注意换表可能改变组的灯杆构成（P2 无单无资产后掉出组），按业务应重选。
        self.upload(
            "asset",
            csv_bytes(
                "asset_no,pole_source,pole_no",
                [
                    ("AST-001", "LEDGER_A", "P1"),
                    ("AST-002", "LEDGER_B", "P1"),
                    ("AST-006", "LEDGER_B", "P1"),
                ],
            ),
            "v4.csv",
        )
        conn = get_db()
        current = services.list_groups(conn)[0]
        detail = services.get_group_detail(conn, current["group_key"])
        self.assertTrue(detail["options"])  # 新版下重新有解
        result = services.apply_plan(
            conn, current["group_key"], detail["options"][0]["confirmation_id"]
        )
        assigned = {
            item["ticket"]: item["asset_no"]
            for part in result["partitions"]
            for item in part["items"]
        }
        self.assertEqual(assigned["LEDGER_B/T400"], "AST-006")
        ver4 = conn.execute(
            "SELECT id FROM asset_version WHERE filename='v4.csv'"
        ).fetchone()["id"]
        self.assertEqual(result["asset_version_id"], ver4)
        self.assertEqual(
            conn.execute(
                "SELECT asset_version_id FROM warehouse WHERE id=?",
                (result["warehouse_id"],),
            ).fetchone()["asset_version_id"],
            ver4,
        )
        conn.close()

    def test_14_bad_replace_keeps_old_table(self):
        """换表文件有问题：整体拒收，继续用旧表。"""
        self._setup_one_warehoused_group()
        # 文件内资产号重复
        bad = csv_bytes(
            "asset_no,pole_source,pole_no",
            [("AST-001", "LEDGER_A", "P1"), ("AST-001", "LEDGER_B", "P1")],
        )
        r = self.upload("asset", bad, "bad.csv")
        self.assertIn("换表已中止", r.get_data(as_text=True))
        # 空文件同样保留旧表
        r2 = self.upload("asset", "".encode(), "empty.csv")
        self.assertIn("保留旧表", r2.get_data(as_text=True))
        conn = get_db()
        # 旧表原样：仍是 3 个活动资产，且没有新增版本
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) c FROM asset WHERE active=1"
            ).fetchone()["c"],
            3,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM asset_version").fetchone()["c"],
            1,
        )
        # 已核对归属不动
        self.assertEqual(
            conn.execute("SELECT COUNT(*) c FROM verified").fetchone()["c"], 2
        )
        conn.close()

    def test_15b_cross_group_locked_asset_not_reusable(self):
        """跨组占号：已核对单的号改挂到另一组灯杆后，该组待核对单也不能用。"""
        self._setup_one_warehoused_group()  # T100->AST-001(A/P1)、T200->AST-002(B/P1)
        # 换表：AST-002 从 B/P1 改挂到独立灯杆 C/P3（与原组无确认相连）
        self.upload(
            "asset",
            csv_bytes(
                "asset_no,pole_source,pole_no",
                [
                    ("AST-001", "LEDGER_A", "P1"),
                    ("AST-002", "LEDGER_C", "P3"),
                    ("AST-003", "LEDGER_A", "P2"),
                    ("AST-009", "LEDGER_C", "P3"),
                ],
            ),
        )
        # 新冲突组：C/P3 与 D/P3 同杆，待核对单 T500 挂 D/P3
        self.upload(
            "confirm",
            csv_bytes(
                "source_a,pole_no_a,source_b,pole_no_b",
                [("LEDGER_C", "P3", "LEDGER_D", "P3")],
            ),
        )
        self.upload(
            "ticket",
            csv_bytes(
                "ticket_no,source,pole_source,pole_no",
                [("T500", "LEDGER_D", "LEDGER_D", "P3")],
            ),
        )
        conn = get_db()
        groups = services.list_groups(conn)
        target = next(
            g for g in groups if "LEDGER_C/P3" in g["poles"]
        )
        no_by_id = {aid: target["arows"][aid]["asset_no"]
                    for aid in target["asset_ids"]}
        # AST-002 物理挂在本组 C/P3，但被其他组的已核对单 T200 占住
        ast2 = next(i for i, no in no_by_id.items() if no == "AST-002")
        self.assertEqual(target["asset_status"][ast2], "locked")
        spare = {no_by_id[i] for i in target["spare_asset_ids"]}
        self.assertEqual(spare, {"AST-009"})  # 占号 AST-002 不在空余集合
        # 匹配规则：T500 只能拿空余号 AST-009，绝不能拿被占的 AST-002
        # （该组撤销任一确认都会拆散同杆关系导致无候选，故只校验当前视图与匹配）
        self.assertEqual(target["feasible"], True)
        t500_id = next(
            tid for tid in target["ticket_ids"]
            if target["trows"][tid]["ticket_no"] == "T500"
        )
        self.assertEqual(target["assignment"][t500_id], next(
            i for i, no in no_by_id.items() if no == "AST-009"
        ))
        # T200 的原归属纹丝不动
        self.assertEqual(
            conn.execute(
                "SELECT a.asset_no FROM verified v JOIN asset a ON a.id=v.asset_id "
                "JOIN ticket t ON t.id=v.ticket_id WHERE t.ticket_no='T200'"
            ).fetchone()["asset_no"],
            "AST-002",
        )
        conn.close()

    def test_15_legacy_db_upgrade_seeds_first_version(self):
        """旧库（asset 无 active 列、无 asset_version 表）升级后沿用现有清单。"""
        import sqlite3

        legacy = os.path.join(_tmp, "legacy.db")
        if os.path.exists(legacy):
            os.remove(legacy)
        old_conn = sqlite3.connect(legacy)
        old_conn.executescript(
            """
            CREATE TABLE asset (id INTEGER PRIMARY KEY AUTOINCREMENT,
                asset_no TEXT NOT NULL UNIQUE, pole_source TEXT NOT NULL,
                pole_no TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            CREATE TABLE ticket (id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL, ticket_no TEXT NOT NULL,
                pole_source TEXT NOT NULL, pole_no TEXT NOT NULL,
                uploaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                UNIQUE(source, ticket_no));
            CREATE TABLE confirmation (id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_a TEXT NOT NULL, pole_no_a TEXT NOT NULL,
                source_b TEXT NOT NULL, pole_no_b TEXT NOT NULL,
                confirmed_by TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            CREATE TABLE verified (ticket_id INTEGER PRIMARY KEY,
                asset_id INTEGER NOT NULL, group_key TEXT NOT NULL,
                verified_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            CREATE TABLE undo_plan (id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_key TEXT NOT NULL, confirmation_id INTEGER NOT NULL,
                edge_label TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            CREATE TABLE warehouse (id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            CREATE TABLE warehouse_item (warehouse_id INTEGER NOT NULL,
                ticket_id INTEGER NOT NULL, asset_id INTEGER NOT NULL,
                PRIMARY KEY (warehouse_id, ticket_id));
            CREATE TABLE batch (id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL, filename TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            INSERT INTO asset(asset_no, pole_source, pole_no)
                VALUES ('OLD-1','L','P1'),('OLD-2','L','P2');
            INSERT INTO ticket(source, ticket_no, pole_source, pole_no)
                VALUES ('L','TK1','L','P1');
            INSERT INTO verified(ticket_id, asset_id, group_key) VALUES (1,1,'gk');
            INSERT INTO warehouse(group_key) VALUES ('gk');
            INSERT INTO warehouse_item VALUES (1,1,1);
            INSERT INTO undo_plan(group_key, confirmation_id, edge_label)
                VALUES ('gk',1,'x ~ y');
            """
        )
        old_conn.commit()
        old_conn.close()

        os.environ["POLE_RECON_DB"] = legacy
        try:
            conn = get_db()
            init_db(conn)  # 触发迁移
            # 现有清单登记为第 1 版，全部仍活动
            ver = conn.execute("SELECT * FROM asset_version").fetchall()
            self.assertEqual(len(ver), 1)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) c FROM asset WHERE active=1 AND version_id=?",
                    (ver[0]["id"],),
                ).fetchone()["c"],
                2,
            )
            # 旧入仓/撤销记录回填版本号且回查原号不变
            self.assertEqual(
                conn.execute(
                    "SELECT asset_version_id FROM warehouse WHERE group_key='gk'"
                ).fetchone()["asset_version_id"],
                ver[0]["id"],
            )
            self.assertEqual(
                conn.execute(
                    "SELECT a.asset_no FROM warehouse_item wi "
                    "JOIN asset a ON a.id=wi.asset_id"
                ).fetchone()["asset_no"],
                "OLD-1",
            )
            # 迁移后可直接在旧库上换表
            conn.close()
            msg = services.import_assets(
                csv_bytes(
                    "asset_no,pole_source,pole_no",
                    [("OLD-1", "L", "P1"), ("OLD-3", "L", "P3")],
                ),
                "after-upgrade.csv",
            )
            self.assertIn("停用 1 个", msg)
            conn = get_db()
            self.assertEqual(
                conn.execute(
                    "SELECT active FROM asset WHERE asset_no='OLD-2'"
                ).fetchone()["active"],
                0,
            )
            conn.close()
        finally:
            os.environ["POLE_RECON_DB"] = os.path.join(_tmp, "test.db")


if __name__ == "__main__":
    unittest.main(verbosity=2)
