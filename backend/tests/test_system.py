"""系统管理（PRD 4.13）测试。

覆盖检测规则版本的完整生命周期（新建 → 生效确认 → 旧版本归档且历史保留）、
操作日志可查询、用户管理，以及非管理员的授权控制。
"""

from sqlalchemy import select

from app.core.config import settings
from app.models import DetectionRuleVersion, User
from app.models.enums import RuleStatus, TaskStatus, UserRole
from app.services import rule_service, system_service


def _login(client, username="admin", password="admin123"):
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_ensure_initial_version_is_idempotent(db):
    created = rule_service.ensure_initial_version(db)
    db.commit()
    assert len(created) == 4
    # 再次调用不应重复创建
    again = rule_service.ensure_initial_version(db)
    db.commit()
    assert again == []
    versions = list(db.scalars(select(DetectionRuleVersion)))
    assert len(versions) == 4
    assert {version.dimension for version in versions} == {
        "text",
        "personnel",
        "price",
        "ocr",
    }
    assert all(version.status == RuleStatus.ACTIVE.value for version in versions)


def test_rule_version_lifecycle_preserves_history(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    before = rule_service.active_version(db, "text")
    original_params = dict(before.params)

    draft = rule_service.create_version(
        db, "text", {"text_similarity": 0.8}, "调高相似度阈值", activate=False
    )
    db.commit()
    assert draft.status == RuleStatus.DRAFT.value
    assert draft.previous_params == original_params
    # 新建草稿不改变当前生效版本
    assert rule_service.active_version(db, "text").id == before.id

    rule_service.activate(db, draft.id)
    db.commit()
    active = rule_service.active_version(db, "text")
    assert active.id == draft.id
    assert active.status == RuleStatus.ACTIVE.value
    assert active.activated_at is not None
    # 旧版本转为历史版本，但记录与参数都保留
    db.refresh(before)
    assert before.status == RuleStatus.ARCHIVED.value
    assert before.params == original_params


def test_text_rule_rejects_invalid_algorithm_params(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    for params in (
        {"text_shingle_size": 0},
        {"text_minhash_num_perm": 4},
        {"text_lsh_threshold": 1.5},
        {"text_chunk_target_min": 900, "text_chunk_target_max": 500},
    ):
        try:
            rule_service.create_version(db, "text", params, "非法参数")
        except rule_service.RuleError:
            continue
        raise AssertionError(f"应当拒绝非法参数：{params}")


def test_engine_upgrade_creates_new_active_text_version(db):
    """旧版规则（8-gram、无 MinHash 参数）升级时新建版本并保留历史。"""
    rule_service.ensure_initial_version(db)
    db.commit()
    legacy = rule_service.active_version(db, "text")
    legacy.params = {
        "text_similarity": 0.6,
        "text_min_match_chars": 40,
        "text_min_block_chars": 30,
        "text_shingle_size": 8,
        "max_candidate_pairs": 20000,
        "max_matches_per_pair": 200,
    }
    db.commit()

    upgraded = rule_service.ensure_engine_version(db)
    db.commit()
    assert upgraded is not None
    assert upgraded.status == RuleStatus.ACTIVE.value
    assert upgraded.params["text_shingle_size"] == settings.text_shingle_size
    assert upgraded.params["text_minhash_num_perm"] == settings.text_minhash_num_perm
    db.refresh(legacy)
    assert legacy.status == RuleStatus.ARCHIVED.value

    # 已经升级过的规则不再重复创建版本
    assert rule_service.ensure_engine_version(db) is None


def test_resolve_thresholds_keeps_legacy_shape(db):
    rule_service.ensure_initial_version(db)
    db.commit()
    thresholds = rule_service.resolve_thresholds(rule_service.active_snapshot(db))
    assert thresholds["text_similarity"] == settings.text_similarity_threshold
    assert thresholds["price_cv"] == settings.price_cv_threshold
    assert thresholds["price_close_deviation"] == settings.price_close_deviation
    assert thresholds["price_sequence_tolerance"] == settings.price_sequence_tolerance
    # 缺失快照时回落到部署配置
    assert rule_service.resolve_thresholds({})["text_similarity"] == settings.text_similarity_threshold


def test_task_records_rule_version_snapshot(db):
    """分析任务把启动时的规则版本固化下来。"""
    from app.models import AnalysisTask

    rule_service.ensure_initial_version(db)
    db.commit()
    task = AnalysisTask(
        project_id="project-id",
        task_no=1,
        status=TaskStatus.QUEUED.value,
        analysis_config={},
        company_snapshot={"companies": []},
        algorithm_version="test",
        progress={},
    )
    db.add(task)
    db.commit()

    snapshot = rule_service.active_snapshot(db)
    thresholds = rule_service.resolve_thresholds(snapshot)
    task.analysis_config = {
        "dimensions": ["text"],
        "thresholds": thresholds,
        "rule_versions": snapshot,
    }
    db.commit()
    db.refresh(task)

    assert task.analysis_config["rule_versions"]["text"]["version_no"] == 1
    assert task.analysis_config["thresholds"]["text_similarity"] == settings.text_similarity_threshold


def test_operation_log_records_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        response = client.get("/api/v1/system/logs?action=auth.login", headers=headers)
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] >= 1
        entry = payload["items"][0]
        assert entry["action"] == "auth.login"
        assert entry["action_label"] == "登录系统"
        assert entry["result"] == "success"
        assert entry["source"] == "web"


def test_log_filter_treats_legacy_payload_as_success(db):
    """历史日志 payload 没有 result/source 时，过滤应与展示保持一致。"""
    from app.models import AuditLog

    legacy = AuditLog(
        actor_name="旧用户",
        action="project.create",
        entity_type="project",
        entity_id="p1",
        payload={"name": "旧项目"},  # 没有 result / source
    )
    db.add(legacy)
    db.commit()

    success = system_service.list_logs(db, result="success")
    assert success["total"] == 1
    assert success["items"][0]["result"] == "success"

    web = system_service.list_logs(db, source="web")
    assert web["total"] == 1

    failed = system_service.list_logs(db, result="failed")
    assert failed["total"] == 0


def test_log_pagination_total_reflects_filter(db):
    from app.models import AuditLog

    for index in range(5):
        db.add(
            AuditLog(
                actor_name="系统",
                action="project.create",
                entity_type="project",
                entity_id=f"p{index}",
                payload={"result": "failed" if index % 2 else "success", "source": "web"},
            )
        )
    db.commit()

    page = system_service.list_logs(db, page=1, page_size=2)
    assert page["total"] == 5
    assert len(page["items"]) == 2

    filtered = system_service.list_logs(db, result="failed", page=1, page_size=2)
    assert filtered["total"] == 2
    assert len(filtered["items"]) == 2
    assert all(item["result"] == "failed" for item in filtered["items"])


def test_rule_version_endpoints_require_admin():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        # 先造一个非管理员账号
        created = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "analyst1",
                "employee_no": "A001",
                "password": "analyst123",
                "display_name": "分析员",
                "role": "user",
            },
        )
        assert created.status_code == 201, created.text
        analyst_headers = _login(client, "A001", "analyst123")

        forbidden = client.post(
            "/api/v1/system/rules/versions",
            headers=analyst_headers,
            json={"dimension": "text", "params": {}, "change_reason": "越权尝试"},
        )
        assert forbidden.status_code == 403

        # 规则读取对所有登录用户开放
        allowed = client.get("/api/v1/system/rules", headers=analyst_headers)
        assert allowed.status_code == 200
        # 操作日志仅管理员可见
        assert client.get("/api/v1/system/logs", headers=analyst_headers).status_code == 403


