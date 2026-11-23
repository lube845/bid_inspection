"""项目级权限：创建者独占、分享给他人、三种权限级别。

覆盖「用户创建的项目只有自己能看」这条主线的三层：
- 服务层：可见项目集合、权限级别包含关系；
- 接口层：未分享的项目对他人 404，分享后按级别放开读/审/管；
- 分享本身：只能按工号/邮箱定位、不能授出高于自己的权限、创建者不可移除。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Project, User
from app.models.enums import ProjectRole, UserRole
from app.services import access_service, project_share_service, system_service


def _client():
    from app.main import app

    return TestClient(app)


def _login(client, account="admin", password="admin123"):
    response = client.post(
        "/api/v1/auth/login", json={"username": account, "password": password}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_user(db, username, employee_no, role=UserRole.USER.value):
    return system_service.create_user(
        db, username, "pass1234", username, role, employee_no=employee_no
    )


def _make_project(db, owner_id, name="归属测试项目"):
    project = Project(project_name=name, created_by=owner_id)
    db.add(project)
    db.commit()
    return project


# ---------------------------------------------------------------- 服务层


def test_creator_and_admin_have_full_access(db):
    owner = User(username="owner", employee_no="OWN1", password_hash="x", role=UserRole.USER.value)
    admin = User(username="root", employee_no="ADM1", password_hash="x", role=UserRole.ADMIN.value)
    other = User(username="stranger", employee_no="STR1", password_hash="x", role=UserRole.USER.value)
    db.add_all([owner, admin, other])
    db.commit()
    project = _make_project(db, owner.id)

    assert access_service.primary_role(db, project, owner) == ProjectRole.MANAGER.value
    assert access_service.primary_role(db, project, admin) == ProjectRole.MANAGER.value
    assert access_service.allowed_roles(db, project, other) == set()
    # 管理员不需要逐条授权就能看到全部项目
    assert access_service.visible_project_ids(db, admin) is None
    assert access_service.visible_project_ids(db, other) == []


def test_shared_role_expands_to_lower_levels(db):
    owner = User(username="owner2", employee_no="OWN2", password_hash="x")
    user = User(username="guest", employee_no="GST2", password_hash="x")
    db.add_all([owner, user])
    db.commit()
    project = _make_project(db, owner.id)

    project_share_service.share_project(
        db, project, owner, "GST2", ProjectRole.REVIEWER.value
    )
    assert access_service.has_role(db, project, user, access_service.VIEW)
    assert access_service.has_role(db, project, user, access_service.REVIEW)
    # 审批不等于管理
    assert not access_service.has_role(db, project, user, access_service.MANAGE)


def test_cannot_grant_higher_than_own_role(db):
    owner = User(username="owner3", employee_no="OWN3", password_hash="x")
    manager = User(username="mgr", employee_no="MGR3", password_hash="x")
    target = User(username="tgt", employee_no="TGT3", password_hash="x")
    db.add_all([owner, manager, target])
    db.commit()
    project = _make_project(db, owner.id)
    project_share_service.share_project(
        db, project, owner, "MGR3", ProjectRole.MANAGER.value
    )

    # 管理者可以继续分享管理权限
    result = project_share_service.share_project(
        db, project, manager, "TGT3", ProjectRole.VIEWER.value
    )
    assert result["member"]["role"] == "viewer"

    # 查看者不能再分享给任何人
    with pytest.raises(project_share_service.ShareError):
        project_share_service.share_project(
            db, project, target, "OWN3", ProjectRole.VIEWER.value
        )


def test_share_locates_user_by_employee_no_or_email(db):
    owner = User(username="owner4", employee_no="OWN4", password_hash="x")
    guest = User(
        username="guest4", employee_no="GST4", email="guest4@example.com", password_hash="x"
    )
    db.add_all([owner, guest])
    db.commit()
    project = _make_project(db, owner.id)

    by_email = project_share_service.share_project(
        db, project, owner, "GUEST4@example.com", ProjectRole.VIEWER.value
    )
    assert by_email["member"]["employee_no"] == "GST4"

    # 重复分享同一级别直接拒绝，避免出现两条授权
    with pytest.raises(project_share_service.ShareError):
        project_share_service.share_project(
            db, project, owner, "GST4", ProjectRole.VIEWER.value
        )

    # 用户名不能用来定位（可能重名）
    with pytest.raises(project_share_service.ShareError):
        project_share_service.share_project(
            db, project, owner, "guest4", ProjectRole.VIEWER.value
        )


def test_owner_cannot_be_revoked(db):
    owner = User(username="owner5", employee_no="OWN5", password_hash="x")
    db.add(owner)
    db.commit()
    project = _make_project(db, owner.id)
    from app.models import ProjectMember

    # 构造一条指向创建者的成员记录（正常流程不会出现，用于校验防御逻辑）
    member = ProjectMember(project_id=project.id, user_id=owner.id, role="viewer")
    db.add(member)
    db.commit()

    with pytest.raises(project_share_service.ShareError):
        project_share_service.revoke_member(db, project, owner, member)


# ---------------------------------------------------------------- 接口层


def test_project_invisible_to_unshared_user():
    with _client() as client:
        admin = _login(client)
        # 管理员建的项目：其他普通用户看不到
        client.post(
            "/api/v1/system/users",
            headers=admin,
            json={"username": "u_a", "employee_no": "IA001", "password": "pass1234"},
        )
        client.post(
            "/api/v1/system/users",
            headers=admin,
            json={"username": "u_b", "employee_no": "IB001", "password": "pass1234"},
        )
        a = _login(client, "IA001", "pass1234")
        b = _login(client, "IB001", "pass1234")

        created = client.post(
            "/api/v1/projects", headers=a, json={"project_name": "甲的私有项目"}
        ).json()
        pid = created["id"]

        # A 自己可见
        assert client.get(f"/api/v1/projects/{pid}", headers=a).status_code == 200
        assert any(
            item["id"] == pid
            for item in client.get("/api/v1/projects", headers=a).json()["items"]
        )
        # B 完全看不到：列表没有、详情 404
        assert all(
            item["id"] != pid
            for item in client.get("/api/v1/projects", headers=b).json()["items"]
        )
        assert client.get(f"/api/v1/projects/{pid}", headers=b).status_code == 404
        assert (
            client.get(f"/api/v1/projects/{pid}/permissions", headers=b).status_code
            == 404
        )
        # 管理员始终能看到
        assert client.get(f"/api/v1/projects/{pid}", headers=admin).status_code == 200


def test_share_then_role_limits_actions():
    with _client() as client:
        admin = _login(client)
        for name, emp in (("s_owner", "SX001"), ("s_viewer", "SX002"), ("s_reviewer", "SX003")):
            client.post(
                "/api/v1/system/users",
                headers=admin,
                json={"username": name, "employee_no": emp, "password": "pass1234"},
            )
        owner = _login(client, "SX001", "pass1234")
        viewer = _login(client, "SX002", "pass1234")
        reviewer = _login(client, "SX003", "pass1234")

        pid = client.post(
            "/api/v1/projects", headers=owner, json={"project_name": "分享测试项目"}
        ).json()["id"]

        shared_view = client.post(
            f"/api/v1/projects/{pid}/permissions",
            headers=owner,
            json={"account": "SX002", "role": "viewer"},
        )
        assert shared_view.status_code == 201, shared_view.text
        shared_review = client.post(
            f"/api/v1/projects/{pid}/permissions",
            headers=owner,
            json={"account": "SX003", "role": "reviewer"},
        )
        assert shared_review.status_code == 201, shared_review.text

        # 查看者：能看，不能改
        assert client.get(f"/api/v1/projects/{pid}", headers=viewer).status_code == 200
        assert (
            client.patch(
                f"/api/v1/projects/{pid}", headers=viewer, json={"project_name": "改名"}
            ).status_code
            == 403
        )
        # 审批者同样不能改项目设置
        assert (
            client.patch(
                f"/api/v1/projects/{pid}",
                headers=reviewer,
                json={"project_name": "改名"},
            ).status_code
            == 403
        )
        # 审批者可以查看比对结果（审核动作已收进比对分析与比对结果两页）
        assert (
            client.get(f"/api/v1/projects/{pid}/results/overview", headers=reviewer).status_code
            == 200
        )
        # 创建者可以改
        assert (
            client.patch(
                f"/api/v1/projects/{pid}",
                headers=owner,
                json={"project_name": "改后的名字"},
            ).status_code
            == 200
        )

        listed = client.get(f"/api/v1/projects/{pid}/permissions", headers=owner).json()
        assert listed["owner_id"] is not None
        roles = {item["employee_no"] or item["username"]: item["role"] for item in listed["items"]}
        assert roles.get("SX002") == "viewer"
        assert roles.get("SX003") == "reviewer"
        assert listed["can_manage"] is True
        # 查看者打开权限页只能看到名单，拿不到可分享名单
        viewer_view = client.get(f"/api/v1/projects/{pid}/permissions", headers=viewer).json()
        assert viewer_view["share_targets"] == []
        assert viewer_view["can_manage"] is False


def test_revoke_removes_visibility():
    with _client() as client:
        admin = _login(client)
        for name, emp in (("r_owner", "RX001"), ("r_guest", "RX002")):
            client.post(
                "/api/v1/system/users",
                headers=admin,
                json={"username": name, "employee_no": emp, "password": "pass1234"},
            )
        owner = _login(client, "RX001", "pass1234")
        guest = _login(client, "RX002", "pass1234")
        pid = client.post(
            "/api/v1/projects", headers=owner, json={"project_name": "撤销测试"}
        ).json()["id"]

        shared = client.post(
            f"/api/v1/projects/{pid}/permissions",
            headers=owner,
            json={"account": "RX002", "role": "manager"},
        ).json()
        assert client.get(f"/api/v1/projects/{pid}", headers=guest).status_code == 200

        # 被授予管理权限的人也可以再分享
        client.post(
            "/api/v1/system/users",
            headers=admin,
            json={"username": "r_third", "employee_no": "RX003", "password": "pass1234"},
        )
        third = client.post(
            f"/api/v1/projects/{pid}/permissions",
            headers=guest,
            json={"account": "RX003", "role": "viewer"},
        )
        assert third.status_code == 201, third.text

        member_id = shared["member"]["id"]
        revoked = client.delete(
            f"/api/v1/projects/{pid}/permissions/{member_id}", headers=owner
        )
        assert revoked.status_code == 200, revoked.text
        assert client.get(f"/api/v1/projects/{pid}", headers=guest).status_code == 404


def test_share_rejects_unknown_account_and_bad_role():
    with _client() as client:
        admin = _login(client)
        client.post(
            "/api/v1/system/users",
            headers=admin,
            json={"username": "v_owner", "employee_no": "VX001", "password": "pass1234"},
        )
        owner = _login(client, "VX001", "pass1234")
        pid = client.post(
            "/api/v1/projects", headers=owner, json={"project_name": "参数校验"}
        ).json()["id"]

        unknown = client.post(
            f"/api/v1/projects/{pid}/permissions",
            headers=owner,
            json={"account": "NO-SUCH", "role": "viewer"},
        )
        assert unknown.status_code == 400
        assert "没有找到" in unknown.json()["detail"]

        bad_role = client.post(
            f"/api/v1/projects/{pid}/permissions",
            headers=owner,
            json={"account": "admin", "role": "owner"},
        )
        assert bad_role.status_code == 422



def test_viewer_cannot_review_and_fake_ids_are_rejected(db):
    """查看者不能审核；批量操作传不存在的证据 id 必须报错，不能被静默跳过。"""
    from app.models import AnalysisTask, Company, ComparisonEvidence

    from app.core.security import hash_password

    owner = User(
        username="b_owner", employee_no="BX01", password_hash=hash_password("pass1234")
    )
    viewer = User(
        username="b_viewer", employee_no="BX02", password_hash=hash_password("pass1234")
    )
    db.add_all([owner, viewer])
    db.commit()
    project = _make_project(db, owner.id, "批量校验项目")
    task = AnalysisTask(project_id=project.id, task_no=1)
    db.add(task)
    db.flush()
    db.add(Company(project_id=project.id, company_name="甲公司"))
    db.flush()
    evidence = ComparisonEvidence(
        project_id=project.id,
        analysis_task_id=task.id,
        evidence_type="text",
        severity="high",
        review_status="pending",
        is_current=True,
        summary="测试证据",
    )
    db.add(evidence)
    db.commit()
    project_share_service.share_project(db, project, owner, "BX02", "viewer")

    # 查看者能看，但不能审核
    assert access_service.has_role(db, project, viewer, access_service.VIEW)
    with pytest.raises(access_service.ProjectAccessError) as ctx:
        access_service.ensure_access(db, project, viewer, access_service.REVIEW)
    assert ctx.value.forbidden is True

    # 接口层：按线索审核传伪造 id 直接 404，不会返回 200
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        headers = _login(client, "BX02", "pass1234")
        fake = client.post(
            "/api/v1/evidences/cluster-review",
            headers=headers,
            json={"evidence_ids": ["no-such-evidence"], "status": "undecided"},
        )
        assert fake.status_code == 404, fake.text

        real = client.post(
            "/api/v1/evidences/cluster-review",
            headers=headers,
            json={"evidence_ids": [evidence.id], "status": "undecided"},
        )
        assert real.status_code == 403, real.text


def test_system_admin_manages_every_project_without_being_shared(db):
    """系统管理员对**所有**项目都有管理权限：包括别人创建、且没有分享给他的项目。"""
    admin = User(
        username="root_all", employee_no="ADMALL", password_hash="x", role=UserRole.ADMIN.value
    )
    owner = User(username="owner_all", employee_no="OWNALL", password_hash="x")
    outsider = User(username="outsider_all", employee_no="OUTALL", password_hash="x")
    db.add_all([admin, owner, outsider])
    db.commit()

    project = _make_project(db, owner.id, "别人的项目")

    # 管理员：管理权限，且能看到
    assert access_service.primary_role(db, project, admin) == ProjectRole.MANAGER.value
    assert access_service.has_role(db, project, admin, access_service.MANAGE)
    assert access_service.visible_project_ids(db, admin) is None  # None = 不过滤，可见全部

    # 没有管理权限的人仍然看不到
    assert access_service.allowed_roles(db, project, outsider) == set()

    # 管理员可以代替创建者继续分享，且不受「不能授予高于自己」限制
    result = project_share_service.share_project(
        db, project, admin, "OUTALL", ProjectRole.MANAGER.value
    )
    assert result["member"]["role"] == "manager"
    assert access_service.has_role(db, project, outsider, access_service.MANAGE)
