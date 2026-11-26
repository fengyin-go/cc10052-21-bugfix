"""数据时效监控领域服务。

核心不变量：
* 无有效完成时间/观测时间就是“缺测”，不能解析失败后默认成 0 分钟或当前时间；
* 只有当前指标、当前窗口且已完成的作业才能恢复该窗口；
* 缺测异常凭证按监控记录稳定幂等，重试/重算不制造重复异常；
* 迟到观测只补充证据，不删除历史缺测凭证和报告申请轨迹。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.freshness_support import (
    append_timeline,
    as_evidence,
    first_value,
    iso,
    next_id,
    parse_datetime,
    utc_now,
)
from app.store import store

MISSING = "缺测"
OVERDUE = "逾期"
NORMAL = "正常"

LEGACY_STATUS = {"新鲜": NORMAL, "延迟": OVERDUE, "过期": OVERDUE}

RECHECK_ACTIONS = {
    "复查时效", "复查", "执行复查", "新检查", "执行新检查", "立即检查",
    "检查时效", "重算水位", "重新计算", "重试重算",
    "recheck", "check", "check_freshness", "recalc", "recalculate",
}
CONTINUE_ACTIONS = {
    "续办原复查", "继续原处置", "续办复查", "核对补报", "核对补报响应", "核对迟到证据",
    "continue", "continue_original", "verify_evidence",
}
REPORT_ACTIONS = {
    "申请缺测影响报告", "缺测影响报告", "申请影响报告", "重新申请", "产生影响报告", "发起缺测报告",
    "apply_impact_report", "request_impact_report", "create_report",
}
RECOVERY_ACTIONS = {"确认恢复", "恢复办结", "按恢复办结", "确认恢复并办结", "confirm_recovery", "recover", "close_recovery"}
CLAIM_ACTIONS = {"领取", "领取缺测", "领取缺测记录", "领取时效记录", "claim", "claim_missing"}


class DataFreshnessService(BaseModuleService):
    MODULE = "data_freshness"
    ENTITY = "时效记录"
    OPERATION_TERM = "复查"
    LIST_FIELDS = [
        "监控对象", "数据域", "最新水位", "延迟分钟", "时效等级",
        "指标编码", "计算窗口", "作业批次", "缺少的作业完成时间", "实际完成时间", "实际观测时间",
        "缺数原因", "告警凭证", "影响报告申请编号", "办理结论",
    ]
    REQUIRED_FIELDS = ["监控对象"]
    STATUS_ORDER = [MISSING, OVERDUE, NORMAL]
    ACTION_RULES = {
        "复查时效": OVERDUE,
        "重算水位": OVERDUE,
        "续办原复查": OVERDUE,
        "申请缺测影响报告": MISSING,
        "确认恢复": NORMAL,
        "忽略告警": OVERDUE,
    }
    NEGATIVE_ACTIONS = ["忽略告警"]

    # ---------- 查询与归一化 ----------

    def _normalize_row(self, row: dict[str, Any]) -> dict[str, Any]:
        status = str(row.get("status") or row.get("时效等级") or "").strip()
        if status in LEGACY_STATUS:
            row.setdefault("原时效状态", status)
            status = LEGACY_STATUS[status]
        if status not in self.STATUS_ORDER:
            # 没有有效状态的历史样例按其是否有有效水位判断；无法解析即为缺测。
            observed, err = parse_datetime(row.get("最新水位"))
            status = NORMAL if observed and err is None else MISSING
        row["status"] = status
        row["时效等级"] = status
        row.setdefault("pending", status != NORMAL)
        row.setdefault("abnormal", status == MISSING)
        row.setdefault("观测有效", status == NORMAL)
        return row

    def list_entries(self, **kwargs: Any):  # type: ignore[override]
        for row in store.rows(self.MODULE):
            self._normalize_row(row)
        return super().list_entries(**kwargs)

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = super().get_entry(entry_id)
        return self._normalize_row(row) if row else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        values = dict(values or {})
        metric = first_value(values, "指标编码", "metric_code")
        window = first_value(values, "计算窗口", "窗口", "window", "数据窗口")
        if not values.get("监控对象") and metric and window:
            values["监控对象"] = f"{metric}:{window}"
        values.setdefault("数据域", "指标计算")
        entry, missing = super().create_entry(values)
        if entry is not None:
            self._normalize_row(entry)
            self._identity(entry)
        return entry, missing

    def _identity(self, entry: dict[str, Any]) -> tuple[str, str, int | None]:
        metric = first_value(entry, "指标编码", "metric_code")
        window = first_value(entry, "计算窗口", "窗口", "window", "数据窗口")
        job_id = first_value(entry, "关联作业ID", "缺失作业ID", "job_id", "作业ID")
        try:
            job_id_int = int(job_id) if job_id not in (None, "") else None
        except (TypeError, ValueError):
            job_id_int = None

        if (not metric or not window) and job_id_int is not None:
            job = store.find("metric_compute", job_id_int)
            if job:
                metric = metric or job.get("指标编码")
                window = window or job.get("计算窗口")

        obj = str(entry.get("监控对象") or "")
        if (not metric or not window) and obj.startswith("metric_compute:"):
            raw_id = obj.split(":", 1)[1]
            try:
                job = store.find("metric_compute", int(raw_id))
            except ValueError:
                job = None
            if job:
                job_id_int = int(job["id"])
                metric = metric or job.get("指标编码")
                window = window or job.get("计算窗口")

        if (not metric or not window) and ":" in obj and not obj.startswith(("http:", "https:")):
            left, right = obj.split(":", 1)
            if not metric and (left.startswith(("METR", "M-", "metric")) or "-" in left):
                metric = left
            if not window:
                window = right

        if metric:
            entry["指标编码"] = str(metric)
        if window:
            entry["计算窗口"] = str(window)
        if job_id_int is not None:
            entry.setdefault("关联作业ID", job_id_int)
        return str(metric or ""), str(window or ""), job_id_int

    # ---------- 作业证据选择 ----------

    def _upsert_job_evidence(self, evidence: dict[str, Any]) -> dict[str, Any] | None:
        rows = store.rows("metric_compute")
        job: dict[str, Any] | None = None
        raw_id = evidence.get("id") or evidence.get("作业ID")
        if raw_id not in (None, ""):
            try:
                job = store.find("metric_compute", int(raw_id))
            except (TypeError, ValueError):
                job = None
        if job is None:
            batch = first_value(evidence, "作业批次", "批次", "job_batch")
            if batch:
                job = next((r for r in rows if str(r.get("作业批次") or r.get("批次")) == str(batch)), None)
        if job is None:
            job = {"id": next_id(rows)}
            rows.append(job)
        for key, value in evidence.items():
            if key != "id":
                job[key] = value
        if str(job.get("status") or "") not in ("待执行", "执行中", "已完成", "计算失败"):
            job["status"] = "已完成"
        if first_value(job, "完成时间", "实际完成时间", "作业完成时间", "completed_at", "finished_at"):
            job.setdefault("status", "已完成")
        if job.get("status") == "已完成":
            job["作业状态"] = "已完成"
        return job

    def _select_observation(
        self,
        entry: dict[str, Any],
        *,
        evidence: dict[str, Any] | None,
        metric: str,
        window: str,
        job_id: int | None,
    ) -> tuple[datetime | None, str | None, dict[str, Any] | None, Any]:
        # 先把补报响应中的作业证据落库，随后新检查才能用同一证据复核。
        evidence_job: dict[str, Any] | None = None
        if evidence:
            ev_metric = first_value(evidence, "指标编码", "metric_code")
            ev_window = first_value(evidence, "计算窗口", "窗口", "window")
            if metric and ev_metric and str(ev_metric) != metric:
                return None, f"证据指标 {ev_metric} 不属于当前指标 {metric}", None, None
            if window and ev_window and str(ev_window) != window:
                return None, f"证据窗口 {ev_window} 不属于当前窗口 {window}，晚到旧窗口不能恢复本窗口", None, None
            # 动作请求可以只带作业批次/完成时间；指标和窗口以当前时效行为准。
            if metric and not ev_metric:
                evidence["指标编码"] = metric
            if window and not ev_window:
                evidence["计算窗口"] = window
            evidence_job = self._upsert_job_evidence(evidence)

        jobs: list[dict[str, Any]]
        if job_id is None and (not metric or not window):
            # 身份不完整时不能猜测作业，更不能让任意已完成作业恢复本记录。
            jobs = []
        else:
            jobs = list(store.rows("metric_compute"))
            if metric:
                jobs = [j for j in jobs if str(j.get("指标编码") or "") == metric]
            if window:
                jobs = [
                    j for j in jobs
                    if str(first_value(j, "计算窗口", "窗口", "window") or "") == window
                ]
        if job_id is not None:
            specified = store.find("metric_compute", job_id)
            if specified is not None and (not metric or specified.get("指标编码") == metric) and (
                not window or str(first_value(specified, "计算窗口", "窗口", "window") or "") == window
            ):
                jobs = [specified]

        wanted_batch = first_value(evidence or {}, "作业批次", "批次") or entry.get("作业批次")
        if wanted_batch:
            batch_jobs = [j for j in jobs if str(j.get("作业批次") or j.get("批次") or "") == str(wanted_batch)]
            if batch_jobs:
                jobs = batch_jobs

        completed = [j for j in jobs if str(first_value(j, "status", "作业状态", "state") or "") in {"已完成", "completed", "succeeded", "success", "done"}]
        valid: list[tuple[datetime, dict[str, Any], Any]] = []
        problem: str | None = None
        for job in completed:
            raw_time = first_value(job, "完成时间", "实际完成时间", "作业完成时间", "completed_at", "finished_at", "实际观测时间", "observed_at", "actual_observed_at", "窗口水位")
            if raw_time in (None, ""):
                # 兼容旧字段：窗口水位在旧实现中被当作完成/水位时间。
                raw_time = job.get("窗口水位")
            dt, err = parse_datetime(raw_time)
            if dt is None:
                batch = job.get("作业批次") or job.get("id")
                if raw_time in (None, ""):
                    problem = f"作业 {batch} 已完成但缺少作业完成时间，不能当成新鲜观测"
                else:
                    problem = f"作业 {batch} 的作业完成时间无法解析（{err or '无效'}）：{raw_time!r}，不能当成新鲜观测"
                continue
            valid.append((dt, job, raw_time))

        if valid:
            observed_dt, job, raw_time = max(valid, key=lambda item: item[0])
            return observed_dt, None, job, raw_time

        if problem:
            return None, problem, (completed[-1] if completed else evidence_job), None

        if completed:
            job = completed[-1]
            batch = job.get("作业批次") or job.get("id")
            return None, f"作业 {batch} 已完成但缺少作业完成时间，无法确认观测时点", job, None

        if jobs:
            job = jobs[-1]
            batch = job.get("作业批次") or job.get("id")
            state = first_value(job, "status", "作业状态") or "状态未知"
            return None, f"作业 {batch} 当前为{state}，缺少可核对的已完成作业完成时间", job, None

        # 没有作业层记录时，监控行自身的有效水位/观测时间也可作为直接观测；
        # 但非法字符串必须报解析失败，不能回落成零延迟。
        raw_direct = first_value(
            entry,
            "实际观测时间", "观测时间", "实际完成时间", "作业完成时间", "完成时间", "最新水位",
        )
        dt, err = parse_datetime(raw_direct)
        if dt is not None:
            return dt, None, evidence_job, raw_direct
        if raw_direct not in (None, ""):
            return None, f"最新水位/实际观测时间{err or '无效'}：{raw_direct!r}", evidence_job, None
        target = f"{metric or '当前指标'} {window or '当前窗口'}".strip()
        return None, f"{target} 未找到已完成作业，缺少作业完成时间", evidence_job, None

    # ---------- 缺测异常凭证 ----------

    def _find_anomaly(self, entry_id: int, cert: str) -> dict[str, Any] | None:
        rows = store.rows("anomaly_metric")
        stable_no = f"AN-DF-{entry_id}"
        for row in rows:
            if str(row.get("时效记录ID") or "") == str(entry_id):
                return row
            if row.get("告警凭证") == cert or row.get("缺测凭证") == cert:
                return row
            if row.get("异常编号") == stable_no:
                return row
            source = str(row.get("异常编号") or "")
            if source.startswith("AN-DF-"):
                tail = source[len("AN-DF-"):].split("-")[0]
                if tail == str(entry_id):
                    return row
        return None

    def _ensure_anomaly(
        self,
        entry: dict[str, Any],
        reason: str,
        job: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> dict[str, Any]:
        entry_id = int(entry["id"])
        cert = str(entry.get("告警凭证") or entry.get("缺测凭证") or f"CERT-DF-{entry_id}")
        entry["告警凭证"] = cert
        entry["缺测凭证"] = cert
        entry["凭证"] = cert
        entry["credential"] = cert
        anomaly = self._find_anomaly(entry_id, cert)
        metric, window, _ = self._identity(entry)
        at_text = iso(at)
        if anomaly is None:
            rows = store.rows("anomaly_metric")
            anomaly = {
                "id": next_id(rows),
                "异常编号": f"AN-DF-{entry_id}",
                "告警凭证": cert,
                "缺测凭证": cert,
                "凭证": cert,
                "credential": cert,
                "异常类型": "缺测",
                "来源模块": self.MODULE,
                "时效记录ID": entry_id,
                "监控对象": entry.get("监控对象", ""),
                "指标编码": metric,
                "计算窗口": window,
                "基线区间": str(window or entry.get("数据域", "")),
                "观测值": "暂无有效完成作业/观测时间",
                "异常评分": "0.9",
                "缺数原因": reason,
                "关联作业ID": (job or {}).get("id", entry.get("关联作业ID", "")),
                "作业批次": (job or {}).get("作业批次") or (job or {}).get("批次") or entry.get("作业批次", ""),
                "实际观测时间": "",
                "作业完成时间": "",
                "证据": {
                    "freshness_id": entry_id,
                    "certificate": cert,
                    "metric": metric,
                    "window": window,
                    "job_id": (job or {}).get("id", entry.get("关联作业ID")),
                    "job_batch": (job or {}).get("作业批次") or (job or {}).get("批次") or entry.get("作业批次"),
                    "missing_reason": reason,
                },
                "status": "待确认",
                "pending": True,
                "abnormal": False,
                "登记时间": at_text,
            }
            rows.append(anomaly)
        else:
            anomaly.update({
                "告警凭证": cert,
                "缺测凭证": cert,
                "凭证": cert,
                "credential": cert,
                "异常类型": anomaly.get("异常类型") or "缺测",
                "时效记录ID": entry_id,
                "指标编码": metric or anomaly.get("指标编码", ""),
                "计算窗口": window or anomaly.get("计算窗口", ""),
                "缺数原因": reason,
            })
            evidence = anomaly.setdefault("证据", {})
            if isinstance(evidence, dict):
                evidence["latest_missing_reason"] = reason
        entry["异常编号"] = anomaly["异常编号"]
        append_timeline(entry, "已核对并登记真实缺测告警凭证", at=at_text, 异常编号=anomaly["异常编号"])
        return anomaly

    def _attach_observation_to_anomaly(
        self,
        entry: dict[str, Any],
        job: dict[str, Any] | None,
        observed_at: datetime,
        checked_at: datetime,
        delay: int,
    ) -> dict[str, Any] | None:
        cert = str(entry.get("告警凭证") or entry.get("缺测凭证") or f"CERT-DF-{entry['id']}")
        anomaly = self._find_anomaly(int(entry["id"]), cert)
        if anomaly is None:
            return None
        batch = ""
        job_id = ""
        if job:
            batch = str(first_value(job, "作业批次", "批次", "job_batch") or "")
            job_id = job.get("id", "")
        actual_completion = ""
        if job:
            raw_completion = first_value(job, "完成时间", "实际完成时间", "作业完成时间", "completed_at", "finished_at", "实际观测时间", "observed_at", "actual_observed_at", "窗口水位")
            actual_completion = str(raw_completion or "")
        obs_text = iso(observed_at)
        anomaly.update({
            "实际观测时间": obs_text,
            "本次检查时间": iso(checked_at),
            "作业完成时间": actual_completion or obs_text,
            "作业批次": batch or entry.get("作业批次", ""),
            "关联作业ID": job_id or entry.get("关联作业ID", ""),
            "job_batch": batch or entry.get("作业批次", ""),
            "actual_observed_at": obs_text,
            "actual_completion_time": actual_completion or obs_text,
            "delay_minutes": delay,
            "延迟分钟": str(delay),
        })
        evidence = anomaly.setdefault("证据", {})
        if isinstance(evidence, dict):
            evidence.update({
                "actual_observed_at": obs_text,
                "checked_at": iso(checked_at),
                "delay_minutes": delay,
                "job_batch": anomaly["作业批次"],
                "job_id": anomaly["关联作业ID"],
            })
        append_timeline(anomaly, "已追到实际观测时间和作业批次", 实际观测时间=obs_text, 作业批次=anomaly["作业批次"])
        entry["作业批次"] = anomaly["作业批次"]
        return anomaly

    # ---------- 复查主流程 ----------

    def recheck(
        self,
        entry_id: int,
        evidence: dict[str, Any] | str | int | None = None,
        check_time: Any = None,
        *,
        apply_report: bool = False,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        context = dict(context or {})
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": f"{self.ENTITY} {entry_id} 不存在"}

        raw_evidence = evidence
        if raw_evidence is None:
            raw_evidence = first_value(context, "evidence", "作业证据", "job", "作业", "补报响应")
        evidence_dict = as_evidence(raw_evidence)

        raw_check_time = check_time or first_value(context, "check_time", "checked_at", "检查时间", "复查时间", "as_of")
        checked_at, time_err = parse_datetime(raw_check_time) if raw_check_time not in (None, "") else (utc_now(), None)
        if checked_at is None:
            return {"ok": False, "http_status": 422, "message": f"检查时间无效：{time_err}"}

        metric, window, job_id = self._identity(entry)
        observed_at, problem, job, raw_observed = self._select_observation(
            entry, evidence=evidence_dict, metric=metric, window=window, job_id=job_id,
        )
        entry["复查时间"] = iso(checked_at)
        entry["检查时间"] = iso(checked_at)

        if observed_at is None:
            reason = problem or "缺少有效观测"
            entry.update({
                "status": MISSING,
                "时效等级": MISSING,
                "pending": True,
                "abnormal": True,
                "观测有效": False,
                "缺数原因": reason,
            })
            if job:
                entry["关联作业ID"] = job.get("id", entry.get("关联作业ID", ""))
                batch = first_value(job, "作业批次", "批次")
                if batch:
                    entry["作业批次"] = batch
                entry["缺少的作业完成时间"] = f"作业 {batch or job.get('id')} 未提供可解析的完成时间"
            else:
                entry["缺少的作业完成时间"] = reason
            anomaly = self._ensure_anomaly(entry, reason, job, checked_at)
            report = None
            if apply_report or context.get("apply_report") or context.get("auto_apply_report"):
                report_result = self.apply_impact_report(entry_id, context=context)
                report = report_result.get("report")
            append_timeline(entry, "复查仍为缺测", at=iso(checked_at), 原因=reason)
            return {
                "ok": True,
                "message": f"复查完成：{MISSING}。{reason}",
                "status": MISSING,
                "missing": True,
                "fresh": False,
                "entry": entry,
                "anomaly": anomaly,
                "anomaly_id": anomaly.get("id"),
                "credential": anomaly.get("告警凭证") or entry.get("告警凭证", ""),
                "report": report,
                "evidence_check": {"matched": False, "reason": reason},
            }

        delay_seconds = (checked_at - observed_at).total_seconds()
        delay = max(0, int(math.floor(delay_seconds / 60.0)))
        try:
            threshold = int(first_value(entry, "时效阈值分钟", "SLA分钟", "sla_minutes") or context.get("sla_minutes") or 30)
        except (TypeError, ValueError):
            threshold = 30
        status = NORMAL if delay <= threshold else OVERDUE
        strict_category = NORMAL if delay == 0 else OVERDUE
        legacy_level = NORMAL if delay <= 30 else ("延迟" if delay <= 120 else "过期")
        obs_text = iso(observed_at)
        completion_text = obs_text
        if job is not None:
            raw_completion = first_value(job, "完成时间", "实际完成时间", "作业完成时间", "completed_at", "finished_at", "实际观测时间", "observed_at", "actual_observed_at", "窗口水位")
            dt_completion, _ = parse_datetime(raw_completion)
            if dt_completion:
                completion_text = iso(dt_completion)
            batch = first_value(job, "作业批次", "批次", "job_batch")
            if batch:
                entry["作业批次"] = str(batch)
            entry["关联作业ID"] = job.get("id", entry.get("关联作业ID", ""))

        entry.update({
            "status": status,
            "时效等级": status,
            "新鲜度": "新鲜" if status == NORMAL else legacy_level,
            "延迟等级": legacy_level,
            "时效阈值分钟": threshold,
            "延迟分类": strict_category,
            "delay_category": strict_category,
            "pending": status != NORMAL,
            "abnormal": status == OVERDUE,
            "观测有效": True,
            "延迟分钟": str(delay),
            "延迟分钟数": delay,
            "delay_minutes": delay,
            "最新水位": obs_text,
            "实际观测时间": first_value(evidence_dict or {}, "实际观测时间", "观测时间") or obs_text,
            "实际完成时间": completion_text,
            "缺数原因": "",
        })
        append_timeline(entry, f"复查得到有效观测，延迟 {delay} 分钟，判定{status}", at=iso(checked_at))
        anomaly = self._attach_observation_to_anomaly(entry, job, observed_at, checked_at, delay)
        report = self._find_report(entry)
        conflict = self._build_conflict(entry, report, delay) if report and not report.get("办结类型") else None
        return {
            "ok": True,
            "message": f"复查完成：{status}，延迟 {delay} 分钟",
            "status": status,
            "missing": False,
            "fresh": status == NORMAL,
            "delay_minutes": delay,
            "entry": entry,
            "anomaly": anomaly,
            "anomaly_id": (anomaly or {}).get("id"),
            "credential": entry.get("告警凭证", ""),
            "report": report,
            "conflict": conflict,
            "冲突": conflict,
            "evidence_check": {
                "matched": True,
                "job_id": (job or {}).get("id"),
                "job_batch": entry.get("作业批次", ""),
                "actual_completion_time": completion_text,
                "actual_observed_at": entry["实际观测时间"],
                "checked_at": iso(checked_at),
                "delay_minutes": delay,
            },
        }

    # 保留旧方法名，兼容已有调用。
    def recalc_watermark(self, entry_id: int, check_time: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.recheck(entry_id, check_time=check_time, context=kwargs)

    # ---------- 影响报告与冲突处置 ----------

    def _find_report(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        entry_id = int(entry["id"])
        cert = entry.get("告警凭证") or f"CERT-DF-{entry_id}"
        code = f"RPT-MISS-{entry_id}"
        metric, window, _ = self._identity(entry)
        for report in store.rows("report_gen"):
            if report.get("时效记录ID") == entry_id or report.get("freshness_id") == entry_id or report.get("监控记录ID") == entry_id:
                return report
            if report.get("告警凭证") == cert or report.get("缺测凭证") == cert:
                return report
            if report.get("报告编码") == code:
                return report
            text = f"{report.get('报告类型', '')}{report.get('报告模板', '')}{report.get('报告名称', '')}"
            report_window = report.get("计算窗口") or report.get("数据窗口") or report.get("window")
            same_metric = not metric or not report.get("指标编码") or report.get("指标编码") == metric
            if window and str(report_window or "") == str(window) and same_metric and "缺测" in text:
                return report
        return None

    def apply_impact_report(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "时效记录不存在"}
        existing = self._find_report(entry)
        if existing is not None:
            return {
                "ok": True,
                "created": False,
                "duplicate": True,
                "initiated": False,
                "message": "缺测影响报告申请已存在，不重复发起",
                "entry": entry,
                "report": existing,
                "report_id": existing.get("id"),
                "report_code": existing.get("报告编码"),
            }
        if entry.get("恢复已确认") or entry.get("status") != MISSING:
            return {
                "ok": False,
                "created": False,
                "initiated": False,
                "code": "RECOVERED_OR_NOT_MISSING",
                "message": "当前不是待确认缺测或已经确认恢复，不再发起缺测影响报告",
                "entry": entry,
            }

        rows = store.rows("report_gen")
        cert = str(entry.get("告警凭证") or f"CERT-DF-{entry_id}")
        report = {
            "id": next_id(rows),
            "报告编码": f"RPT-MISS-{entry_id}",
            "实验编码": context.get("实验编码") or entry.get("实验编码") or "DATA-QUALITY",
            "报告模板": "缺测影响报告",
            "报告类型": "缺测影响报告",
            "数据窗口": entry.get("计算窗口") or entry.get("监控对象", ""),
            "计算窗口": entry.get("计算窗口", ""),
            "指标编码": entry.get("指标编码", ""),
            "生成状态": "待计算",
            "报告数据状态": "未计算",
            "时效记录ID": entry_id,
            "告警凭证": cert,
            "缺测凭证": cert,
            "凭证": cert,
            "credential": cert,
            "异常编号": entry.get("异常编号", f"AN-DF-{entry_id}"),
            "申请原因": entry.get("缺数原因", "计算窗口无有效观测"),
            "办理状态": "已申请，待领取",
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "申请时间": iso(),
        }
        append_timeline(report, "第一次无数据时登记缺测影响报告申请；报告数据尚未计算")
        rows.append(report)
        entry["影响报告申请ID"] = report["id"]
        entry["影响报告申请编号"] = report["报告编码"]
        append_timeline(entry, "已发起缺测影响报告申请", 报告编码=report["报告编码"])
        return {
            "ok": True,
            "created": True,
            "duplicate": False,
            "initiated": True,
            "message": "已登记缺测影响报告申请，报告数据待计算",
            "entry": entry,
            "report": report,
            "report_id": report["id"],
            "report_code": report["报告编码"],
        }

    def _build_conflict(self, entry: dict[str, Any], report: dict[str, Any] | None, delay: int) -> dict[str, Any] | None:
        if report is None:
            return None
        if report.get("办结类型") in {"历史缺测影响调查", "恢复办结"}:
            return {
                "code": "CONFLICT_RESOLVED",
                "resolution": report.get("办结类型"),
                "message": f"冲突已按{report.get('办结类型')}处理",
            }
        return {
            "code": "RECOVERY_REPORT_CLAIM_CONFLICT",
            "message": "迟到作业证明窗口已恢复，但缺测影响报告申请已提交；需领取后选择调查历史缺测影响，或按恢复办结",
            "freshness_claim": {
                "freshness_id": entry["id"],
                "status": entry.get("status"),
                "delay_minutes": delay,
                "claim_token": entry.get("claim_token", ""),
            },
            "report_claim": {
                "report_id": report.get("id"),
                "报告编码": report.get("报告编码"),
                "claim_token": report.get("claim_token", ""),
                "status": report.get("status"),
            },
            "options": ["调查历史缺测及其影响", "按恢复办结"],
        }

    def continue_original(self, entry_id: int, evidence: Any = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(context or {})
        entry_before = self.get_entry(entry_id)
        if entry_before is None:
            return {"ok": False, "http_status": 404, "message": "时效记录不存在"}
        original_anomaly = self._find_anomaly(entry_id, str(entry_before.get("告警凭证") or f"CERT-DF-{entry_id}"))
        original_report = self._find_report(entry_before)
        result = self.recheck(entry_id, evidence=evidence, context=context)
        if not result.get("ok"):
            return result
        entry = result["entry"]
        anomaly = result.get("anomaly") or original_anomaly
        report = result.get("report") or original_report or self._find_report(entry)
        if report is not None:
            report["调查类型"] = "历史缺测及其影响"
            report["办理状态"] = "历史缺测影响调查中"
            report["办结类型"] = "历史缺测影响调查"
            report["办理结论"] = "保留并调查历史缺测及其实际影响，不因迟到观测删除申请"
            report["pending"] = True
            append_timeline(
                report,
                "用同一份作业证据核对补报响应，继续原处置并调查历史缺测影响",
                异常编号=(anomaly or {}).get("异常编号", ""),
            )
            entry["办理方向"] = "调查历史缺测及其影响"
            entry["影响报告申请编号"] = report.get("报告编码", "")
            append_timeline(entry, "续办原复查：历史缺测及影响调查保留")
            result["report"] = report
        result.update({
            "continued": True,
            "original_anomaly_id": (original_anomaly or {}).get("id") or (anomaly or {}).get("id"),
            "original_report_id": (original_report or {}).get("id") or (report or {}).get("id"),
            "same_evidence": True,
        })
        result["message"] = "已用同一份作业证据核对补报响应并续办原复查，原缺测凭证和报告申请保持可追溯"
        return result

    def confirm_recovery(self, entry_id: int, evidence: Any = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(context or {})
        before = self.get_entry(entry_id)
        if before is None:
            return {"ok": False, "http_status": 404, "message": "时效记录不存在"}
        expected_token = context.get("claim_token")
        if before.get("claim_token") and expected_token and expected_token != before.get("claim_token"):
            return {"ok": False, "http_status": 409, "code": "CLAIM_CONFLICT", "message": "缺测记录领取令牌已变更，请刷新后确认恢复", "entry": before}
        result = self.recheck(entry_id, evidence=evidence, context=context)
        if not result.get("ok"):
            return result
        if result.get("missing"):
            result["ok"] = False
            result["message"] = "仍无有效作业证据，不能确认恢复"
            result["http_status"] = 409
            return result
        entry = result["entry"]
        now_text = iso()
        entry["恢复已确认"] = True
        entry["恢复确认时间"] = now_text
        entry["缺测已办结"] = True
        entry["办理结论"] = "按恢复办结"
        # 恢复只关闭历史缺测案件；若实际延迟已超过 SLA，逾期告警仍保持待处置。
        if context.get("resolve_all_alerts") is True:
            entry["pending"] = False
        else:
            entry["pending"] = entry.get("status") != NORMAL
        append_timeline(entry, "确认恢复，原缺测按恢复办结", at=now_text)

        report = self._find_report(entry)
        if report is not None:
            report.update({
                "status": "已办结",
                "生成状态": "恢复办结",
                "办理状态": "按恢复办结",
                "办结类型": "恢复办结",
                "办理结论": f"W{entry.get('计算窗口', '')} 已收到有效作业观测（延迟 {entry.get('延迟分钟', '0')} 分钟），不再计算缺测影响",
                "pending": False,
                "abnormal": False,
                "办结时间": now_text,
            })
            append_timeline(report, "冲突办结：按恢复办结，不再生成缺测影响数据", at=now_text)
            result["report"] = report

        anomaly = result.get("anomaly") or self._find_anomaly(entry_id, str(entry.get("告警凭证") or ""))
        if anomaly is not None and context.get("close_anomaly", False):
            anomaly.update({"status": "已办结", "pending": False, "办结时间": now_text, "核实结论": "凭证核对后办结"})
            append_timeline(anomaly, "凭告警凭证办结", at=now_text)
        result["recovery_confirmed"] = True
        result["conflict"] = self._build_conflict(entry, report, int(entry.get("延迟分钟数") or 0))
        result["冲突"] = result["conflict"]
        result["message"] = "已确认恢复；已提交的影响报告申请按恢复办结并保留追查轨迹，后续检查不再发起该报告"
        return result

    def claim(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "时效记录不存在"}
        owner = str(context.get("owner") or context.get("值班员") or "值班员")
        version = int(entry.get("version") or 1) + 1
        token = f"CLAIM-DF-{entry_id}-v{version}"
        expected = context.get("claim_token") or context.get("版本令牌")
        if entry.get("claim_token") and expected and expected != entry.get("claim_token"):
            return {"ok": False, "http_status": 409, "code": "CLAIM_CONFLICT", "message": "领取令牌已过期，请刷新后重试", "entry": entry}
        entry.update({"version": version, "claim_token": token, "领取人": owner, "领取时间": iso()})
        metric, window, job_id = self._identity(entry)
        _, _, job, _ = self._select_observation(entry, evidence=None, metric=metric, window=window, job_id=job_id)
        anomaly = self._find_anomaly(entry_id, str(entry.get("告警凭证") or f"CERT-DF-{entry_id}"))
        append_timeline(entry, "值班员领取缺测记录", 领取人=owner)
        return {
            "ok": True,
            "message": "已领取与作业关联的缺测记录",
            "entry": entry,
            "missing_record": entry,
            "anomaly": anomaly,
            "job": job,
            "claim_token": token,
        }

    # 常见命名别名：测试或其他领域服务可能使用“检查/观测/影响报告”词汇。
    def check_freshness(self, entry_id: int, check_time: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.recheck(entry_id, check_time=check_time, context=kwargs)

    def check_window(self, entry_id: int, check_time: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.recheck(entry_id, check_time=check_time, context=kwargs)

    def recheck_freshness(self, entry_id: int, check_time: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.recheck(entry_id, check_time=check_time, context=kwargs)

    def submit_observation(self, entry_id: int, observation: Any = None, **kwargs: Any) -> dict[str, Any]:
        evidence = observation if observation is not None else kwargs
        return self.recheck(entry_id, evidence=evidence, context=kwargs)

    def request_impact_report(self, entry_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.apply_impact_report(entry_id, context=kwargs)

    def create_missing_impact_report(self, entry_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.apply_impact_report(entry_id, context=kwargs)

    def claim_missing_record(self, entry_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.claim(entry_id, context=kwargs)

    def continue_review(self, entry_id: int, evidence: Any = None, **kwargs: Any) -> dict[str, Any]:
        return self.continue_original(entry_id, evidence=evidence, context=kwargs)

    verify_late_report = continue_review

    def create_missing_report(self, entry_id: int, **kwargs: Any) -> dict[str, Any]:
        return self.apply_impact_report(entry_id, context=kwargs)

    register_impact_report = create_missing_report

    # ---------- 动作分派 ----------

    def run_action(  # type: ignore[override]
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """兼容旧契约：直接调用服务时仍返回 (entry, message)。"""
        result = self.dispatch_action(entry_id, action, context or {})
        return result.get("entry"), str(result.get("message", ""))

    def dispatch_action(self, entry_id: int, action: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = dict(context or {})
        evidence = first_value(ctx, "evidence", "作业证据", "job", "作业", "补报响应")
        if evidence is None and any(
            key in ctx for key in ("完成时间", "实际完成时间", "作业完成时间", "实际观测时间", "observed_at", "actual_observed_at", "作业批次", "批次", "job_id", "作业ID", "窗口水位", "completed_at")
        ):
            evidence = ctx
        if action in RECHECK_ACTIONS:
            result = self.recheck(entry_id, evidence=evidence, context=ctx)
        elif action in CONTINUE_ACTIONS:
            result = self.continue_original(entry_id, evidence=evidence, context=ctx)
        elif action in REPORT_ACTIONS:
            result = self.apply_impact_report(entry_id, context=ctx)
        elif action in RECOVERY_ACTIONS:
            result = self.confirm_recovery(entry_id, evidence=evidence, context=ctx)
        elif action in CLAIM_ACTIONS:
            result = self.claim(entry_id, context=ctx)
        elif action == "忽略告警":
            reason = ctx.get("reason", None)
            if reason is None and "remark" in ctx:
                reason = ctx.get("remark")
            result = self.ignore_alert(entry_id, "" if reason is None else str(reason))
        else:
            base_result = super().dispatch_action(entry_id, action, ctx)
            result = base_result
        if isinstance(result, dict) and result.get("entry") is None:
            result["entry"] = self.get_entry(entry_id)
        return result

    # ---------- 旧接口/导出/健康度 ----------

    def ignore_alert(self, entry_id: int, reason: str | None = None) -> dict[str, Any]:
        """忽略时效告警并把处置记录同步给指标异常检测（凭证保持幂等）。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "监控对象不存在"}
        if entry.get("status") == NORMAL:
            return {"ok": False, "message": "正常对象无需忽略告警"}
        if reason is not None and not reason.strip():
            return {"ok": False, "message": "忽略告警必须填写原因"}
        reason = (reason or "值班员忽略告警").strip()
        entry["status"] = OVERDUE
        entry["时效等级"] = OVERDUE
        entry["pending"] = False
        entry["abnormal"] = True
        entry["忽略原因"] = reason
        anomaly = self._ensure_anomaly(entry, f"告警被忽略：{reason}")
        anomaly["观测值"] = f"延迟 {entry.get('延迟分钟', '未知')} 分钟被忽略"
        append_timeline(entry, "忽略告警并同步异常凭证", 原因=reason)
        return {"ok": True, "message": "告警已忽略并登记异常事件待确认", "entry": entry, "anomaly": anomaly}

    def batch_recalc_domain(self, domain: str) -> dict[str, Any]:
        rows = [r for r in store.rows(self.MODULE) if r.get("数据域") == domain]
        if not rows:
            return {"ok": False, "message": f"数据域 {domain} 无监控对象"}
        done = sum(1 for r in rows if self.recheck(int(r.get("id", 0))).get("ok"))
        return {"ok": True, "message": f"数据域 {domain} 复查完成 {done}/{len(rows)}"}

    def consume_from_metric_compute(self, metric_code: str) -> list[dict[str, Any]]:
        return [
            {"作业名称": r.get("作业名称", ""), "窗口水位": r.get("窗口水位", ""), "作业状态": r.get("status", "")}
            for r in store.rows("metric_compute")
            if r.get("指标编码") == metric_code
        ]

    def produce_to_anomaly_metric(self, domain: str) -> dict[str, Any]:
        expired = [
            r for r in store.rows(self.MODULE)
            if r.get("数据域") == domain and r.get("status") in (OVERDUE, "过期")
        ]
        if not expired:
            return {"ok": False, "message": f"数据域 {domain} 无逾期对象"}
        anomaly_rows = store.rows("anomaly_metric")
        anomaly = {
            "id": next_id(anomaly_rows),
            "异常编号": f"AN-DF-B-{domain}-{int(utc_now().timestamp())}",
            "指标编码": domain,
            "基线区间": domain,
            "观测值": f"逾期对象 {len(expired)} 个",
            "异常评分": "0.9",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        }
        anomaly_rows.append(anomaly)
        return {"ok": True, "message": f"已打包登记 {len(expired)} 个逾期对象", "anomaly": anomaly}

    def check_domain_health(self) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        domains: dict[str, dict[str, int]] = {}
        for raw_row in rows:
            row = self._normalize_row(raw_row)
            domain = str(row.get("数据域", "未知"))
            bucket = domains.setdefault(domain, {MISSING: 0, OVERDUE: 0, NORMAL: 0})
            status = str(row.get("status", NORMAL))
            if status in bucket:
                bucket[status] += 1
        return {"ok": True, "数据域健康度": domains}

    def export_delay_report(self) -> list[dict[str, Any]]:
        result = []
        for raw in store.rows(self.MODULE):
            row = self._normalize_row(raw)
            if row.get("status") in (MISSING, OVERDUE):
                result.append({
                    "监控对象": row.get("监控对象", ""),
                    "数据域": row.get("数据域", ""),
                    "计算窗口": row.get("计算窗口", ""),
                    "延迟分钟": row.get("延迟分钟", "0"),
                    "时效等级": row.get("时效等级", ""),
                    "缺数原因": row.get("缺数原因", ""),
                    "告警凭证": row.get("告警凭证", ""),
                })
        return result
