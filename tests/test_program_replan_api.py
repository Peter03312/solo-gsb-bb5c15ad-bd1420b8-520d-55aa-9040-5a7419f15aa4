"""培养方案达成课表执行中途滚动重排：HTTP API 集成测试（独立临时数据库）。

覆盖：
- 已确认达成课表重排草案（最早达成 + 字典序、累计已修、可重选拟修课与选修归属、
  与原剩余课表差异、预计归属）；
- 已达成时草案的未来课表为空；
- 草案不预占席位；确认沿用 POST /plans/{id}/confirm：原子复核目录与席位、
  仅替换来源方案未来占位、同一来源至多一个生效重排、重复确认幂等；
- 拒绝路径：来源不是已确认达成课表、已结束学期数越界、已修回退、未知已修课、
  期限内无解、路径要求版本不符；
- 普通规划重排与审核等既有行为保持兼容。
"""
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _equiv_catalog(client, groups=(["CS301", "CS302"], ["CS401", "CS402"])):
    current = client.get("/catalog/current").json()
    r = client.post("/catalog/versions", json={
        "note": "互认目录", "courses": current["courses"],
        "equivalence_groups": [list(g) for g in groups]})
    assert r.status_code == 201, r.text
    return r.json()["version_id"]


def _register(client, vid, program="CS-2024", **kw):
    body = {
        "program_code": program,
        "catalog_version_id": vid,
        "required_courses": ["CS101", "CS201", "CS301", "CS401"],
        "elective_groups": [
            {"name": "专业选修", "candidates": ["CS303", "CS402"], "min_credits": 3},
            {"name": "基础拓展", "candidates": ["MATH101", "MATH103"], "min_credits": 6},
        ],
    }
    body.update(kw)
    r = client.post(f"/programs/{program}/requirement-versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()["requirement_version_id"]


def _plan_body(**kw):
    body = {
        "completed_courses": ["CS101", "CS101L", "CS201", "MATH101", "MATH103"],
        "start_semester": "2026-SPRING",
        "max_credits_per_semester": 12,
        "num_semesters": 6,
    }
    body.update(kw)
    return body


BASE_COMPLETED = ["CS101", "CS101L", "CS201", "MATH101", "MATH103"]
# 执行 2 学期后，原课表（2026-SPRING CS301/CS303、2026-FALL CS401）均按计划修完
ON_TRACK_COMPLETED = BASE_COMPLETED + ["CS301", "CS303"]


def _confirmed_program_plan(client, rv_id, **kw):
    r = client.post(f"/requirement-versions/{rv_id}/plans", json=_plan_body(**kw))
    assert r.status_code == 201, r.text
    plan = r.json()
    assert client.post(f"/plans/{plan['plan_id']}/confirm").status_code == 200
    return plan


def _replan(client, plan_id, elapsed, completed, rv_id=None):
    url = (f"/requirement-versions/{rv_id}/plans/{plan_id}/replan" if rv_id
           else f"/plans/{plan_id}/replan")
    return client.post(url, json={
        "elapsed_semesters": elapsed, "completed_courses": completed})


# ---------------------------------------------------------------- 成功路径

def test_program_replan_creates_draft_with_schedule_attribution_and_diff(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)

    r = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED)
    assert r.status_code == 201, r.text
    draft = r.json()

    # 草案形态：关联来源、draft、沿用要求版本与学分上限/剩余期限
    assert draft["status"] == "draft"
    assert draft["source_plan_id"] == plan["plan_id"]
    assert draft["plan_type"] == "program_plan"
    assert draft["requirement_version_id"] == rv_id
    assert draft["catalog_version"] == vid
    assert draft["bound_catalog_version"] == vid
    req = draft["request"]
    assert req["start_semester"] == "2027-SPRING"
    assert req["num_semesters"] == 4
    assert req["max_credits_per_semester"] == 12
    assert req["plan_type"] == "program_plan"
    assert req["requirement_version_id"] == rv_id
    # 未来按当前目录重排，已修课不重复安排
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [[], ["CS401"]]
    for code in ON_TRACK_COMPLETED:
        assert all(code not in sem for sem in sems)

    # 预计归属：合并累计已修与拟修，全部达成
    res = draft["attribution"]["result"]
    assert res["achieved"] is True
    assert res["unsatisfied_required"] == []
    by = {q["code"]: q for q in res["required"]}
    assert by["CS401"]["attributed_course"]["code"] == "CS401"

    # 与原剩余学期（跳过前 2 学期；原未来仅 2027-FALL 无安排——原 CS401 在第 2 学期）
    # 新课表在 2027-FALL 增排 CS401
    diff = draft["replan"]["difference"]
    assert diff["semesters"] == [
        {"semester": "2027-FALL", "added_courses": ["CS401"], "removed_courses": []}]
    assert diff["added_courses"] == ["CS401"]
    assert diff["removed_courses"] == []

    # 原方案保持不变、仍为 confirmed
    orig = client.get(f"/plans/{plan['plan_id']}").json()
    assert orig["status"] == "confirmed"
    assert "replan" not in orig

    # 拟修归属不写成历史审核
    assert client.get("/audits").json()["audits"] == []


