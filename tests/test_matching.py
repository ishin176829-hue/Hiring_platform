
from fastapi.testclient import TestClient

from boss_hiring.app import create_app
from boss_hiring.matching import ProfileInput, hard_conditions


class Browser:
    async def close(self):
        pass

    async def status(self):
        return {"state": "closed"}


def job(key, city, description, salary="25-35K"):
    return {"job_key": key, "url": f"https://www.zhipin.com/job_detail/{key}.html",
            "title": "流程工程师", "company": "测试公司", "city": city,
            "salary": salary, "description": description, "recruiter": "测试招聘者"}


def test_monthly_salary_and_hard_conditions():
    settings = {"cities": ["杭州", "上海"], "exclude_companies": []}
    profile = ProfileInput(text="流程经验", min_salary_k=28, outsourcing="exclude").model_dump()
    assert hard_conditions(job("one", "杭州", "公司直签。"), profile, settings) == (True, ["薪资区间跨过下限"])
    assert hard_conditions(job("two", "杭州", "本岗位为外包。"), profile, settings) == (False, [])
    assert hard_conditions(job("three", "北京", "公司直签。"), profile, settings) == (False, [])
    assert hard_conditions(job("four", "", "公司直签。", "300元/天"), profile, settings) == (True, ["实际城市待核对", "月薪口径待核对"])


def test_match_existing_jobs_routes_validate_quotes_and_do_not_send(tmp_path, monkeypatch):
    resume = "负责自动化工作流编排并上线"
    quote = "负责自动化工作流交付"
    profiles = []

    async def model(consent, instructions, data):
        assert consent
        assert "13800138000" not in data["resume"]
        profiles.append(data["resume"])
        if "fields" in data:
            return {"duties": [resume, "简历不存在的业绩"]}
        evidence = resume if data["jd"] == quote else "简历不存在的业绩"
        return {"items": [{"kind": "core", "status": "supported",
                           "jd_excerpt": data["jd"], "resume_excerpt": evidence}]}

    monkeypatch.setattr("boss_hiring.matching.ask_model", model)
    app = create_app(tmp_path, browser=Browser())

    async def finish():
        if app.state.service.task:
            await app.state.service.task

    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.config.token
        assert client.post("/api/matching/run", json={}).status_code == 400
        resume_input = {"text": "手机：13800138000\n" + resume, "external_consent": True}
        extracted = client.post("/api/matching/extract", json=resume_input).json()["fields"]
        assert extracted["duties"] == [resume]
        saved = client.put("/api/matching/profile", json={**resume_input, "fields": {"duties": [resume, "自行补充"]}}).json()
        assert [field["source"] for field in saved["fields"]["duties"]] == ["resume", "user"]
        client.post("/api/jobs/import", json={"jobs": [job("one", "杭州", quote), job("two", "上海", "其他职责"),
                                                      job("three", "北京", quote)]}).raise_for_status()
        assert not any(row["eligible"] for row in client.get("/api/jobs").json())
        task_id = client.post("/api/matching/run", json={}).json()["task_id"]
        client.portal.call(finish)
        result = client.get("/api/matching/latest").json()
        assert result["id"] == task_id
        assert result["scope"] == "existing"
        assert [row["tier"] for row in result["jobs"]] == ["priority"]
        assert result["jobs"][0]["evidence"][0]["resume_excerpt"] == resume
        assert len(result["errors"]) == 1
        assert len(profiles) == 3
        assert client.get("/api/batches").json() == []
        assert app.state.store.daily_used() == 0
        client.put("/api/matching/profile", json={**resume_input, "cities": ["上海"]}).raise_for_status()
        assert client.get("/api/matching/latest").json()["stale"]
        client.delete("/api/matching/profile").raise_for_status()
        assert client.get("/api/matching/profile").json() is None
        assert client.get("/api/matching/latest").json() is None


