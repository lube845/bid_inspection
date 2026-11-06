from fastapi.testclient import TestClient

from app.main import app


def test_health_login_and_project_creation():
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"

        login = client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "admin123"}
        )
        assert login.status_code == 200
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        me = client.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["username"] == "admin"

        created = client.post(
            "/api/v1/projects",
            json={"project_name": "接口测试项目", "project_code": "API-TEST-001"},
            headers=headers,
        )
        assert created.status_code == 201
        project = created.json()
        assert project["project_name"] == "接口测试项目"
        assert project["status"] == "pending"

        listed = client.get("/api/v1/projects", headers=headers)
        assert listed.status_code == 200
        assert any(item["id"] == project["id"] for item in listed.json()["items"])

        unauthorized = client.get("/api/v1/projects")
        assert unauthorized.status_code == 401