def test_program_replan_may_reselect_elective_when_planned_course_not_finished(client):
    """原计划第 1 学期的选修 CS303 未修：未来可重选拟修课，CS303 顺延排入未来，
    差异中自原未来课表移除的课程与新增课程均给出。"""
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)

    # CS303（专业选修）未修完；CS301 已修
    r = _replan(client, plan["plan_id"], 2, BASE_COMPLETED + ["CS301"])
    assert r.status_code == 201, r.text
    draft = r.json()
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    # CS303 仅 SPRING：2027-SPRING 补修；CS401 仍为 2027-FALL
    assert sems == [["CS303"], ["CS401"]]
    assert draft["result"]["completion_semester"] == "2027-FALL"

    res = draft["attribution"]["result"]
    groups = {g["name"]: g for g in res["elective_groups"]}
    assert [c["code"] for c in groups["专业选修"]["attributed_courses"]] == ["CS303"]
    assert groups["基础拓展"]["counted_credits"] == 6
    assert res["achieved"] is True

    # 差异：原未来学期无安排；新课表在 2027-SPRING 增 CS303、2027-FALL 增 CS401
    diff = draft["replan"]["difference"]
    by_sem = {s["semester"]: s for s in diff["semesters"]}
    assert by_sem["2027-SPRING"]["added_courses"] == ["CS303"]
    assert by_sem["2027-FALL"]["added_courses"] == ["CS401"]


def test_program_replan_equivalence_reattribution(client):
    """原按 CS301 拟修（已结束学期占位保留），累计已修为互认课 CS302：
    归属按绑定目录互认重新计算，未来不重复安排该必修单元。"""
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)  # 原课表含 CS301@2026-SPRING

    r = _replan(client, plan["plan_id"], 2, BASE_COMPLETED + ["CS302", "CS303"])
    assert r.status_code == 201, r.text
    draft = r.json()
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [[], ["CS401"]]
    res = draft["attribution"]["result"]
    by = {q["code"]: q for q in res["required"]}
    attr = by["CS301"]["attributed_course"]
    assert attr["code"] == "CS302" and attr["via"] == "equivalence"
    assert res["achieved"] is True


def test_program_replan_already_achieved_has_empty_future_schedule(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # CS302/CS402 经互认满足必修 CS301/CS401，CS303 满足专业选修
    done = BASE_COMPLETED + ["CS302", "CS402", "CS303"]

    r = _replan(client, rv_id=rv_id, plan_id=plan["plan_id"], elapsed=2, completed=done)
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["status"] == "draft"
    assert draft["source_plan_id"] == plan["plan_id"]
    assert draft["result"]["semesters"] == []
    assert draft["result"]["completion_semester"] is None
    assert draft["request"]["target_courses"] == []
    assert draft["attribution"]["result"]["achieved"] is True
    # 差异为空（原未来学期本就无课）
    diff = draft["replan"]["difference"]
    assert diff["semesters"] == []
    assert diff["added_courses"] == [] and diff["removed_courses"] == []


def test_empty_future_draft_confirm_releases_future_holds_keeps_elapsed(client):
    """已达成时空未来课表草案确认：释放来源“未来学期”占位，已结束学期占位保留。"""
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)  # CS401@2026-FALL（序号 2）
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    # elapsed=1：CS401@2026-FALL（序号 2 > 1）属于未来占位；累计已修已全部达成
    # CS402 归必修 CS401，专业选修由 CS303 计入，CS302 互认 CS301
    done = sorted(set(BASE_COMPLETED + ["CS301", "CS302", "CS402", "CS303"]))
    r = _replan(client, plan["plan_id"], 1, done)
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["result"]["semesters"] == []
    assert draft["attribution"]["result"]["achieved"] is True
    # 确认前来源占位仍在
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    # 未来占位被空课表替换而释放
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 0
    # 重复确认幂等
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 0


