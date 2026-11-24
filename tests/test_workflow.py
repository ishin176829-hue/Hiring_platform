import asyncio
from copy import deepcopy

import pytest

from boss_hiring.store import Store, job_fingerprint
from boss_hiring.service import Service


def job(key="one", city="杭州"):
    return dict(job_key=key, url=f"https://www.zhipin.com/job_detail/{key}.html",
                title="AI应用开发工程师", company="测试公司", city=city, salary="20-30K",
                experience="3-5年", education="本科", description="负责智能体工作流与系统对接。",
                recruiter="测试招聘者 · 招聘经理", tags=["Agent"])


def prepared(tmp_path, mode="live", count=1):
    store = Store(tmp_path / "test.sqlite3")
    store.upsert_jobs([job(str(i)) for i in range(count)])
    batch = store.create_batch([j["id"] for j in store.list_jobs()], "greet", "您好，想了解该职位。", "", mode)
    store.approve(batch["id"], batch["digest"], mode == "live")
    return store, store.batch(batch["id"])


def test_city_requires_actual_evidence_and_site_links(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.upsert_jobs([job("hz"), job("bj", "北京"), job("unknown", "")])
    assert [j["job_key"] for j in store.list_jobs() if j["eligible"]] == ["hz"]
    with pytest.raises(ValueError):
        store.upsert_jobs([{**job(), "url": "https://www.zhipin.com.attacker.test/job_detail/a.html"}])
    with pytest.raises(ValueError):
        store.upsert_jobs([{**job(), "url": "file:///etc/passwd"}])


def test_job_identity_and_fingerprint_are_stable_but_not_blind_to_changes(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    assert store.upsert_jobs([job(), job()]) == 1
    first = store.list_jobs()[0]
    store.upsert_jobs([{**job(), "salary": "25-35K"}])
    assert len(store.list_jobs()) == 1
    assert store.list_jobs()[0]["id"] == first["id"]
    assert job_fingerprint(first) != job_fingerprint(store.list_jobs()[0])
    assert job_fingerprint(job()) == job_fingerprint({**job(), "tags": ["其他"], "collected_at": "later"})


def test_live_confirmation_binds_immutable_recipient_message_and_content(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.upsert_jobs([job()])
    batch = store.create_batch([1], "greet", "您好，想了解该职位。", "", "live")
    with pytest.raises(ValueError):
        store.approve(batch["id"], "wrong", True)
    with pytest.raises(ValueError):
        store.approve(batch["id"], batch["digest"], False)
    with pytest.raises(ValueError):
        store.claim_batch(batch["id"])
    store.upsert_jobs([{**job(), "recruiter": "另一个人"}])
    assert store.batch(batch["id"])["items"][0]["job"]["recruiter"] == job()["recruiter"]
    store.approve(batch["id"], batch["digest"], True)
    store.claim_batch(batch["id"])
    with pytest.raises(ValueError):
        store.claim_batch(batch["id"])


def test_batch_validates_every_id_and_cannot_select_ineligible_jobs(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.upsert_jobs([job(), job("outside", "北京")])
    for ids in ([1, 999], [1, 1], [2], []):
        with pytest.raises(ValueError):
            store.create_batch(ids, "greet", "您好", "", "dry_run")
    with pytest.raises(ValueError):
        store.create_batch([1], "resume", "", "", "live")


def test_reservation_prevents_duplicates_even_across_different_batches(tmp_path):
    store, batch = prepared(tmp_path)
    item = batch["items"][0]
    store.reserve(item["id"], 20)
    assert store.daily_used() == 1
    other = store.create_batch([item["job_id"]], "greet", "您好", "", "live")
    store.approve(other["id"], other["digest"], True)
    with pytest.raises(ValueError):
        store.reserve(other["items"][0]["id"], 20)


def test_first_contact_uses_explicit_platform_action_and_shares_greeting_dedup(tmp_path):
    store = Store(tmp_path / "test.sqlite3")
    store.upsert_jobs([job()])
    first = store.create_batch([1], "start_chat", "", "", "live")
    store.approve(first["id"], first["digest"], True)
    store.reserve(first["items"][0]["id"], 20)
    custom = store.create_batch([1], "greet", "您好", "", "live")
    store.approve(custom["id"], custom["digest"], True)
    with pytest.raises(ValueError):
        store.reserve(custom["items"][0]["id"], 20)


def test_quota_is_persistent_and_sending_recovery_is_uncertain(tmp_path):
    store, batch = prepared(tmp_path, count=2)
    store.reserve(batch["items"][0]["id"], 1)
    with pytest.raises(ValueError):
        store.reserve(batch["items"][1]["id"], 1)
    reopened = Store(tmp_path / "test.sqlite3")
    reopened.recover()
    assert reopened.batch(batch["id"])["items"][0]["status"] == "uncertain"
    assert reopened.daily_used() == 1


class Browser:
    def __init__(self, outcome="greeted", delay=0):
        self.outcome, self.calls, self.delay = outcome, [], delay

    async def check(self, job, action, resume_name):
        return {"ok": True, "reason": ""}

    async def perform(self, job, action, message, resume_name):
        self.calls.append((job["job_key"], message))
        await asyncio.sleep(self.delay)
        if self.outcome == "raise":
            raise RuntimeError("connection lost after click")
        return {"status": self.outcome, "detail": "fixture outcome"}

    async def close(self):
        pass


async def test_dry_run_never_calls_send_or_uses_live_quota(tmp_path):
    store, batch = prepared(tmp_path, mode="dry_run")
    browser = Browser()
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await service.task
    assert browser.calls == []
    assert store.daily_used() == 0
    assert store.batch(batch["id"])["items"][0]["status"] == "dry_run"


async def test_unknown_send_outcome_pauses_and_does_not_send_next_item(tmp_path):
    store, batch = prepared(tmp_path, count=2)
    browser = Browser("raise")
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await service.task
    statuses = [i["status"] for i in store.batch(batch["id"])["items"]]
    assert statuses == ["uncertain", "pending"]
    assert len(browser.calls) == 1
    assert store.paused()


async def test_changed_stored_job_cannot_be_sent_under_old_confirmation(tmp_path):
    store, batch = prepared(tmp_path)
    store.upsert_jobs([{**job("0"), "description": "职责已发生变化"}])
    browser = Browser()
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await service.task
    assert browser.calls == []
    assert store.batch(batch["id"])["items"][0]["status"] == "skipped"


async def test_pause_and_single_active_task_are_enforced(tmp_path):
    store, batch = prepared(tmp_path)
    browser = Browser(delay=0.02)
    service = Service(store, browser)
    store.set_paused(True)
    with pytest.raises(ValueError):
        service.start_batch(batch["id"])
    store.set_paused(False)
    service.start_batch(batch["id"])
    with pytest.raises(ValueError):
        service.start_batch(batch["id"])
    await service.task
    assert len(browser.calls) == 1


async def test_browser_verification_stops_batch_without_discarding_candidates(tmp_path):
    store, batch = prepared(tmp_path, count=2)
    browser = Browser()

    async def verification(job, action, resume_name):
        return {"ok": False, "blocked": True, "reason": "请先完成人工验证"}

    browser.check = verification
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await service.task
    assert [item["status"] for item in store.batch(batch["id"])["items"]] == ["pending", "pending"]
    assert browser.calls == [] and store.daily_used() == 0
    assert store.paused()


async def test_new_company_exclusion_is_respected_after_approval(tmp_path):
    store, batch = prepared(tmp_path)
    store.update_settings({**store.settings(), "exclude_companies": ["测试公司"]})
    browser = Browser()
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await service.task
    assert browser.calls == []
    assert store.batch(batch["id"])["items"][0]["status"] == "skipped"


async def test_shutdown_during_send_preserves_uncertainty(tmp_path):
    store, batch = prepared(tmp_path)
    entered = asyncio.Event()
    browser = Browser()

    async def interrupted(*args):
        entered.set()
        await asyncio.sleep(60)

    browser.perform = interrupted
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await asyncio.wait_for(entered.wait(), 1)
    await service.close()
    assert store.batch(batch["id"])["items"][0]["status"] == "uncertain"
    assert store.paused()


def test_action_spacing_survives_new_batch_and_store_instance(tmp_path):
    store, batch = prepared(tmp_path, count=2)
    store.reserve(batch["items"][0]["id"], 20, interval_seconds=30)
    reopened = Store(tmp_path / "test.sqlite3")
    with pytest.raises(ValueError, match="间隔"):
        reopened.reserve(batch["items"][1]["id"], 20, interval_seconds=30)


async def test_pause_then_resume_during_preflight_cannot_dispatch_or_consume_quota(tmp_path):
    store, batch = prepared(tmp_path)
    browser = Browser()
    browser.before_send = lambda: True
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_preflight(*args):
        entered.set()
        await release.wait()
        if not browser.before_send():
            return {"status": "paused", "dispatched": False, "detail": "发送前暂停"}
        browser.calls.append("sent")
        return {"status": "greeted", "detail": "sent"}

    browser.perform = delayed_preflight
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await asyncio.wait_for(entered.wait(), 1)
    service.pause()
    store.set_paused(False)
    release.set()
    await asyncio.wait_for(service.task, 1)
    assert browser.calls == []
    assert store.daily_used() == 0
    assert store.batch(batch["id"])["items"][0]["status"] == "pending"


async def test_resuming_during_inflight_send_does_not_resume_old_batch(tmp_path):
    store, batch = prepared(tmp_path, count=2)
    browser = Browser()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_receipt(*args):
        browser.calls.append("sent")
        entered.set()
        await release.wait()
        return {"status": "greeted", "detail": "已确认发送"}

    browser.perform = delayed_receipt
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await asyncio.wait_for(entered.wait(), 1)
    service.pause()
    store.set_paused(False)
    release.set()
    await asyncio.wait_for(service.task, 1)
    assert browser.calls == ["sent"]
    assert [i["status"] for i in store.batch(batch["id"])["items"]] == ["greeted", "pending"]


def test_terminal_uncertainty_atomically_pauses_and_recovers_correct_batch_state(tmp_path):
    store, batch = prepared(tmp_path)
    store.claim_batch(batch["id"])
    store.reserve(batch["items"][0]["id"], 20)
    store.item_result(batch["items"][0]["id"], "uncertain", "未确认的发送")
    assert Store(tmp_path / "test.sqlite3").paused()
    store.recover()
    assert store.batch(batch["id"])["status"] == "needs_review"


async def test_settings_change_while_reading_page_cannot_bypass_exclusion(tmp_path):
    store, batch = prepared(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    browser = Browser()

    async def waiting_check(*args):
        entered.set()
        await release.wait()
        return {"ok": True}

    browser.check = waiting_check
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await asyncio.wait_for(entered.wait(), 1)
    store.update_settings({**store.settings(), "exclude_companies": ["测试公司"]})
    release.set()
    await service.task
    assert browser.calls == []
    assert store.daily_used() == 0


async def test_known_preclick_failure_releases_duplicate_claim_for_new_confirmation(tmp_path):
    store, batch = prepared(tmp_path)
    browser = Browser()

    async def no_dispatch(*args):
        return {"status": "blocked", "dispatched": False, "detail": "输入框存在草稿，未点击"}

    browser.perform = no_dispatch
    service = Service(store, browser)
    service.start_batch(batch["id"])
    await service.task
    assert store.daily_used() == 0
    retry = store.create_batch([batch["items"][0]["job_id"]], "greet", "您好", "", "live")
    store.approve(retry["id"], retry["digest"], True)
    store.reserve(retry["items"][0]["id"], 20)


def test_slow_preflight_records_actual_dispatch_time_and_day(tmp_path, monkeypatch):
    clock = {"time": "2026-11-20T23:59:55+08:00"}
    monkeypatch.setattr("boss_hiring.store.now", lambda: clock["time"])
    monkeypatch.setattr("boss_hiring.store.day", lambda: clock["time"][:10])
    store, batch = prepared(tmp_path, count=2)
    first, second = batch["items"]
    store.reserve(first["id"], 20, interval_seconds=30)
    clock["time"] = "2026-11-21T00:00:30+08:00"
    assert store.confirm_dispatch(first["id"], {**first["job"], "id": first["job_id"]})
    assert store.daily_used() == 1
    clock["time"] = "2026-11-21T00:00:31+08:00"
    with pytest.raises(ValueError, match="间隔"):
        store.reserve(second["id"], 20, interval_seconds=30)


def test_final_dispatch_cannot_bypass_new_day_quota_with_old_reservation(tmp_path, monkeypatch):
    clock = {"time": "2026-11-20T23:59:55+08:00"}
    monkeypatch.setattr("boss_hiring.store.now", lambda: clock["time"])
    monkeypatch.setattr("boss_hiring.store.day", lambda: clock["time"][:10])
    store, batch = prepared(tmp_path, count=2)
    store.update_settings({**store.settings(), "daily_limit": 1, "interval_seconds": 10})
    first, second = batch["items"]
    store.reserve(first["id"], 1)
    clock["time"] = "2026-11-21T00:00:05+08:00"
    store.reserve(second["id"], 1)
    assert store.confirm_dispatch(second["id"], {**second["job"], "id": second["job_id"]})
    clock["time"] = "2026-11-21T00:00:35+08:00"
    assert not store.confirm_dispatch(first["id"], {**first["job"], "id": first["job_id"]})