def test_resume_search_keeps_global_keywords_and_falls_back_to_existing_jobs(tmp_path, monkeypatch):
    quote = "工作流交付"
    calls = []

    class SearchBrowser(Browser):
        async def collect(self, cities, keywords, limit):
            calls.append((cities, keywords, limit))
            if len(calls) == 2:
                raise ValueError("未登录")
            return [job("same", "杭州", quote), job("new", "杭州", quote)]

    async def model(consent, instructions, data):
        if "jd" not in data:
            return {"directions": [{"title": "企业流程", "keyword": "工作流工程师", "excerpt": quote},
                                   {"title": "虚构能力", "keyword": "模型训练", "excerpt": "无原文"}]}
        return {"items": [{"kind": "core", "status": "supported", "jd_excerpt": quote, "resume_excerpt": quote}]}

    monkeypatch.setattr("boss_hiring.matching.ask_model", model)
    app = create_app(tmp_path, browser=SearchBrowser())

    async def finish():
        await app.state.service.task

    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.config.token
        saved = client.put("/api/matching/profile", json={"text": quote, "external_consent": True, "cities": ["杭州"]}).json()
        client.post("/api/jobs/import", json={"jobs": [job("same", "杭州", quote)]}).raise_for_status()
        settings = client.get("/api/settings").json()
        suggestions = client.post("/api/matching/suggestions", json={}).json()
        assert suggestions["profile_version"] == saved["version"]
        assert [item["keyword"] for item in suggestions["directions"]] == ["工作流工程师"]
        assert client.post("/api/matching/run", json={"keywords": []}).status_code == 422
        client.post("/api/matching/run", json={"keywords": [" 企业流程开发 "]}).raise_for_status()
        client.portal.call(finish)
        result = client.get("/api/matching/latest").json()
        assert calls[0] == (["杭州"], ["企业流程开发"], settings["collect_limit"])
        assert result["scope"] == "updated"
        assert result["keywords"] == ["企业流程开发"]
        assert len(result["jobs"]) == 2
        assert client.get("/api/settings").json() == settings
        client.post("/api/matching/run", json={"keywords": ["企业流程开发"]}).raise_for_status()
        client.portal.call(finish)
        result = client.get("/api/matching/latest").json()
        assert result["scope"] == "existing"
        assert result["collection_notice"]
        assert len(result["jobs"]) == 2


def test_recommended_shortlist_checks_version_and_requires_normal_confirmation(tmp_path, monkeypatch):
    quote = "工作流交付"
    unexpected = []

    class BatchBrowser(Browser):
        async def check(self, *args):
            return {"ok": True}

        async def perform(self, *args):
            unexpected.append("sent")
            return {"status": "greeted"}

    async def model(consent, instructions, data):
        return {"items": [{"kind": "core", "status": "supported", "jd_excerpt": quote, "resume_excerpt": quote}]}

    monkeypatch.setattr("boss_hiring.matching.ask_model", model)
    app = create_app(tmp_path, browser=BatchBrowser())

    async def finish():
        await app.state.service.task

    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.config.token
        profile = {"text": quote, "external_consent": True, "min_salary_k": 28}
        client.put("/api/matching/profile", json=profile).raise_for_status()
        original = job("good", "杭州", quote)
        client.post("/api/jobs/import", json={"jobs": [original, job("outside", "北京", quote),
                                                      job("unknown", "", quote)]}).raise_for_status()
        job_ids = {row["job_key"]: row["id"] for row in client.get("/api/jobs").json()}
        run = client.post("/api/matching/run", json={}).json()["task_id"]
        client.portal.call(finish)
        batch_body = {"job_ids": [job_ids["good"]], "action": "start_chat", "mode": "dry_run"}
        assert client.post("/api/batches", json=batch_body).status_code == 400
        for key in ("outside", "unknown"):
            assert client.post("/api/batches", json={**batch_body, "job_ids": [job_ids[key]], "match_run_id": run}).status_code == 400
        draft = client.post("/api/batches", json={**batch_body, "match_run_id": run}).json()
        assert draft["status"] == "draft"
        assert draft["items"][0]["job"]["match_checks"] == ["薪资区间跨过下限"]
        assert not unexpected and app.state.store.daily_used() == 0
        assert client.post(f"/api/batches/{draft['id']}/run", json={}).status_code == 400
        client.post(f"/api/batches/{draft['id']}/approve", json={"digest": draft["digest"]}).raise_for_status()
        client.post(f"/api/batches/{draft['id']}/run", json={}).raise_for_status()
        client.portal.call(finish)
        assert client.get(f"/api/batches/{draft['id']}").json()["items"][0]["status"] == "dry_run"
        assert not unexpected and app.state.store.daily_used() == 0
        client.post("/api/jobs/import", json={"jobs": [{**original, "salary": "10K"}]}).raise_for_status()
        assert client.post("/api/batches", json={**batch_body, "match_run_id": run}).status_code == 400
        client.put("/api/matching/profile", json={**profile, "min_salary_k": 30}).raise_for_status()
        assert "已过期" in client.post("/api/batches", json={**batch_body, "match_run_id": run}).json()["detail"]