def test_rule_activation_requires_confirmation():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        created = client.post(
            "/api/v1/system/rules/versions",
            headers=headers,
            json={
                "dimension": "price",
                "params": {"price_cv": 0.08},
                "change_reason": "调整变异系数阈值",
            },
        )
        assert created.status_code == 201, created.text
        version = created.json()
        assert version["status"] == "draft"
        assert version["changed"][0]["key"] == "price_cv"
        assert version["changed"][0]["before"] == settings.price_cv_threshold

        # 未确认不能生效
        not_confirmed = client.post(
            f"/api/v1/system/rules/versions/{version['id']}/activate",
            headers=headers,
            json={"confirmed": False},
        )
        assert not_confirmed.status_code == 400
        assert "生效确认" in not_confirmed.json()["detail"]

        activated = client.post(
            f"/api/v1/system/rules/versions/{version['id']}/activate",
            headers=headers,
            json={"confirmed": True},
        )
        assert activated.status_code == 200
        assert activated.json()["status"] == "active"


def test_user_management_lifecycle():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        created = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "reviewer1",
                "email": "reviewer1@example.com",
                "password": "review123",
                "display_name": "审核员",
                "role": "user",
            },
        )
        assert created.status_code == 201, created.text
        user_id = created.json()["id"]

        # 新账号用工号/邮箱登录（用户名不作为登录凭证）
        _login(client, "reviewer1@example.com", "review123")

        # 禁用后不能登录
        disabled = client.patch(
            f"/api/v1/system/users/{user_id}", headers=headers, json={"is_active": False}
        )
        assert disabled.status_code == 200
        assert disabled.json()["is_active"] is False
        blocked = client.post(
            "/api/v1/auth/login",
            json={"username": "reviewer1@example.com", "password": "review123"},
        )
        assert blocked.status_code == 401

        # 重置密码
        reset = client.post(
            f"/api/v1/system/users/{user_id}/password",
            headers=headers,
            json={"password": "newpass123"},
        )
        assert reset.status_code == 200
        client.patch(f"/api/v1/system/users/{user_id}", headers=headers, json={"is_active": True})
        _login(client, "reviewer1@example.com", "newpass123")