def test_program_replan_uses_current_catalog_after_revision(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 目录修订：CS401 学分 3 -> 4（要求版本仍绑定旧版本，归属学分按绑定版本）
    current = client.get("/catalog/current").json()["courses"]
    for c in current:
        if c["code"] == "CS401":
            c["credits"] = 4
    new_vid = client.post("/catalog/versions",
                          json={"note": "v3", "courses": current,
                                "equivalence_groups": [["CS301", "CS302"], ["CS401", "CS402"]]}
                          ).json()["version_id"]

    r = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED)
    assert r.status_code == 201, r.text
    draft = r.json()
    assert draft["catalog_version"] == new_vid
    assert draft["bound_catalog_version"] == vid
    fall = draft["result"]["semesters"][1]
    assert fall["semester"] == "2027-FALL"
    assert fall["total_credits"] == 4  # 课表学分按当前目录
    # 归属学分仍按绑定目录（CS401=3）
    res = draft["attribution"]["result"]
    by = {q["code"]: q for q in res["required"]}
    assert by["CS401"]["attributed_course"]["credits"] == 3


# ---------------------------------------------------------------- 席位与确认

def test_program_replan_draft_does_not_prehold_seats(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1

    r = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED)
    draft = r.json()
    # 草案未来 CS401@2027-FALL 不预占；来源占位原样保留
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 0
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1

    # 确认：仅替换未来占位，已结束学期占位保留
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1
    # 重复确认幂等、不重复占位
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    assert client.get("/seat-caps/CS401/2027-FALL").json()["occupied"] == 1


def test_program_replan_confirm_atomically_checks_seats_and_catalog(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    draft = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED).json()
    # 草案生成后封掉新课表学期席位
    client.put("/seat-caps/CS401/2027-FALL", json={"cap": 0})
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SEAT_CONFLICT"
    # 草案仍 draft，原占位保留（事务回滚）
    assert client.get(f"/plans/{draft['plan_id']}").json()["status"] == "draft"
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1

    # 目录修订后确认 -> CATALOG_CHANGED
    current = client.get("/catalog/current").json()["courses"]
    client.post("/catalog/versions", json={"note": "v3", "courses": current})
    r = client.post(f"/plans/{draft['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"


def test_only_one_active_program_replan_per_source(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    d1 = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED).json()
    d2 = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED).json()
    assert client.post(f"/plans/{d1['plan_id']}/confirm").status_code == 200
    r = client.post(f"/plans/{d2['plan_id']}/confirm")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "DRAFT_EXPIRED"
    # 已生效草案重复确认幂等
    assert client.post(f"/plans/{d1['plan_id']}/confirm").status_code == 200


def test_program_replan_planning_ignores_own_unfinished_holds(client):
    """来源方案自身占满某未来学期时，重排排回该学期仍可行（自身占位确认时替换）。"""
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    # 原课表：2026-SPRING CS301/CS303（第 1 学期）、CS401@2026-FALL（第 2 学期）
    plan = _confirmed_program_plan(client, rv_id)
    client.put("/seat-caps/CS401/2026-FALL", json={"cap": 1})
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1
    # 仅结束 1 学期：CS401@2026-FALL（序号 2 > 1）是来源“未来占位”；
    # 重排仍把 CS401 排回 2026-FALL，不与自身占位冲突
    elapsed_done = BASE_COMPLETED + ["CS301", "CS303"]
    r = _replan(client, plan["plan_id"], 1, elapsed_done)
    assert r.status_code == 201, r.text
    draft = r.json()
    sems = [[c["code"] for c in s["courses"]] for s in draft["result"]["semesters"]]
    assert sems == [["CS401"]]
    assert draft["result"]["completion_semester"] == "2026-FALL"
    assert client.post(f"/plans/{draft['plan_id']}/confirm").status_code == 200
    # 替换后不叠加占用
    assert client.get("/seat-caps/CS401/2026-FALL").json()["occupied"] == 1


def test_child_replan_off_confirmed_program_draft(client):
    """对已生效的达成课表重排结果可继续重排（链式），同一来源各自至多一个生效重排。"""
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    d1 = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED).json()
    assert client.post(f"/plans/{d1['plan_id']}/confirm").status_code == 200
    # 以已生效草案为来源再重排（草案起始 2027-SPRING，elapsed=1 -> 2027-FALL 起）
    r = _replan(client, d1["plan_id"], 1, ON_TRACK_COMPLETED)
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["source_plan_id"] == d1["plan_id"]
    assert child["request"]["start_semester"] == "2027-FALL"
    sems = [[c["code"] for c in s["courses"]] for s in child["result"]["semesters"]]
    assert sems == [["CS401"]]
    assert child["attribution"]["result"]["achieved"] is True
    assert client.post(f"/plans/{child['plan_id']}/confirm").status_code == 200


# ---------------------------------------------------------------- 拒绝路径

def test_program_replan_source_not_found_404(client):
    r = _replan(client, "nope123", 1, [])
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize("status", ["proposed", "draft"])
def test_program_replan_requires_confirmed_plan(client, status):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = client.post(f"/requirement-versions/{rv_id}/plans", json=_plan_body()).json()
    if status == "draft":
        # 先确认另一个方案，再对它重排出草案供测试
        other = _confirmed_program_plan(client, rv_id)
        plan = _replan(client, other["plan_id"], 2, ON_TRACK_COMPLETED).json()
    r = _replan(client, plan["plan_id"], 1, [])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PLAN_NOT_CONFIRMED"


