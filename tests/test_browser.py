from pathlib import Path
from urllib.parse import urlsplit

import pytest

from boss_hiring.browser import BrowserAdapter, BrowserBlocked


FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN = "https://www.zhipin.com"
JOB_PATH = "/job_detail/right-city.html"
START = (
    '<button class="btn-startchat" onclick="sessionStorage.setItem(\'attempts\', '
    '\'default greeting\')">立即沟通</button>'
)
PRESET_GREETING = "您好，这是预设招呼。"


def approved_job():
    return {
        "job_key": "right-city",
        "url": ORIGIN + JOB_PATH,
        "title": "AI 应用工程师",
        "company": "示例科技",
        "city": "杭州",
        "salary": "25-40K",
        "experience": "3-5年",
        "education": "本科",
        "description": "设计知识检索系统。\n维护模型应用。",
        "recruiter": "陈老师",
        "tags": ["Agent", "Python"],
    }


@pytest.fixture
async def browser(tmp_path):
    adapter = BrowserAdapter(tmp_path, headless=True)
    await adapter._ensure_browser()
    scenario = {
        "contact": START,
        "open_page": "ready",
        "detail_edit": "",
        "closed_job": False,
        "challenge_on_detail": False,
        "recruiter": "陈老师",
        "messages": "",
        "extra_send": "",
        "navigations": [],
    }

    async def serve(route):
        parsed = urlsplit(route.request.url)
        if parsed.hostname != "www.zhipin.com":
            await route.abort()
            return
        scenario["navigations"].append(parsed.path)
        if parsed.path == "/web/geek/jobs":
            markup = (FIXTURES / "boss-search.html").read_text(encoding="utf-8")
        elif parsed.path.startswith("/job_detail/"):
            if scenario["challenge_on_detail"]:
                await route.fulfill(
                    content_type="text/html; charset=utf-8",
                    body='<main class="security-check"><h1>安全验证</h1></main>',
                )
                return
            markup = (FIXTURES / "boss-detail.html").read_text(encoding="utf-8")
            markup = markup.replace("{{CONTACT}}", scenario["contact"])
            markup = markup.replace("维护模型应用。", "维护模型应用。" + scenario["detail_edit"])
            if scenario["closed_job"]:
                markup = markup.replace('<main class="job-detail">', '<main class="job-detail"><p>该职位已关闭</p>')
            if parsed.path.endswith("/missing-city.html"):
                markup = markup.replace(">杭州<", "><")
            elif parsed.path.endswith("/wrong-city.html"):
                markup = markup.replace(">杭州<", ">上海<")
        elif parsed.path == "/web/geek/chat":
            markup = (FIXTURES / "boss-chat.html").read_text(encoding="utf-8")
            for placeholder in ("recruiter", "messages", "extra_send"):
                markup = markup.replace("{{" + placeholder.upper() + "}}", scenario[placeholder])
        elif scenario["open_page"] == "challenge":
            markup = '<main class="security-check"><h1>安全验证</h1></main>'
        elif scenario["open_page"] == "login":
            markup = '<main class="login-register-content"><h1>登录/注册</h1></main>'
        elif scenario["open_page"] == "unknown":
            markup = "<main>获取职位</main>"
        else:
            markup = (
                '<script>history.replaceState({}, "", "/web/geek/recommend");</script>'
                '<nav class="user-nav"><div class="nav-figure">'
                '<a ka="header-username" href="/web/geek/recommend">已登录求职者</a></div></nav>'
            )
        await route.fulfill(content_type="text/html; charset=utf-8", body=markup)

    await adapter._context.route("**/*", serve)
    try:
        yield adapter, scenario
    finally:
        await adapter.close()


async def chat_link(scenario, *, existing=False, no_ack=False):
    query = "?friendId=noAck" if no_ack else "?friendId=fixture"
    text = "继续沟通" if existing else "查看聊天"
    scenario["contact"] = f'<a href="/web/geek/chat{query}">{text}</a>'


async def attempts(adapter):
    return await adapter._page.evaluate("sessionStorage.getItem('attempts')")


def start_click(scenario, *, acknowledge=False):
    navigate = "location.href='/web/geek/chat?friendId=fixture'"
    if acknowledge:
        navigate = f"sessionStorage.setItem('pendingPreset','{PRESET_GREETING}');{navigate}"
    scenario["contact"] = START.replace("default greeting')", f"default greeting');{navigate}")


async def test_collect_uses_page_city_and_global_limit(browser):
    adapter, scenario = browser
    await adapter.open_login()
    jobs = await adapter.collect(["杭州", "上海"], ["AI"], 1)
    assert len(jobs) == 1
    assert jobs[0] == approved_job()
    assert "/job_detail/missing-city.html" in scenario["navigations"]
    assert "/job_detail/wrong-city.html" in scenario["navigations"]
    assert "/job_detail/second-job.html" not in scenario["navigations"]


