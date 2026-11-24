import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .config import SearchSettings
from .matching import hard_conditions

FIELDS = ("job_key", "url", "title", "company", "city", "salary", "experience", "education", "description", "recruiter")
ITEM_STATES = {"pending", "sending", "greeted", "resume_sent", "dry_run", "skipped", "uncertain", "blocked"}


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def day():
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def job_fingerprint(job):
    normalized = {key: " ".join(str(job.get(key, "")).split()) for key in FIELDS}
    return hashlib.sha256(json_text(normalized).encode()).hexdigest()


def normalize_job(raw):
    result = {}
    for key in FIELDS:
        value = raw.get(key, "")
        if not isinstance(value, str) or len(value) > (100_000 if key == "description" else 2000):
            raise ValueError(f"岗位字段 {key} 格式或长度不正确")
        result[key] = value.strip()
    parsed = urlsplit(result["url"])
    match = re.fullmatch(r"/job_detail/([A-Za-z0-9_~=\-]+)\.html", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "www.zhipin.com" or not match:
        raise ValueError("仅接受 BOSS 官方 HTTPS 岗位详情链接")
    if result["job_key"] and result["job_key"] != match.group(1):
        raise ValueError("岗位 ID 与链接不一致")
    result["job_key"] = match.group(1)
    result["url"] = "https://www.zhipin.com" + parsed.path
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 50 or any(not isinstance(t, str) or len(t) > 100 for t in tags):
        raise ValueError("岗位标签格式不正确")
    result["tags"] = tags
    return result


class QuotaExceeded(ValueError):
    pass


class DuplicateAttempt(ValueError):
    pass


class ActionInterval(ValueError):
    def __init__(self, seconds):
        super().__init__("尚未达到本工具设置的发送间隔")
        self.seconds = seconds


class ChangedConstraints(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, job_key TEXT UNIQUE NOT NULL,
                    data TEXT NOT NULL, collected_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, digest TEXT NOT NULL,
                    status TEXT NOT NULL, action TEXT NOT NULL, message TEXT NOT NULL,
                    resume_name TEXT NOT NULL, mode TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL,
                    job_id INTEGER NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    detail TEXT NOT NULL DEFAULT '', FOREIGN KEY(batch_id) REFERENCES batches(id));
                CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, item_id INTEGER UNIQUE NOT NULL,
                    job_key TEXT NOT NULL, action TEXT NOT NULL, local_day TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(job_key,action));
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
                    kind TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    status TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS match_runs (id TEXT PRIMARY KEY, profile_version TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS match_shortlists (batch_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                    preferences TEXT NOT NULL, FOREIGN KEY(batch_id) REFERENCES batches(id));
            """)

    @contextmanager
    def connection(self, write=False):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def settings(self):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
        return SearchSettings.model_validate_json(row[0]).model_dump() if row else SearchSettings().model_dump()

    @staticmethod
    def _profile(conn):
        row = conn.execute("SELECT value FROM meta WHERE key='match_profile'").fetchone()
        return json.loads(row[0]) if row else None

    def match_profile(self):
        with self.connection() as conn:
            return self._profile(conn)

    def save_profile(self, data):
        profile = {**data, "version": uuid.uuid4().hex}
        with self.connection(True) as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('match_profile',?)", (json_text(profile),))
        return profile

    def delete_profile(self):
        with self.connection(True) as conn:
            conn.execute("DELETE FROM match_runs")
            conn.execute("DELETE FROM meta WHERE key='match_profile'")

    def save_match(self, task_id, profile_version, result):
        with self.connection(True) as conn:
            profile = self._profile(conn)
            if not profile or profile["version"] != profile_version:
                return False
            conn.execute("INSERT INTO match_runs VALUES (?,?,?,?)", (task_id, profile_version, json_text(result), now()))
        return True

    def latest_match(self):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM match_runs ORDER BY rowid DESC LIMIT 1").fetchone()
            if not row:
                return None
            profile = self._profile(conn)
            result = json.loads(row["data"])
            for item in result["jobs"]:
                job = conn.execute("SELECT data FROM jobs WHERE id=?", (item["id"],)).fetchone()
                item["stale"] = not job or job_fingerprint(json.loads(job[0])) != item["fingerprint"]
        settings = self.settings()
        stale = not profile or profile["version"] != row["profile_version"] or any(
            settings.get(key) != value for key, value in result.get("filters", {}).items())
        return {**result, "id": row["id"], "created_at": row["created_at"], "stale": stale}

    def update_settings(self, values):
        validated = SearchSettings.model_validate(values).model_dump()
        with self.connection(True) as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('settings',?)", (json_text(validated),))
        self.event("settings", "已更新采集与执行设置")
        return validated

    def paused(self):
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='paused'").fetchone()
        return bool(row and row[0] == "1")

    def set_paused(self, value):
        with self.connection(True) as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('paused',?)", ("1" if value else "0",))
        self.event("pause" if value else "resume", "已暂停后续操作" if value else "已解除暂停；需要手动继续原清单")

    def eligibility(self, job, settings=None, *, preferences=None):
        settings = settings or self.settings()
        if job["city"] not in settings["cities"]:
            return False, "岗位实际城市不在所选范围，或尚未确认"
        if not all(job[k] for k in ("title", "company", "description", "recruiter")):
            return False, "缺少职位详情或招聘者，请先补全采集"
        if any(word.casefold() in job["company"].casefold() for word in settings["exclude_companies"]):
            return False, "公司命中排除条件"
        if preferences is not None:
            allowed, _ = hard_conditions(job, preferences, settings)
            return allowed, "已从简历匹配选入" if allowed else "岗位不符合本清单的求职偏好"
        haystack = " ".join(str(job.get(k, "")) for k in ("title", "description", "tags")).casefold()
        hits = [word for word in settings["keywords"] if word.casefold() in haystack]
        if not hits:
            return False, "未命中岗位方向关键词"
        return True, "关键词命中：" + "、".join(hits)

    def upsert_jobs(self, jobs):
        if len(jobs) > 100:
            raise ValueError("一次最多导入 100 个岗位")
        normalized = [normalize_job(job) for job in jobs]
        unique = {job["job_key"]: job for job in normalized}
        with self.connection(True) as conn:
            for job in unique.values():
                conn.execute("INSERT INTO jobs(job_key,data,collected_at) VALUES(?,?,?) ON CONFLICT(job_key) DO UPDATE SET data=excluded.data,collected_at=excluded.collected_at",
                             (job["job_key"], json_text(job), now()))
        if unique:
            self.event("jobs", f"已保存或更新 {len(unique)} 个岗位")
        return len(unique)

    def _job(self, row, settings, attempted):
        job = json.loads(row["data"])
        eligible, reason = self.eligibility(job, settings)
        return {**job, "id": row["id"], "collected_at": row["collected_at"], "eligible": eligible,
                "filter_reason": reason, "contact_status": attempted.get(job["job_key"], "未执行")}

    def list_jobs(self, city="", q=""):
        settings = self.settings()
        with self.connection() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
            attempted = {r["job_key"]: r["status"] for r in conn.execute("SELECT a.job_key,i.status FROM attempts a JOIN items i ON i.id=a.item_id ORDER BY a.id")}
        jobs = [self._job(row, settings, attempted) for row in rows]
        return [j for j in jobs if (not city or j["city"] == city) and
                (not q or q.casefold() in (j["title"] + j["company"] + j["description"]).casefold())]

    def get_job(self, job_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise ValueError("岗位不存在")
        return {**json.loads(row["data"]), "id": row["id"], "collected_at": row["collected_at"]}

    @staticmethod
    def _batch_preferences(conn, batch_id):
        row = conn.execute("SELECT preferences FROM match_shortlists WHERE batch_id=?", (batch_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def create_batch(self, ids, action, message, resume_name, mode, match_run_id=None):
        if not ids or len(ids) > 100 or len(set(ids)) != len(ids):
            raise ValueError("请选择 1–100 个不重复的岗位")
        if action not in {"start_chat", "greet", "resume"} or mode not in {"dry_run", "live"}:
            raise ValueError("动作或执行模式不正确")
        if action == "greet" and (not message.strip() or len(message) > 1000):
            raise ValueError("请填写要发送的问候语，最多 1000 字")
        if action == "resume" and (not resume_name.strip() or len(resume_name) > 200):
            raise ValueError("请填写 BOSS 账号中已有附件简历的完整名称")
        settings = self.settings()
        with self.connection(True) as conn:
            preferences, recommendations = None, {}
            if match_run_id is not None:
                run = conn.execute("SELECT * FROM match_runs WHERE id=?", (match_run_id,)).fetchone()
                profile = self._profile(conn)
                if not run or not profile or profile["version"] != run["profile_version"]:
                    raise ValueError("推荐已过期，请重新匹配")
                result = json.loads(run["data"])
                if any(settings.get(key) != value for key, value in result.get("filters", {}).items()):
                    raise ValueError("筛选条件已更新，请重新匹配")
                preferences = result["preferences"]
                recommendations = {item["id"]: item for item in result["jobs"]}
            snapshots = []
            for job_id in ids:
                row = conn.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row:
                    raise ValueError(f"岗位 {job_id} 不存在")
                snapshot = json.loads(row[0])
                if preferences is not None:
                    recommendation = recommendations.get(job_id)
                    if not recommendation or job_fingerprint(snapshot) != recommendation["fingerprint"]:
                        raise ValueError("所选岗位不在本次推荐中或已有变更，请重新匹配")
                    snapshot["match_checks"] = recommendation["checks"]
                eligible, reason = self.eligibility(snapshot, settings, preferences=preferences)
                if not eligible:
                    raise ValueError(f"{snapshot['title']}：{reason}")
                snapshots.append(snapshot)
            digest = hashlib.sha256(json_text(dict(ids=ids, jobs=snapshots, action=action, message=message,
                                                  resume_name=resume_name, mode=mode, match_run_id=match_run_id,
                                                  match_preferences=preferences)).encode()).hexdigest()
            batch_id = conn.execute("INSERT INTO batches(digest,status,action,message,resume_name,mode,created_at) VALUES(?,'draft',?,?,?,?,?)",
                                    (digest, action, message, resume_name, mode, now())).lastrowid
            conn.executemany("INSERT INTO items(batch_id,job_id,snapshot) VALUES(?,?,?)",
                             [(batch_id, job_id, json_text(snapshot)) for job_id, snapshot in zip(ids, snapshots)])
            if preferences is not None:
                conn.execute("INSERT INTO match_shortlists VALUES (?,?,?)", (batch_id, match_run_id, json_text(preferences)))
        self.event("batch", f"已创建清单 #{batch_id}，模式 {mode}，{len(ids)} 个岗位")
        return self.batch(batch_id)

    def batch(self, batch_id):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError("清单不存在")
            result = dict(row)
            result["match_preferences"] = self._batch_preferences(conn, batch_id)
            result["items"] = []
            for item in conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY id", (batch_id,)):
                item = dict(item)
                item["job"] = json.loads(item.pop("snapshot"))
                result["items"].append(item)
        return result

    def batches(self):
        with self.connection() as conn:
            ids = [row[0] for row in conn.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 100")]
        return [self.batch(batch_id) for batch_id in ids]

    def approve(self, batch_id, digest, acknowledge_live):
        with self.connection(True) as conn:
            row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError("清单不存在")
            if row["status"] != "draft" or row["digest"] != digest:
                raise ValueError("清单已变化或已确认，请重新检查清单")
            if row["mode"] == "live" and acknowledge_live is not True:
                raise ValueError("真实发送需要勾选确认收件人及发送内容")
            conn.execute("UPDATE batches SET status='approved' WHERE id=?", (batch_id,))
        self.event("approval", f"已人工确认清单 #{batch_id}，模式 {row['mode']}")
        return self.batch(batch_id)

    def claim_batch(self, batch_id):
        with self.connection(True) as conn:
            row = conn.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
            if not row:
                raise KeyError("清单不存在")
            if row[0] not in {"approved", "paused"}:
                raise ValueError("清单尚未确认、已在运行或已结束")
            pending = conn.execute("SELECT 1 FROM items WHERE batch_id=? AND status='pending'", (batch_id,)).fetchone()
            if not pending:
                raise ValueError("没有可以继续执行的项目；不明确的发送结果不能自动重试")
            conn.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))

    def reserve(self, item_id, daily_limit, interval_seconds=0):
        with self.connection(True) as conn:
            row = conn.execute("SELECT i.*,b.mode,b.action,b.status batch_status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)).fetchone()
            if not row or row["status"] != "pending" or row["mode"] != "live" or row["batch_status"] not in {"approved", "running"}:
                raise ValueError("项目当前不能进行真实发送")
            key = json.loads(row["snapshot"])["job_key"]
            setting_row = conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            settings = SearchSettings.model_validate_json(setting_row[0]).model_dump() if setting_row else SearchSettings().model_dump()
            snapshot = json.loads(row["snapshot"])
            eligible, reason = self.eligibility(snapshot, settings, preferences=self._batch_preferences(conn, row["batch_id"]))
            latest = conn.execute("SELECT data FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if not eligible or not latest or job_fingerprint(json.loads(latest[0])) != job_fingerprint(snapshot):
                raise ChangedConstraints(reason if not eligible else "岗位内容已变化，请重新建立清单")
            attempt_action = "greet" if row["action"] == "start_chat" else row["action"]
            if conn.execute("SELECT 1 FROM attempts WHERE job_key=? AND action=?", (key, attempt_action)).fetchone():
                raise DuplicateAttempt("该岗位此动作已有执行记录，不重复发送")
            used = conn.execute("SELECT COUNT(*) FROM attempts WHERE local_day=?", (day(),)).fetchone()[0]
            if used >= min(daily_limit, settings["daily_limit"]):
                raise QuotaExceeded("已达到本工具设置的每日尝试上限")
            last = conn.execute("SELECT created_at FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
            if last and interval_seconds:
                elapsed = (datetime.fromisoformat(now()) - datetime.fromisoformat(last[0])).total_seconds()
                if elapsed < interval_seconds:
                    raise ActionInterval(interval_seconds - elapsed)
            conn.execute("INSERT INTO attempts(item_id,job_key,action,local_day,created_at) VALUES(?,?,?,?,?)", (item_id, key, attempt_action, day(), now()))
            conn.execute("UPDATE items SET status='sending',detail='已记录发送意图，正在核验并执行' WHERE id=?", (item_id,))

    def cancel_undispatched(self, item_id):
        """Only for an adapter-confirmed pause before the actual dispatch click."""
        with self.connection(True) as conn:
            row = conn.execute("SELECT status FROM items WHERE id=?", (item_id,)).fetchone()
            if not row or row[0] != "sending":
                raise ValueError("项目没有可撤销的未发送意图")
            conn.execute("DELETE FROM attempts WHERE item_id=?", (item_id,))
            conn.execute("UPDATE items SET status='pending',detail='已在页面发送前暂停，尚未发送' WHERE id=?", (item_id,))
        self.event("pause", f"项目 #{item_id} 在页面发送前暂停，已撤销未执行的发送意图")

    def confirm_dispatch(self, item_id, job):
        """Authorize and timestamp the dispatch boundary, not the earlier reservation."""
        with self.connection(True) as conn:
            setting_row = conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
            settings = SearchSettings.model_validate_json(setting_row[0]).model_dump() if setting_row else SearchSettings().model_dump()
            own = conn.execute("SELECT a.id,i.status,i.batch_id FROM attempts a JOIN items i ON i.id=a.item_id WHERE a.item_id=?", (item_id,)).fetchone()
            if not own or own["status"] != "sending":
                return False
            eligible, _ = self.eligibility(job, settings, preferences=self._batch_preferences(conn, own["batch_id"]))
            latest = conn.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if not eligible or not latest or job_fingerprint(json.loads(latest[0])) != job_fingerprint(job):
                return False
            instant = now()
            local_day = datetime.fromisoformat(instant).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            others_today = conn.execute("SELECT COUNT(*) FROM attempts WHERE local_day=? AND id<>?", (local_day, own["id"])).fetchone()[0]
            if others_today >= settings["daily_limit"]:
                return False
            previous = conn.execute("SELECT created_at FROM attempts WHERE id<>? ORDER BY created_at DESC,id DESC LIMIT 1", (own["id"],)).fetchone()
            if previous and (datetime.fromisoformat(instant) - datetime.fromisoformat(previous[0])).total_seconds() < settings["interval_seconds"]:
                return False
            conn.execute("UPDATE attempts SET created_at=?,local_day=? WHERE id=?", (instant, local_day, own["id"]))
        return True

    def item_result(self, item_id, status, detail):
        if status not in ITEM_STATES - {"pending", "sending"}:
            raise ValueError("未知执行结果")
        with self.connection(True) as conn:
            conn.execute("UPDATE items SET status=?,detail=? WHERE id=?", (status, str(detail)[:2000], item_id))
            if status in {"uncertain", "blocked"}:
                conn.execute("INSERT OR REPLACE INTO meta VALUES('paused','1')")
        self.event("item", f"项目 #{item_id}：{status}，{str(detail)[:500]}")

    def finish_batch(self, batch_id):
        with self.connection(True) as conn:
            statuses = [row[0] for row in conn.execute("SELECT status FROM items WHERE batch_id=?", (batch_id,))]
            status = self._batch_state(statuses)
            conn.execute("UPDATE batches SET status=? WHERE id=?", (status, batch_id))

    @staticmethod
    def _batch_state(statuses):
        return "paused" if "pending" in statuses else "needs_review" if "uncertain" in statuses or "blocked" in statuses else "completed"

    def daily_used(self):
        with self.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM attempts WHERE local_day=?", (day(),)).fetchone()[0]

    def recover(self):
        with self.connection(True) as conn:
            interrupted = [r[0] for r in conn.execute("SELECT id FROM batches WHERE status='running' UNION SELECT batch_id FROM items WHERE status='sending'")]
            count = conn.execute("SELECT COUNT(*) FROM items WHERE status='sending'").fetchone()[0]
            conn.execute("UPDATE items SET status='uncertain',detail='服务中断，发送结果待人工核对，不会自动重试' WHERE status='sending'")
            for batch_id in interrupted:
                statuses = [r[0] for r in conn.execute("SELECT status FROM items WHERE batch_id=?", (batch_id,))]
                conn.execute("UPDATE batches SET status=? WHERE id=?", (self._batch_state(statuses), batch_id))
            conn.execute("UPDATE tasks SET status='interrupted',detail='服务中断，请核对后重新启动任务' WHERE status='running'")
            if count:
                conn.execute("INSERT OR REPLACE INTO meta VALUES('paused','1')")
        if count:
            self.event("recovery", f"{count} 条发送结果待核对，已暂停后续操作")

    def event(self, kind, detail):
        with self.connection(True) as conn:
            conn.execute("INSERT INTO events(created_at,kind,detail) VALUES(?,?,?)", (now(), kind, detail))

    def events(self, limit=100):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))]

    def new_task(self, kind):
        task_id = uuid.uuid4().hex
        with self.connection(True) as conn:
            conn.execute("INSERT INTO tasks VALUES(?,?,'running','',?)", (task_id, kind, now()))
        return task_id

    def finish_task(self, task_id, status, detail):
        with self.connection(True) as conn:
            conn.execute("UPDATE tasks SET status=?,detail=? WHERE id=?", (status, detail[:2000], task_id))
        self.event("task", detail)

    def tasks(self):
        with self.connection() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC,rowid DESC LIMIT 50")]
