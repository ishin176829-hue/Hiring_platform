"""BOSS web UI access through an isolated Playwright browser."""

import asyncio
import os
import re
from collections import deque
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, async_playwright

from .store import job_fingerprint, normalize_job


ORIGIN = "https://www.zhipin.com"
LOGIN_URL = f"{ORIGIN}/web/user/?ka=header-login"
SEARCH_URL = f"{ORIGIN}/web/geek/jobs"
CHAT_PATH = "/web/geek/chat"
CITY_CODES = {"杭州": "101210100", "上海": "101020100"}
NAV_TIMEOUT_MS = 20000
ELEMENT_TIMEOUT_MS = 5000
SEND_CONFIRM_TIMEOUT_MS = 8000

ACCOUNT = ".user-nav .nav-figure a[ka='header-username'], .user-nav .nav-figure a[href^='/web/geek/']"
CHALLENGE = ".security-check, .verify-wrap, .slider-captcha, #nc_1_wrapper"
CHALLENGE_PATHS = ("/web/passport/zp/security", "/web/common/security", "/captcha", "/security")
LOGIN = ".login-register-content, .login-dialog, a[ka='header-login']"
DETAIL_TITLE = ".job-banner h1"
DETAIL_TEXT = ".job-detail .job-sec-text, .job-detail .job-sec .text"
CHAT = ".chat-container"
HEADER = ".chat-header"
MESSAGES = ".message-list"
EDITOR = ".chat-editor"
RESUME_CHOOSER = ".resume-dialog[role='dialog']"
MESSAGE_ROWS = ".message-item, .message-self"
OUTGOING = ".message-self:not(.is-failed, .is-pending, [data-status='failed'], [data-status='sending'])"
CONTACT_AREA = ".job-banner, .job-detail"
CLOSED_JOB = re.compile(r"^(?:该)?职位已(?:关闭|下线|停止招聘)$")
START_CHAT_RECEIPT = re.compile(r"^\s*(?:招呼已发送|打招呼成功)[。！!]?\s*$")


class BrowserBlocked(RuntimeError):
    """Browser or page state does not permit the requested action."""


class _AlreadyContacted(BrowserBlocked):
    """The approved job has an existing conversation."""


class _JobClosed(BrowserBlocked):
    """The position no longer accepts messages."""


def _visible(locator: Locator, selector: str) -> Locator:
    return locator.locator(f":is({selector}):visible")


async def _one(locator: Locator, reason: str) -> Locator:
    if await locator.count() != 1:
        raise BrowserBlocked(reason)
    return locator


async def _field(locator: Locator, selector: str) -> str:
    matches = _visible(locator, selector)
    return (await matches.inner_text()).strip() if await matches.count() == 1 else ""


def _job_url(link: str) -> tuple[str, str] | None:
    try:
        normalized = normalize_job({"url": urljoin(ORIGIN, link)})
    except ValueError:
        return None
    return normalized["job_key"], normalized["url"]