def test_admin_cannot_disable_own_account():
    """管理员不能禁用自己，避免把系统锁死。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        me = client.get("/api/v1/auth/me", headers=headers).json()
        response = client.patch(
            f"/api/v1/system/users/{me['id']}", headers=headers, json={"is_active": False}
        )
        assert response.status_code == 400
        assert "自己" in response.json()["detail"]


def test_last_admin_cannot_be_disabled(db):
    """系统至少要保留一个启用的管理员账号。"""
    from app.core.security import hash_password

    other = User(
        username="admin2",
        employee_no="ADM002",
        password_hash=hash_password("x"),
        display_name="第二管理员",
        role=UserRole.ADMIN.value,
        is_active=True,
    )
    db.add(other)
    db.commit()

    try:
        system_service.update_user(db, other, is_active=False, actor=None)
    except system_service.SystemError as exc:
        assert "管理员" in str(exc)
    else:
        raise AssertionError("禁用最后一个管理员应当被拒绝")


def test_user_service_rejects_duplicate_employee_no(db):
    existing = User(
        username="dup",
        employee_no="D001",
        password_hash="x",
        display_name="重复",
        role=UserRole.USER.value,
        is_active=True,
    )
    db.add(existing)
    db.commit()
    # 同名允许
    system_service.create_user(db, "dup", "pass123", "重复", "user", employee_no="D002")
    # 同工号拒绝
    try:
        system_service.create_user(db, "other", "pass123", "另一个", "user", employee_no="D001")
    except system_service.SystemError as exc:
        assert "已被其他账号使用" in str(exc)
    else:
        raise AssertionError("重复工号应当被拒绝")


def test_rule_thresholds_actually_affect_detection():
    """规则版本里的阈值必须真正作用到检测引擎上（否则版本页只是摆设）。"""
    from app.analysis.sources import BlockRef
    from app.analysis.text_similarity import build_text_evidences, find_text_matches

    text = "我方将严格按照招标文件要求组织施工，合理配置人员设备，确保工程按期保质完成。" * 3

    def block(company_id, company_name, block_id):
        return BlockRef(
            company_id=company_id,
            company_name=company_name,
            file_id=f"{company_id}-file",
            file_name=f"{company_name}.pdf",
            file_type="pdf",
            file_version=1,
            page_index=1,
            printed_page_number=None,
            block_id=block_id,
            block_index=0,
            content=text,
            normalized=text,
            block_type="paragraph",
        )

    blocks_a = [block("c1", "甲公司", "b1")]
    blocks_b = [block("c2", "乙公司", "b2")]

    # 默认阈值：能召回相似内容
    default_matches = find_text_matches(blocks_a, blocks_b)
    assert default_matches
    assert build_text_evidences(default_matches, None)

    # 提高最小匹配字数阈值后，同样的内容不再满足条件
    strict = find_text_matches(
        blocks_a, blocks_b, thresholds={"text_min_match_chars": 100000}
    )
    assert strict == []

    # 招标公共内容排除阈值来自规则版本
    from app.utils.text import shingle_hashes

    tender = shingle_hashes(text, settings.text_shingle_size)
    excluded = build_text_evidences(default_matches, tender, thresholds={"tender_exclusion_ratio": 0.5})
    assert all(draft.evidence_data["excluded_by_tender"] for draft in excluded)
    assert all(draft.evidence_data["exclusion_ratio"] == 1.0 for draft in excluded)
    # 命中占比为 1.0，阈值高于 1.0 时不应判为公共内容
    not_excluded = build_text_evidences(default_matches, tender, thresholds={"tender_exclusion_ratio": 1.5})
    assert not any(draft.evidence_data["excluded_by_tender"] for draft in not_excluded)


def test_price_thresholds_come_from_rule_snapshot():
    """价格异常规则使用任务快照里的阈值。"""
    from app.analysis.price import Quote, analyze_prices
    from app.analysis.sources import BlockRef

    def quote(amount, name):
        block = BlockRef(
            company_id="c",
            company_name=name,
            file_id="f",
            file_name="q.pdf",
            file_type="pdf",
            file_version=1,
            page_index=1,
            printed_page_number=None,
            block_id="b",
            block_index=0,
            content="",
            normalized="",
            block_type="paragraph",
        )
        return Quote(
            company_id="c",
            company_name=name,
            amount=amount,
            raw=str(amount),
            upper=None,
            block=block,
            start=0,
            end=0,
        )

    quotes = [quote(1000000, "甲"), quote(1000100, "乙"), quote(1000200, "丙")]
    rules = {rule.rule for rule in analyze_prices(quotes)}
    assert "high_concentration" in rules

    # 把变异系数阈值调到极低后，同样的报价不再触发集中规则
    relaxed = {
        rule.rule for rule in analyze_prices(quotes, thresholds={"price_cv": 0.0})
    }
    assert "high_concentration" not in relaxed


# --------------------------------------------------------------------------- 身份字段与两种角色


def test_create_user_requires_employee_no_or_email():
    """工号与邮箱必须至少填一个。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        neither = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={"username": "no_identity", "password": "pass1234", "role": "user"},
        )
        assert neither.status_code == 400
        assert "至少" in neither.json()["detail"]

        only_employee = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "emp_only",
                "employee_no": "E1001",
                "password": "pass1234",
                "role": "user",
            },
        )
        assert only_employee.status_code == 201, only_employee.text
        assert only_employee.json()["employee_no"] == "E1001"

        only_email = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "mail_only",
                "email": "Mail.Only@Example.com",
                "password": "pass1234",
                "role": "user",
            },
        )
        assert only_email.status_code == 201, only_email.text
        # 邮箱统一小写存储
        assert only_email.json()["email"] == "mail.only@example.com"


