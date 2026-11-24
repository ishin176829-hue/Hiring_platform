from fastapi.testclient import TestClient

from boss_hiring.app import create_app


class Browser:
    async def status(self):
        return {"state": "closed", "url": "", "message": "尚未启动"}

    async def close(self):
        pass


def client(tmp_path):
    app = create_app(tmp_path, browser=Browser())
    return TestClient(app), {"Authorization": f"Bearer {app.state.config.token}"}


def test_api_requires_token_even_on_loopback(tmp_path):
    c, headers = client(tmp_path)
    with c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers=headers).status_code == 200
        assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_rejects_cross_origin_and_unsafe_hosts(tmp_path):
    c, headers = client(tmp_path)
    with c:
        assert c.post("/api/pause", json={}, headers={**headers, "Origin": "https://evil.test"}).status_code == 403
        assert c.get("/api/status", headers={**headers, "Host": "evil.test"}).status_code == 400
        assert c.post("/api/pause", content="{}", headers=headers).status_code == 415


def test_settings_and_missing_resume_do_not_block_collection_configuration(tmp_path):
    c, headers = client(tmp_path)
    with c:
        current = c.get("/api/settings", headers=headers).json()
        assert current["cities"] == ["杭州", "上海"]
        assert "resume" not in current
        good = c.put("/api/settings", json={**current, "daily_limit": 5}, headers=headers)
        assert good.status_code == 200
        bad = c.put("/api/settings", json={**current, "cities": ["北京"]}, headers=headers)
        assert bad.status_code == 422
        unknown = c.put("/api/settings", json={**current, "arbitrary_path": "/etc/passwd"}, headers=headers)
        assert unknown.status_code == 422


def test_invalid_batch_requests_have_readable_errors(tmp_path):
    c, headers = client(tmp_path)
    with c:
        response = c.post("/api/batches", json={"job_ids": [999], "action": "greet", "message": "您好", "resume_name": "", "mode": "live"}, headers=headers)
        assert response.status_code == 400
        assert isinstance(response.json()["detail"], str)
        assert c.get("/api/batches/999", headers=headers).status_code == 404
