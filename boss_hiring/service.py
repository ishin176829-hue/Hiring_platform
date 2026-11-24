import asyncio
from contextlib import suppress

from .matching import ModelError, RANKING_VERSION, TIERS, assess_job, candidate_jobs
from .store import ActionInterval, ChangedConstraints, DuplicateAttempt, QuotaExceeded, job_fingerprint


class Service:
    def __init__(self, store, browser):
        self.store, self.browser = store, browser
        self.task = None
        self.active_id = None
        self.browser_opening = False
        self.pause_generation = 0
        self.run_generation = 0
        self.pause_event = asyncio.Event()
        self.dispatch_item = None
        if hasattr(browser, "before_send"):
            browser.before_send = self._before_send

    def _before_send(self):
        if not self.can_dispatch() or self.dispatch_item is None:
            return False
        item_id, job = self.dispatch_item
        return self.store.confirm_dispatch(item_id, job)

    def can_dispatch(self):
        return not self.store.paused() and self.run_generation == self.pause_generation

    async def _wait(self, seconds):
        try:
            await asyncio.wait_for(self.pause_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _reserve(self, item_id):
        while self.can_dispatch():
            settings = self.store.settings()
            try:
                self.store.reserve(item_id, settings["daily_limit"], settings["interval_seconds"])
                return True
            except ActionInterval as exc:
                await self._wait(exc.seconds)
            except DuplicateAttempt as exc:
                self.store.item_result(item_id, "skipped", str(exc))
                return False
            except ChangedConstraints as exc:
                self.store.item_result(item_id, "skipped", str(exc))
                return False
            except QuotaExceeded as exc:
                self.store.set_paused(True)
                self.store.event("quota", str(exc))
                return False
        return False

    def _available(self):
        if self.task and not self.task.done():
            raise ValueError("已有任务运行中，请等待结束或暂停")
        if self.browser_opening:
            raise ValueError("浏览器正在打开，请稍后再启动任务")
        if self.store.paused():
            raise ValueError("机器人已暂停，请先解除暂停")

    def start_collection(self):
        self._available()
        task_id = self.store.new_task("collect")
        self.active_id = task_id
        self.task = asyncio.create_task(self._collect(task_id))
        return task_id

    def start_matching(self, keywords=None):
        self._available()
        profile = self.store.match_profile()
        if not profile:
            raise ValueError("请先在工作台填写并保存简历")
        if not profile["external_consent"]:
            raise ValueError("请先在工作台允许使用所配置的模型")
        settings = self.store.settings()
        cities = [city for city in profile["cities"] if city in settings["cities"]]
        if not cities:
            raise ValueError("求职城市与工作台采集城市没有交集，请检查设置")
        task_id = self.store.new_task("match")
        self.active_id = task_id
        self.task = asyncio.create_task(self._match(task_id, profile, settings, cities, keywords))
        return task_id

    async def _match(self, task_id, profile, settings, cities, keywords):
        try:
            scope, collection_notice = "existing", ""
            if keywords:
                try:
                    jobs = await self.browser.collect(cities, keywords, settings["collect_limit"])
                    if jobs:
                        self.store.upsert_jobs(jobs)
                        scope = "updated"
                    else:
                        collection_notice = "本次采集没有可核实的岗位，仅使用已有岗位。"
                except Exception as exc:
                    collection_notice = f"本次采集未完成，仅使用已有岗位。{self._error(exc)}"
            candidates = candidate_jobs(self.store.list_jobs(), profile, settings)
            results, errors = [], []
            consecutive_errors = 0
            for job, pending in candidates:
                current = self.store.match_profile()
                if not current or current["version"] != profile["version"]:
                    self.store.finish_task(task_id, "interrupted", "简历或偏好已更新，请重新匹配")
                    return
                fields = {key: job[key] for key in ("id", "title", "company", "city", "salary", "url", "collected_at", "contact_status")}
                try:
                    assessment = await assess_job(profile, job, pending)
                except ModelError as exc:
                    consecutive_errors += 1
                    errors.append({"id": job["id"], "title": job["title"], "reason": str(exc)})
                    if consecutive_errors >= 3 or str(exc).startswith("尚未配置"):
                        break
                    continue
                consecutive_errors = 0
                results.append({**fields, **assessment, "fingerprint": job_fingerprint(job)})
            results.sort(key=lambda item: (-TIERS[item["tier"]], item["core_coverage"], item["bonus_hits"], item["collected_at"], item["id"]), reverse=True)
            preferences = {key: profile[key] for key in ("cities", "min_salary_k", "outsourcing", "exclude_companies")}
            result = {"scope": scope, "rule_version": RANKING_VERSION, "jobs": results, "errors": errors,
                      "preferences": preferences, "keywords": keywords or [], "candidate_count": len(candidates),
                      "collection_notice": collection_notice,
                      "filters": {key: settings[key] for key in ("cities", "exclude_companies")},
                      "unprocessed": len(candidates) - len(results) - len(errors)}
            if not self.store.save_match(task_id, profile["version"], result):
                self.store.finish_task(task_id, "interrupted", "简历或偏好已更新，请重新匹配")
                return
            failure = f"，{len(errors)} 条待重试" if errors else ""
            state = "blocked" if errors and not results else "completed"
            detail = f"匹配完成，{len(results)} 条岗位{failure}" if candidates else "目前没有符合条件的可匹配岗位"
            self.store.finish_task(task_id, state, f"{collection_notice}{detail}")
        except asyncio.CancelledError:
            self.store.finish_task(task_id, "interrupted", "简历匹配已中止")
            raise
        except Exception as exc:
            self.store.finish_task(task_id, "blocked", self._error(exc))

    def pause(self):
        self.pause_generation += 1
        self.pause_event.set()
        self.store.set_paused(True)
        active = next((t for t in self.store.tasks() if t["id"] == self.active_id), None)
        if active and active["kind"] in {"collect", "match"} and self.task and not self.task.done():
            self.task.cancel()

    async def _collect(self, task_id):
        try:
            settings = self.store.settings()
            jobs = await self.browser.collect(settings["cities"], settings["keywords"], settings["collect_limit"])
            if not jobs:
                self.store.finish_task(task_id, "blocked", "未采集到可验证的岗位，请检查浏览器登录、筛选条件及页面状态")
                return
            count = self.store.upsert_jobs(jobs)
            self.store.finish_task(task_id, "completed", f"采集完成，保存或更新 {count} 个岗位；请检查实际城市与岗位详情")
        except asyncio.CancelledError:
            self.store.finish_task(task_id, "interrupted", "采集任务已中止")
            raise
        except Exception as exc:
            self.store.finish_task(task_id, "blocked", self._error(exc))

    @staticmethod
    def _error(exc):
        if type(exc).__name__ == "BrowserBlocked":
            return str(exc)[:1000]
        return f"操作未完成（{type(exc).__name__}），请检查浏览器状态与本地日志"

    def start_batch(self, batch_id):
        self._available()
        self.store.claim_batch(batch_id)
        self.run_generation = self.pause_generation
        self.pause_event.clear()
        task_id = self.store.new_task("batch")
        self.active_id = task_id
        self.task = asyncio.create_task(self._execute(task_id, batch_id))
        return task_id

    async def _execute(self, task_id, batch_id):
        current = None
        try:
            batch = self.store.batch(batch_id)
            pending = [item for item in batch["items"] if item["status"] == "pending"]
            for item in pending:
                if not self.can_dispatch():
                    break
                job = item["job"]
                eligible, reason = self.store.eligibility(job)
                fresh_stored = self.store.get_job(item["job_id"])
                if not eligible or job_fingerprint(fresh_stored) != job_fingerprint(job):
                    self.store.item_result(item["id"], "skipped", reason if not eligible else "岗位内容已变化，请重新采集并建立清单")
                    continue
                check = await self.browser.check(job, batch["action"], batch["resume_name"])
                if not check.get("ok"):
                    if check.get("blocked"):
                        self.store.set_paused(True)
                        self.store.event("browser", check.get("reason", "浏览器需要人工处理"))
                        break
                    self.store.item_result(item["id"], "skipped", check.get("reason", "页面前置检查未通过"))
                    continue
                if not self.can_dispatch():
                    break
                if batch["mode"] == "dry_run":
                    self.store.item_result(item["id"], "dry_run", "演练检查通过，未发送消息或附件")
                    continue
                if not await self._reserve(item["id"]):
                    if not self.can_dispatch():
                        break
                    continue
                current = item["id"]
                self.dispatch_item = (current, {**job, "id": item["job_id"]})
                try:
                    result = await self.browser.perform(job, batch["action"], batch["message"], batch["resume_name"])
                    status = result.get("status", "uncertain")
                    if status in {"paused", "blocked", "skipped"} and result.get("dispatched") is False:
                        self.store.cancel_undispatched(current)
                        if status == "paused":
                            current = None
                            break
                    expected = "resume_sent" if batch["action"] == "resume" else "greeted"
                    if status not in {expected, "skipped", "uncertain", "blocked"}:
                        status = "uncertain"
                    self.store.item_result(current, status, result.get("detail", "未得到可核验的页面反馈"))
                except Exception:
                    status = "uncertain"
                    self.store.item_result(current, status, "发送过程发生中断，结果待人工核对，不会自动重试")
                current = None
                if status in {"uncertain", "blocked"}:
                    self.store.set_paused(True)
                    break
            self.store.finish_batch(batch_id)
            state = self.store.batch(batch_id)["status"]
            self.store.finish_task(task_id, state, f"清单 #{batch_id}：{state}；请查看逐项结果")
        except asyncio.CancelledError:
            if current:
                self.store.item_result(current, "uncertain", "服务关闭时操作尚未确认，请人工核对")
                self.store.set_paused(True)
            self.store.finish_batch(batch_id)
            self.store.finish_task(task_id, "interrupted", "清单执行已中止，未确认项目不会自动重发")
            raise
        except Exception as exc:
            if current:
                self.store.item_result(current, "uncertain", "执行结果无法核对，不会自动重试")
            self.store.set_paused(True)
            self.store.finish_batch(batch_id)
            self.store.finish_task(task_id, "blocked", self._error(exc))
        finally:
            self.dispatch_item = None

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        await self.browser.close()
