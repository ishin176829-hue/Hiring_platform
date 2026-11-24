
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