async def test_collect_rotates_between_cities_under_one_global_limit(browser):
    adapter, _ = browser
    await adapter.open_login()
    jobs = await adapter.collect(["杭州", "上海"], ["AI"], 2)
    assert [(job["city"], job["job_key"]) for job in jobs] == [
        ("杭州", "right-city"),
        ("上海", "wrong-city"),
    ]


async def test_status_requires_affirmative_login_evidence(browser):
    adapter, scenario = browser
    scenario["open_page"] = "unknown"
    await adapter._page.goto(ORIGIN + "/web/geek/recommend")
    assert (await adapter.status())["state"] == "unknown"
    scenario["open_page"] = "challenge"
    await adapter._page.reload()
    assert (await adapter.status())["state"] == "verification_required"
    scenario["open_page"] = "unknown"
    await adapter._page.goto(ORIGIN + "/web/passport/zp/security.html")
    assert (await adapter.status())["state"] == "verification_required"
    scenario["open_page"] = "login"
    await adapter._page.goto(ORIGIN + "/web/geek/recommend")
    assert (await adapter.status())["state"] == "login_required"
    scenario["open_page"] = "ready"
    await adapter._page.reload()
    assert (await adapter.status())["state"] == "ready"
    assert (await adapter.screenshot()).startswith(b"\x89PNG")
    await adapter.close()
    assert (await adapter.status())["state"] == "closed"
    with pytest.raises(BrowserBlocked):
        await adapter.screenshot()


async def test_check_is_read_only_and_blocks_changed_snapshot(browser):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario)
    result = await adapter.check(approved_job(), "greet", "")
    assert result["ok"], result
    assert await attempts(adapter) is None
    scenario["detail_edit"] = " 包含新的职位职责。"
    assert not (await adapter.check(approved_job(), "greet", ""))["ok"]
    result = await adapter.perform(approved_job(), "greet", "您好，想了解岗位", "")
    assert result["status"] == "blocked"
    assert result["dispatched"] is False
    assert await attempts(adapter) is None


async def test_check_flags_need_for_human_verification(browser):
    adapter, scenario = browser
    await adapter.open_login()
    scenario["challenge_on_detail"] = True
    result = await adapter.check(approved_job(), "greet", "")
    assert not result["ok"]
    assert result.get("blocked")
    assert "安全验证" in result["reason"]


async def test_immediate_chat_is_never_clicked(browser):
    adapter, _ = browser
    await adapter.open_login()
    assert not (await adapter.check(approved_job(), "greet", ""))["ok"]
    result = await adapter.perform(approved_job(), "greet", "您好，想了解岗位", "")
    assert result["status"] == "blocked"
    assert await attempts(adapter) is None


@pytest.mark.parametrize("acknowledges", [True, False])
async def test_start_chat_records_visible_default_greeting_or_stops_uncertain(browser, monkeypatch, acknowledges):
    adapter, scenario = browser
    await adapter.open_login()
    start_click(scenario, acknowledge=acknowledges)
    monkeypatch.setattr("boss_hiring.browser.SEND_CONFIRM_TIMEOUT_MS", 1500 if acknowledges else 700)
    assert (await adapter.check(approved_job(), "start_chat", ""))["ok"]
    assert await attempts(adapter) is None
    result = await adapter.perform(approved_job(), "start_chat", "", "")
    assert result["status"] == ("greeted" if acknowledges else "uncertain")
    assert result.get("dispatched") is not False
    assert await attempts(adapter) == "default greeting"
    if acknowledges:
        assert PRESET_GREETING in result["detail"]


async def test_start_chat_does_not_treat_old_outgoing_message_as_new_send(browser, monkeypatch):
    adapter, scenario = browser
    await adapter.open_login()
    scenario["messages"] = (
        '<div class="message-item message-self"><span class="message-text">旧招呼</span></div>'
    )
    start_click(scenario)
    monkeypatch.setattr("boss_hiring.browser.SEND_CONFIRM_TIMEOUT_MS", 700)
    result = await adapter.perform(approved_job(), "start_chat", "", "")
    assert result["status"] == "uncertain"
    assert await attempts(adapter) == "default greeting"


async def test_start_chat_does_not_treat_loaded_confirmation_as_new_receipt(browser, monkeypatch):
    adapter, scenario = browser
    await adapter.open_login()
    scenario["messages"] = (
        '<div class="message-item message-self"><span class="message-text">旧招呼</span></div>'
        '<div role="status">招呼已发送</div>'
    )
    start_click(scenario)
    monkeypatch.setattr("boss_hiring.browser.SEND_CONFIRM_TIMEOUT_MS", 700)
    result = await adapter.perform(approved_job(), "start_chat", "", "")
    assert result["status"] == "uncertain"