def test_login_accepts_employee_no_and_email_only():
    """登录只认工号与邮箱；用户名仅作展示，允许重复，不能登录。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "three_keys",
                "employee_no": "E2002",
                "email": "three@example.com",
                "password": "pass1234",
                "role": "user",
            },
        )
        for account in ("E2002", "three@example.com", "THREE@example.com"):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": account, "password": "pass1234"},
            )
            assert response.status_code == 200, f"{account}: {response.text}"
            assert response.json()["user"]["username"] == "three_keys"

        by_username = client.post(
            "/api/v1/auth/login",
            json={"username": "three_keys", "password": "pass1234"},
        )
        assert by_username.status_code == 401

        wrong = client.post(
            "/api/v1/auth/login", json={"username": "E2002", "password": "nope"}
        )
        assert wrong.status_code == 401


def test_identity_fields_are_unique():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "u1",
                "employee_no": "E3003",
                "email": "dup@example.com",
                "password": "pass1234",
            },
        )
        # 用户名允许重复（同名同事是常态）
        same_name = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={"username": "u1", "employee_no": "E3004", "password": "pass1234"},
        )
        assert same_name.status_code == 201, same_name.text

        for payload, label in (
            ({"username": "u2", "employee_no": "E3003"}, "工号"),
            ({"username": "u3", "email": "dup@example.com"}, "邮箱"),
            ({"username": "u4", "email": "DUP@example.com"}, "邮箱"),
        ):
            response = client.post(
                "/api/v1/system/users",
                headers=headers,
                json={"password": "pass1234", **payload},
            )
            assert response.status_code == 400, response.text
            assert label in response.json()["detail"]


def test_login_credentials_are_unique_across_fields():
    """工号与邮箱都能登录，二者之间不能重名，否则登录结果取决于查询顺序。"""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        with_mail = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={"username": "beta", "email": "beta@example.com", "password": "pass1234"},
        ).json()
        # 把别人的邮箱当成自己的工号
        clash = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "gamma",
                "employee_no": "beta@example.com",
                "password": "pass1234",
            },
        )
        assert clash.status_code == 400
        assert "已被其他账号使用" in clash.json()["detail"]
        assert with_mail["email"] == "beta@example.com"

        # 用户名与别人的工号同名不冲突（用户名不参与登录）
        shadow = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={"username": "beta@example.com", "employee_no": "E6006", "password": "pass1234"},
        )
        assert shadow.status_code == 201, shadow.text


def test_update_user_identity_fields():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        created = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={"username": "editable", "employee_no": "E4004", "password": "pass1234"},
        ).json()
        user_id = created["id"]

        # 三项都能改
        updated = client.patch(
            f"/api/v1/system/users/{user_id}",
            headers=headers,
            json={
                "username": "editable2",
                "employee_no": "E4005",
                "email": "editable@example.com",
            },
        )
        assert updated.status_code == 200, updated.text
        body = updated.json()
        assert body["username"] == "editable2"
        assert body["employee_no"] == "E4005"
        assert body["email"] == "editable@example.com"

        # 改后用新工号登录；用户名不参与登录
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "E4005", "password": "pass1234"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "editable2", "password": "pass1234"},
            ).status_code
            == 401
        )

        # 清空工号后仍有邮箱，允许
        cleared = client.patch(
            f"/api/v1/system/users/{user_id}", headers=headers, json={"employee_no": None}
        )
        assert cleared.status_code == 200
        assert cleared.json()["employee_no"] is None

        # 再把邮箱也清空就违反「至少一项」
        denied = client.patch(
            f"/api/v1/system/users/{user_id}", headers=headers, json={"email": None}
        )
        assert denied.status_code == 400
        assert "至少" in denied.json()["detail"]


def test_roles_are_only_admin_and_user():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        headers = _login(client)
        for bad_role in ("analyst", "reviewer", "superuser"):
            response = client.post(
                "/api/v1/system/users",
                headers=headers,
                json={
                    "username": f"bad_{bad_role}",
                    "employee_no": f"R{bad_role}",
                    "password": "pass1234",
                    "role": bad_role,
                },
            )
            assert response.status_code == 422, response.text

        created = client.post(
            "/api/v1/system/users",
            headers=headers,
            json={
                "username": "plain_user",
                "employee_no": "U9001",
                "password": "pass1234",
                "role": "user",
            },
        )
        assert created.status_code == 201
        assert created.json()["role_label"] == "用户"


def test_last_admin_cannot_be_demoted(db):
    """把最后一个启用的管理员降级同样会把系统锁死。"""
    other = User(
        username="admin3",
        employee_no="ADM003",
        password_hash="x",
        display_name="第三管理员",
        role=UserRole.ADMIN.value,
        is_active=True,
    )
    db.add(other)
    db.commit()

    try:
        system_service.update_user(db, other, role=UserRole.USER.value, actor=None)
    except system_service.SystemError as exc:
        assert "管理员" in str(exc)
    else:
        raise AssertionError("降级最后一个管理员应当被拒绝")