def _chat_url(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc == "www.zhipin.com" and parsed.path == CHAT_PATH


def _start_chat_receipt(page: Page) -> Locator:
    return page.locator(":is([role='status'], [role='alert'], .toast):visible").filter(has_text=START_CHAT_RECEIPT)


def _city(area: str) -> str:
    for city in CITY_CODES:
        if re.match(rf"^{city}(?:市?(?:[·•・\s/—-]|$)|市)", area):
            return city
    return ""


class BrowserAdapter:
    def __init__(
        self,
        state_dir: Path,
        *,
        headless: bool = False,
        before_send: Callable[[], bool] | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.headless = headless
        self.before_send = before_send if before_send is not None else lambda: True
        self._playwright = None
        self._context = None
        self._page = None

    async def _ensure_browser(self) -> None:
        if self._context is not None and self._page is not None and not self._page.is_closed():
            return
        await self.close()
        local_browsers = Path(__file__).resolve().parents[1] / ".cache/ms-playwright"
        if local_browsers.is_dir():
            os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(local_browsers))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._playwright = await async_playwright().start()
            chromium = self._playwright.chromium
            system_chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            channel = "chrome" if not Path(chromium.executable_path).is_file() and system_chrome.is_file() else None
            self._context = await chromium.launch_persistent_context(
                str(self.state_dir / "browser-profile"),
                headless=self.headless,
                channel=channel,
                accept_downloads=False,
                service_workers="block",
            )
            self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            self._page.set_default_timeout(ELEMENT_TIMEOUT_MS)
            self._page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        except PlaywrightError as error:
            await self.close()
            raise BrowserBlocked("Chromium 启动失败。请检查本机 Playwright 浏览器安装和桌面会话。") from error

    async def open_login(self) -> dict:
        await self._ensure_browser()
        current = await self.status()
        try:
            await self._page.bring_to_front()
            if current["state"] == "unknown":
                await self._page.goto(LOGIN_URL, wait_until="domcontentloaded")
        except PlaywrightError as error:
            raise BrowserBlocked("打开 BOSS 登录页失败，请检查网络并在浏览器中确认页面。") from error
        return await self.status()

    async def status(self) -> dict:
        if self._page is None or self._page.is_closed():
            return {"state": "closed", "url": "", "message": "浏览器尚未打开或已关闭。"}
        url = self._page.url
        parsed = urlsplit(url)
        state = "unknown"
        message = "未能确认登录状态，请检查打开的 BOSS 页面。"
        try:
            if parsed.scheme == "https" and (
                parsed.netloc in ("www.zhipin.com", "zhipin.com")
            ):
                if (
                    parsed.path.startswith(CHALLENGE_PATHS)
                    or await self._page.locator(f":is({CHALLENGE}):visible").count()
                    or (await self._page.title()).strip() in {"安全验证", "访问验证"}
                ):
                    state, message = "verification_required", "请在浏览器中手动完成 BOSS 安全验证。"
                elif (
                    parsed.path.rstrip("/") == "/web/user"
                    or await self._page.locator(f":is({LOGIN}):visible").count()
                ):
                    state, message = "login_required", "请在浏览器中登录 BOSS 直聘。"
                elif await self._page.locator(f":is({ACCOUNT}):visible").count() == 1:
                    state, message = "ready", "BOSS 页面显示已登录。"
        except PlaywrightError:
            pass
        # Query strings can contain identifiers or session information.
        visible_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.netloc else url
        return {"state": state, "url": visible_url, "message": message}

    async def screenshot(self) -> bytes:
        if self._page is None or self._page.is_closed():
            raise BrowserBlocked("请先打开 BOSS 浏览器。")
        try:
            return await self._page.screenshot(type="png", timeout=ELEMENT_TIMEOUT_MS)
        except PlaywrightError as error:
            raise BrowserBlocked("当前浏览器页面无法截图。") from error

    async def _require_ready(self) -> None:
        current = await self.status()
        if current["state"] != "ready":
            raise BrowserBlocked(current["message"])

    async def _visit(self, url: str, expected: str) -> None:
        try:
            await self._page.goto(url, wait_until="domcontentloaded")
            await self._page.locator(f":is({expected}, {CHALLENGE}, {LOGIN}):visible").first.wait_for()
        except PlaywrightError as error:
            await self._require_ready()
            raise BrowserBlocked("BOSS 页面加载超时或布局已变化。") from error
        await self._require_ready()

    async def _read_job(self, url: str) -> dict:
        await self._visit(url, f"{DETAIL_TITLE}, .job-status")
        current = _job_url(self._page.url)
        if current is None or current[1] != url:
            raise BrowserBlocked("岗位页面已重定向，无法核验原岗位。")
        closure = self._page.get_by_text(CLOSED_JOB).and_(self._page.locator(":visible"))
        if await closure.count():
            raise _JobClosed("BOSS 页面显示该职位已关闭。")
        body = self._page.locator("body")
        title = await _field(body, DETAIL_TITLE)
        description = await _field(body, DETAIL_TEXT)
        company = await _field(
            body, "a[ka='job-detail-company'], .sider-company a[ka='job-detail-brand'], .company-info .company-name"
        )
        if not company:
            company = await _field(body, ".sider-company .company-info a, .brand-wrap .brand-name")
        recruiter = await _field(body, ".boss-info h2, .job-boss-info .boss-name")
        area = await _field(body, ".job-banner .job-area")
        facts = await _visible(body, ".job-banner .text-desc > span").all_inner_texts()
        experience = next((value.strip() for value in facts if re.search(r"经验|年|应届|在校", value)), "")
        education = next(
            (value.strip() for value in facts if re.search(r"学历|本科|大专|硕士|初中|高中|中专|博士", value)),
            "",
        )
        return {
            "job_key": current[0],
            "url": current[1],
            "title": title,
            "company": company,
            "city": _city(area),
            "salary": await _field(body, ".job-banner .salary"),
            "experience": experience,
            "education": education,
            "description": description,
            "recruiter": recruiter,
            "tags": [
                tag.strip()
                for tag in await _visible(body, ".job-detail .job-tags > *").all_inner_texts()
                if tag.strip()
            ],
        }

    async def collect(self, cities: list[str], keywords: list[str], limit: int) -> list[dict]:
        if any(city not in CITY_CODES for city in cities):
            raise BrowserBlocked("仅支持杭州和上海岗位。")
        if limit <= 0:
            return []
        await self._require_ready()
        jobs = []
        added = set()
        streams = deque(self._city_jobs(city, keywords) for city in dict.fromkeys(cities))
        while streams and len(jobs) < limit:
            stream = streams.popleft()
            job = await anext(stream, None)
            if job is None:
                continue
            streams.append(stream)
            if job["job_key"] not in added:
                jobs.append(job)
                added.add(job["job_key"])
        return jobs

    async def _city_jobs(self, city: str, keywords: list[str]):
        examined = set()
        search_selectors = "a[href*='/job_detail/'], .job-list-empty, .search-empty, .job-empty"
        for keyword in keywords:
            query = urlencode({"city": CITY_CODES[city], "query": keyword})
            await self._visit(f"{SEARCH_URL}?{query}", search_selectors)
            links = await self._page.locator("a[href*='/job_detail/']:visible").evaluate_all(
                "(links) => links.map(link => link.href)"
            )
            for link in links:
                candidate = _job_url(link)
                if candidate is None or candidate[0] in examined:
                    continue
                key, url = candidate
                examined.add(key)
                try:
                    job = await self._read_job(url)
                except _JobClosed:
                    continue
                if job["city"] == city and all(
                    job[field] for field in ("title", "company", "recruiter", "description")
                ):
                    yield job

    async def _check_job(self, job: dict) -> None:
        await self._require_ready()
        target = _job_url(job["url"])
        if (
            target is None
            or target != (job["job_key"], job["url"])
            or job["city"] not in CITY_CODES
            or not job["recruiter"].strip()
        ):
            raise BrowserBlocked("岗位链接、城市或招聘者无法核验。")
        fresh = await self._read_job(target[1])
        if job_fingerprint(fresh) != job_fingerprint(job):
            raise BrowserBlocked("岗位详情已变化或无法读取，请重新采集并审核。")

    async def _require_new_contact(self) -> None:
        existing = self._page.locator(CONTACT_AREA).get_by_text(re.compile(r"^(继续沟通|已沟通)$"))
        if await existing.and_(self._page.locator(":visible")).count():
            raise _AlreadyContacted("该岗位已有沟通记录。")

    async def _start_chat_control(self, job: dict) -> Locator:
        await self._check_job(job)
        await self._require_new_contact()
        candidates = self._page.locator(CONTACT_AREA).locator("a.btn-startchat:visible, button.btn-startchat:visible")
        button = await _one(
            candidates.filter(has_text=re.compile(r"^\s*立即沟通\s*$")),
            "无法唯一定位 BOSS 的首次沟通按钮。",
        )
        if not await button.is_enabled():
            raise BrowserBlocked("该岗位的首次沟通按钮不可用。")
        return button

    async def _chat(self, job: dict, action: str) -> Locator:
        if action not in {"greet", "resume"}:
            raise BrowserBlocked("无法识别申请动作。")
        await self._check_job(job)
        if action == "greet":
            await self._require_new_contact()
        links = self._page.locator(CONTACT_AREA).get_by_role("link", name=re.compile(r"^(查看聊天|继续沟通)$"))
        link = await _one(
            links, "未找到可直接查看的聊天页面。立即沟通可能发送平台问候语，请先在 BOSS 手动完成首聊。"
        )
        href = await link.get_attribute("href")
        if not href:
            raise BrowserBlocked("聊天入口缺少可核验链接。")
        chat_url = urljoin(ORIGIN, href)
        parsed = urlsplit(chat_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        friend_ids = query.get("friendId", [])
        if (
            not _chat_url(chat_url)
            or parsed.fragment
            or query.keys() != {"friendId"}
            or len(friend_ids) != 1
            or not friend_ids[0]
        ):
            raise BrowserBlocked("聊天入口缺少明确的会话链接。")
        # Navigate to the link directly, without dispatching a job-page click.
        await self._visit(chat_url, f"{CHAT} {EDITOR}")
        if self._page.url != chat_url:
            raise BrowserBlocked("聊天页面发生重定向，无法确认会话。")
        pane = await _one(self._page.locator(f"{CHAT}:visible"), "无法唯一定位聊天面板。")
        await self._verify_thread(pane, job)
        if action == "greet" and await _visible(pane, f"{MESSAGES} :is({MESSAGE_ROWS})").count():
            raise _AlreadyContacted("该会话已有消息。")
        return pane

    async def _verify_thread(self, pane: Locator, job: dict) -> None:
        header = await _one(_visible(pane, HEADER), "无法读取聊天会话的对方信息。")
        links = _visible(header, "a[href*='/job_detail/']")
        await _one(links, "无法确认聊天会话对应的岗位。")
        href = await links.get_attribute("href")
        if (
            not href
            or _job_url(href) != (job["job_key"], job["url"])
            or await _field(header, ".boss-name") != job["recruiter"].strip()
            or await _field(header, ".company-name") != job["company"].strip()
            or (await links.inner_text()).strip() != job["title"].strip()
        ):
            raise BrowserBlocked("聊天会话中的岗位、企业或招聘者与确认清单不同。")
        await _one(_visible(pane, MESSAGES), "无法读取聊天消息记录。")

    async def _greet_controls(self, pane: Locator) -> tuple[Locator, Locator]:
        editor = await _one(_visible(pane, EDITOR), "无法唯一定位消息编辑区。")
        control = await _one(
            _visible(editor, "textarea, [contenteditable='true'][role='textbox']"),
            "无法唯一定位手动消息输入框。",
        )
        send = await _one(editor.get_by_role("button", name="发送", exact=True), "发送按钮无法唯一定位。")
        return control, send

    async def _resume_controls(self, pane: Locator, resume_name: str) -> tuple[Locator, Locator]:
        if not resume_name.strip():
            raise BrowserBlocked("请填写需要发送的完整附件简历名称。")
        editor = await _one(_visible(pane, EDITOR), "无法唯一定位聊天编辑区。")
        chooser = await _one(
            editor.get_by_role("button", name="选择附件简历", exact=True),
            "没有可确认的附件选择入口，请在 BOSS 聊天中检查附件简历。",
        )
        await chooser.click()
        dialog = _visible(pane, RESUME_CHOOSER)
        await dialog.first.wait_for()
        await _one(dialog, "无法唯一定位附件选择弹窗。")
        choice = await _one(
            dialog.get_by_role("radio", name=resume_name, exact=True),
            "未找到唯一同名的附件简历，请检查 BOSS 账户中的完整名称。",
        )
        send = await _one(
            dialog.get_by_role("button", name="发送附件", exact=True), "发送附件按钮无法唯一定位。"
        )
        if await choice.get_attribute("value") != resume_name:
            raise BrowserBlocked("附件选项与已确认文件名不一致。")
        return choice, send

    async def check(self, job: dict, action: str, resume_name: str) -> dict:
        try:
            if action == "start_chat":
                await self._start_chat_control(job)
            else:
                pane = await self._chat(job, action)
                if action == "greet":
                    await self._greet_controls(pane)
                else:
                    await self._resume_controls(pane, resume_name)
            await self._require_ready()
        except (BrowserBlocked, PlaywrightError) as error:
            current = await self.status()
            if current["state"] != "ready":
                return {"ok": False, "blocked": True, "reason": current["message"]}
            reason = str(error) if isinstance(error, BrowserBlocked) else "页面控件不可用或浏览器已关闭。"
            return {"ok": False, "reason": reason}
        return {"ok": True, "reason": "岗位、招聘者和聊天控件均已核验。"}

    async def perform(self, job: dict, action: str, message: str, resume_name: str) -> dict:
        if action == "start_chat":
            if message or resume_name:
                return {
                    "status": "blocked",
                    "dispatched": False,
                    "detail": "首次沟通只使用 BOSS 账户当前的预设招呼。",
                }
            return await self._perform_start_chat(job)
        try:
            pane = await self._chat(job, action)
            if action == "greet":
                if not message.strip():
                    raise BrowserBlocked("批准的问候语为空。")
                control, send = await self._greet_controls(pane)
                if await control.evaluate("(el) => el.value ?? el.innerText"):
                    raise BrowserBlocked("聊天输入框已有草稿，请在 BOSS 中处理草稿后重新检查。")
                await control.fill(message)
                if await control.evaluate("(el) => el.value ?? el.innerText") != message:
                    raise BrowserBlocked("消息输入框显示的内容与批准文本不一致。")
                expected, field, success = message, ".message-text", "greeted"
            else:
                choice, send = await self._resume_controls(pane, resume_name)
                await choice.check()
                if not await choice.is_checked():
                    raise BrowserBlocked("无法确认已选择批准的附件简历。")
                expected, field, success = resume_name, ".attachment-name", "resume_sent"
            await self._require_ready()
            await self._verify_thread(pane, job)
            if action == "greet" and await _visible(pane, f"{MESSAGES} :is({MESSAGE_ROWS})").count():
                raise _AlreadyContacted("发送前检测到该会话已有消息。")
            outgoing = f"{CHAT} {MESSAGES} {OUTGOING} {field}"
            previous = await self._page.locator(f"{outgoing}:visible").all_inner_texts()
            exact_before = previous.count(expected)
            send_button = await send.element_handle()
        except _AlreadyContacted as error:
            return {"status": "skipped", "dispatched": False, "detail": str(error)}
        except (BrowserBlocked, PlaywrightError) as error:
            detail = str(error) if isinstance(error, BrowserBlocked) else "发送前页面变化、控件不可用或浏览器已关闭。"
            return {"status": "blocked", "dispatched": False, "detail": detail}

        if paused := self._pause_outcome():
            return paused
        try:
            await send_button.click(timeout=ELEMENT_TIMEOUT_MS)
            await self._page.wait_for_function(
                """([selector, expected, before]) => Array.from(document.querySelectorAll(selector))
                 .filter(el => el.getClientRects().length && getComputedStyle(el).visibility === 'visible'
                   && el.innerText === expected).length === before + 1""",
                arg=[outgoing, expected, exact_before],
                timeout=SEND_CONFIRM_TIMEOUT_MS,
            )
            await self._require_ready()
            await self._verify_thread(pane, job)
        except (BrowserBlocked, PlaywrightError):
            return {"status": "uncertain", "detail": "未能确认新发出的记录，请在 BOSS 聊天中核对发送结果。"}
        return {"status": success, "detail": "已在确认的会话中看到新发出的记录。"}

    def _pause_outcome(self) -> dict | None:
        if not self.before_send():
            return {
                "status": "paused",
                "dispatched": False,
                "detail": "发送前检测到暂停或执行条件已变化，请重新检查清单。",
            }
        return None

    async def _perform_start_chat(self, job: dict) -> dict:
        try:
            button = await self._start_chat_control(job)
            await self._require_ready()
            await self._require_new_contact()
            target = await button.element_handle()
            original = self._page
            before = set(self._context.pages)
            if await _start_chat_receipt(original).count():
                raise BrowserBlocked("页面已有招呼发送提示，请在提示消失后重新检查。")
        except _AlreadyContacted as error:
            return {"status": "skipped", "dispatched": False, "detail": str(error)}
        except (BrowserBlocked, PlaywrightError) as error:
            detail = str(error) if isinstance(error, BrowserBlocked) else "首次沟通按钮不可用。"
            return {"status": "blocked", "dispatched": False, "detail": detail}

        if paused := self._pause_outcome():
            return paused
        try:
            await target.click(timeout=ELEMENT_TIMEOUT_MS)
            deadline = asyncio.get_running_loop().time() + SEND_CONFIRM_TIMEOUT_MS / 1000
            empty_chats = set()
            while asyncio.get_running_loop().time() < deadline:
                opened = [page for page in self._context.pages if page not in before]
                for page in [original, *opened]:
                    if page.is_closed() or not _chat_url(page.url):
                        continue
                    pane = page.locator(f"{CHAT}:visible")
                    if await pane.count() != 1 or await _visible(pane, MESSAGES).count() != 1:
                        continue
                    chat = (page, page.url)
                    message_count = await _visible(pane, f"{MESSAGES} :is({MESSAGE_ROWS})").count()
                    receipt_count = await _start_chat_receipt(page).count()
                    # A loaded history or confirmation provides no evidence about this click.
                    if message_count == 0 and receipt_count == 0:
                        empty_chats.add(chat)
                        continue
                    if chat not in empty_chats or message_count != 1 or receipt_count != 1:
                        continue
                    outgoing = _visible(pane, f"{MESSAGES} {OUTGOING} .message-text")
                    greeting = await outgoing.all_inner_texts()
                    if len(greeting) != 1 or not greeting[0].strip():
                        continue
                    await self._verify_thread(pane, job)
                    self._page = page
                    await self._require_ready()
                    return {"status": "greeted", "detail": f"BOSS 新会话显示已发送招呼「{greeting[0]}」。"}
                await asyncio.sleep(0.15)
        except (BrowserBlocked, PlaywrightError):
            pass
        return {"status": "uncertain", "detail": "未能确认 BOSS 首次沟通的发送记录，请在浏览器中核对。"}

    async def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = self._page = self._playwright = None
        try:
            if context is not None:
                await context.close()
        finally:
            if playwright is not None:
                await playwright.stop()