def test_program_replan_rejects_normal_plan_on_strict_endpoint(client):
    """显式入口只接受培养方案达成课表；普通已确认方案走该入口返回 409。"""
    plan = client.post("/plans", json={
        "completed_courses": [], "target_courses": ["CS401"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 6}).json()
    client.post(f"/plans/{plan['plan_id']}/confirm")
    r = _replan(client, plan["plan_id"], 2,
                ["CS101", "CS101L", "CS201", "MATH103"], rv_id="CS-2024:v1")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PLAN_NOT_CONFIRMED"
    # 通用入口仍可按普通规划重排
    assert _replan(client, plan["plan_id"], 2,
                   ["CS101", "CS101L", "CS201", "MATH103"]).status_code == 201


def test_program_replan_elapsed_out_of_range(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 非正：请求层 422
    r = _replan(client, plan["plan_id"], 0, ON_TRACK_COMPLETED)
    assert r.status_code == 422
    # 不小于原规划学期数：业务层 400
    r = _replan(client, plan["plan_id"], 6, ON_TRACK_COMPLETED)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "INVALID_INPUT"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_program_replan_completed_superset_of_source_declared(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 撤销来源申请时声明的 CS101L
    revoked_done = ["CS101", "CS201", "MATH101", "MATH103", "CS301", "CS303"]
    r = _replan(client, plan["plan_id"], 2, revoked_done)
    assert r.status_code == 422
    body = r.json()["detail"]
    assert body["code"] == "COMPLETED_REVOKED"
    assert "CS101L" in body["message"]
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_program_replan_unknown_completed_course_rejected(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    r = _replan(client, plan["plan_id"], 2, BASE_COMPLETED + ["NOPE999"])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "COURSE_MISSING"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_program_replan_no_solution_saves_nothing(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    # 仅剩 1 学期（2028-SPRING）：CS401 仅 FALL，无法在学期限内达成
    r = _replan(client, plan["plan_id"], 5, BASE_COMPLETED)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NO_SOLUTION"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


def test_strict_endpoint_rejects_mismatched_requirement_version(client):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)
    r = client.post(f"/requirement-versions/OTHER:v9/plans/{plan['plan_id']}/replan",
                    json={"elapsed_semesters": 2, "completed_courses": ON_TRACK_COMPLETED})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "NOT_FOUND"
    # 未知要求版本入口
    r = client.post(f"/requirement-versions/NOPE:v1/plans/{plan['plan_id']}/replan",
                    json={"elapsed_semesters": 2, "completed_courses": ON_TRACK_COMPLETED})
    assert r.status_code == 404


def test_program_replan_rejects_stale_result_on_concurrent_catalog_revision(
        client, monkeypatch):
    vid = _equiv_catalog(client)
    rv_id = _register(client, vid)
    plan = _confirmed_program_plan(client, rv_id)

    from app import main as main_mod
    original_get_current = main_mod.db.get_current_version
    calls = {"n": 0}

    def flaky(conn):
        row = original_get_current(conn)
        calls["n"] += 1
        if calls["n"] == 2:  # 引擎读第 1 次；落库前复查第 2 次
            return dict(row) | {"id": row["id"] + 1}
        return row

    monkeypatch.setattr(main_mod.db, "get_current_version", flaky)
    r = _replan(client, plan["plan_id"], 2, ON_TRACK_COMPLETED)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CATALOG_CHANGED"
    assert all(p["status"] != "draft" for p in client.get("/plans").json())


# ---------------------------------------------------------------- 兼容回归

def test_normal_planning_replan_and_audit_remain_compatible(client):
    vid = _equiv_catalog(client)
    # 普通规划 + 普通重排：响应不带 attribution/plan_type
    plan = client.post("/plans", json={
        "completed_courses": [], "target_courses": ["CS401"],
        "start_semester": "2026-SPRING", "max_credits_per_semester": 12,
        "num_semesters": 6}).json()
    assert "attribution" not in plan and "plan_type" not in plan
    client.post(f"/plans/{plan['plan_id']}/confirm")
    draft = _replan(client, plan["plan_id"], 2,
                    ["CS101", "CS101L", "CS201", "MATH103"]).json()
    assert "attribution" not in draft and "plan_type" not in draft
    assert draft["status"] == "draft"

    # 审核接口行为不变
    rv_id = _register(client, vid)
    audit = client.post(f"/requirement-versions/{rv_id}/audits",
                        json={"completed_courses": ["CS101"]})
    assert audit.status_code == 201