@pytest.mark.parametrize("action", ["greet", "resume", "start_chat"])
async def test_pause_guard_checked_on_verified_page_before_dispatch(browser, action):
    adapter, scenario = browser
    await adapter.open_login()
    if action == "start_chat":
        start_click(scenario, acknowledge=True)
    else:
        await chat_link(scenario, existing=(action == "resume"))
    guard_pages = []

    def paused():
        guard_pages.append(urlsplit(adapter._page.url).path)
        return False

    adapter.before_send = paused
    message = "您好" if action == "greet" else ""
    resume = "AI工程师.pdf" if action == "resume" else ""
    result = await adapter.perform(approved_job(), action, message, resume)
    assert result["status"] == "paused"
    assert result["dispatched"] is False
    assert guard_pages == [JOB_PATH if action == "start_chat" else "/web/geek/chat"]
    assert await attempts(adapter) is None


async def test_start_chat_skips_already_contacted_job(browser):
    adapter, scenario = browser
    await adapter.open_login()
    previous = '<a href="/web/geek/chat?friendId=fixture">继续沟通</a>'
    scenario["contact"] = START + previous
    result = await adapter.perform(approved_job(), "start_chat", "", "")
    assert result["status"] == "skipped"
    assert result["dispatched"] is False
    assert await attempts(adapter) is None


async def test_closed_job_is_blocked_even_if_stale_contact_link_remains(browser):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario)
    scenario["closed_job"] = True
    assert not (await adapter.check(approved_job(), "greet", ""))["ok"]
    result = await adapter.perform(approved_job(), "greet", "您好", "")
    assert result["status"] == "blocked"
    assert "/web/geek/chat" not in scenario["navigations"]


async def test_chat_link_with_unknown_parameters_is_blocked(browser):
    adapter, scenario = browser
    await adapter.open_login()
    scenario["contact"] = '<a href="/web/geek/chat?friendId=fixture&amp;autoGreet=true">查看聊天</a>'
    result = await adapter.perform(approved_job(), "greet", "您好", "")
    assert result["status"] == "blocked"
    assert "/web/geek/chat" not in scenario["navigations"]


@pytest.mark.parametrize("existing", ["detail", "message"])
async def test_greeting_skips_existing_communication(browser, existing):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario, existing=(existing == "detail"))
    if existing == "message":
        scenario["messages"] = '<div class="message-item"><span class="message-text">旧消息</span></div>'
    assert not (await adapter.check(approved_job(), "greet", ""))["ok"]
    result = await adapter.perform(approved_job(), "greet", "您好", "")
    assert result["status"] == "skipped"
    assert await attempts(adapter) is None


@pytest.mark.parametrize("failure", ["recipient", "button"])
async def test_greeting_blocks_mismatched_recipient_or_ambiguous_send(browser, failure):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario)
    if failure == "recipient":
        scenario["recruiter"] = "另一位陈老师"
    else:
        scenario["extra_send"] = '<button type="button" onclick="sendText()">发送</button>'
    result = await adapter.perform(approved_job(), "greet", "您好", "")
    assert result["status"] == "blocked"
    assert await attempts(adapter) is None


async def test_greet_requires_new_visible_exact_outgoing_message(browser):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario)
    message = "您好，\n想了解岗位。"
    result = await adapter.perform(approved_job(), "greet", message, "")
    assert result["status"] == "greeted", result
    assert await attempts(adapter) == f"text:{message}\n"


async def test_failed_acknowledgement_is_uncertain_without_second_send(browser, monkeypatch):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario, no_ack=True)
    monkeypatch.setattr("boss_hiring.browser.SEND_CONFIRM_TIMEOUT_MS", 350)
    result = await adapter.perform(approved_job(), "greet", "您好", "")
    assert result["status"] == "uncertain"
    assert result.get("dispatched") is not False
    assert await attempts(adapter) == "text:您好\n"


async def test_named_resume_sent_after_verified_single_choice(browser):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario, existing=True)
    scenario["messages"] = (
        '<div class="message-item"><span class="message-text">请发送附件</span></div>'
    )
    assert (await adapter.check(approved_job(), "resume", "AI工程师.pdf"))["ok"]
    assert await attempts(adapter) is None
    result = await adapter.perform(approved_job(), "resume", "", "AI工程师.pdf")
    assert result["status"] == "resume_sent"
    assert await attempts(adapter) == "resume:AI工程师.pdf\n"


async def test_missing_exact_resume_blocks_without_sending(browser):
    adapter, scenario = browser
    await adapter.open_login()
    await chat_link(scenario, existing=True)
    assert not (await adapter.check(approved_job(), "resume", "工程师.pdf"))["ok"]
    result = await adapter.perform(approved_job(), "resume", "", "工程师.pdf")
    assert result["status"] == "blocked"
    assert await attempts(adapter) is None
