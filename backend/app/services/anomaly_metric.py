"""指标异常检测领域服务：基线对比、异常评分、缺测凭证处置与人工核实。

数据链路位置：质量检测层，消费指标计算结果与时效监控告警。缺测异常必须携带
可追查的时效记录、作业批次和观测时间；办结、误报或恢复都不能删除历史证据。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.freshness_support import append_timeline, next_id
from app.store import store


class AnomalyMetricService(BaseModuleService):
    MODULE = "anomaly_metric"
    ENTITY = "异常事件"
    OPERATION_TERM = "校验"
    LIST_FIELDS = [
        "异常编号", "指标编码", "基线区间", "观测值", "异常评分",
        "异常类型", "告警凭证", "计算窗口", "作业批次", "实际观测时间",
        "时效记录ID", "核实结论",
    ]
    REQUIRED_FIELDS = ["异常编号", "指标编码", "基线区间"]
    STATUS_ORDER = ["待确认", "保留核实", "已确认", "已办结", "误报"]
    ACTION_RULES = {
        "确认异常": "已确认",
        "标记误报": "误报",
        "办结": "已办结",
        "办结核实": "已办结",
        "保留核实": "保留核实",
    }
    NEGATIVE_ACTIONS = []

    def _credential_matches(self, entry: dict[str, Any], credential: Any) -> bool:
        if credential in (None, ""):
            return True
        expected = {
            str(entry.get("告警凭证") or ""),
            str(entry.get("缺测凭证") or ""),
            str(entry.get("异常编号") or ""),
        }
        return str(credential) in expected

    def _linked_freshness_id(self, entry: dict[str, Any]) -> int | None:
        raw = (
            entry.get("时效记录ID")
            or entry.get("freshness_id")
            or entry.get("freshness_record_id")
            or entry.get("监控记录ID")
            or entry.get("data_freshness_id")
        )
        try:
            if raw not in (None, ""):
                return int(raw)
        except (TypeError, ValueError):
            pass
        source = str(entry.get("异常编号", ""))
        if source.startswith("AN-DF-"):
            tail = source[len("AN-DF-"):].split("-")[0]
            if tail.isdigit():
                return int(tail)
        cert = str(entry.get("告警凭证") or entry.get("缺测凭证") or "")
        if cert.startswith("CERT-DF-"):
            tail = cert.rsplit("-", 1)[-1]
            if tail.isdigit():
                return int(tail)
        return None

    # ---- 业务扩展：异常检测与人工处置 ----

    def confirm_anomaly(self, entry_id: int) -> dict[str, Any]:
        """确认异常并把处置单推送到结论评审队列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") not in ("待确认", "保留核实"):
            return {"ok": False, "message": "仅待确认异常可确认"}
        entry["status"] = "已确认"
        entry["pending"] = False
        entry["确认时间"] = datetime.now(timezone.utc).isoformat()
        entry["核实结论"] = "已确认并推送评审"
        append_timeline(entry, "确认异常并进入结论评审")
        review_rows = store.rows("conclusion_review")
        review_rows.append({
            "id": next_id(review_rows),
            "记录编号": f"RV-AN-{entry_id}",
            "实验编码": str(entry.get("指标编码", "")).replace("M-", "EXP_-"),
            "评审结论": f"指标异常 {entry.get('异常编号', '')} 待评审处置",
            "评审角色": "数据质量",
            "评审时间": datetime.now(timezone.utc).isoformat(),
            "status": "待评审",
            "pending": True,
            "abnormal": False,
            "告警凭证": entry.get("告警凭证", ""),
        })
        return {"ok": True, "message": f"异常 {entry.get('异常编号')} 已确认并进入评审", "entry": entry}

    def mark_false_positive(self, entry_id: int, reason: str) -> dict[str, Any]:
        """标记误报并保留来源时效证据，不再把历史缺测直接涂改成新鲜。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") not in ("待确认", "保留核实"):
            return {"ok": False, "message": "仅待确认异常可标记误报"}
        if not reason.strip():
            return {"ok": False, "message": "标记误报必须填写原因"}
        entry["status"] = "误报"
        entry["pending"] = False
        entry["abnormal"] = True
        entry["误报原因"] = reason
        entry["核实结论"] = "误报"
        append_timeline(entry, "标记误报；原始缺测证据保留可审阅", 原因=reason)

        fresh_id = self._linked_freshness_id(entry)
        if fresh_id is not None:
            fresh = store.find("data_freshness", fresh_id)
            if fresh is not None:
                # 只记录修正结论；status/延迟/缺数原因/轨迹均保留，避免毁灭证据。
                fresh["误报修正"] = reason
                fresh["关联异常结论"] = "误报"
                append_timeline(fresh, "关联异常被标记误报，但历史缺测证据保留", 原因=reason)
        return {"ok": True, "message": f"已标记误报（{reason}），原始凭证仍可追查", "entry": entry}

    def resolve_with_credential(
        self,
        entry_id: int,
        credential: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "异常事件不存在"}
        if not self._credential_matches(entry, credential):
            return {"ok": False, "http_status": 409, "code": "CREDENTIAL_MISMATCH", "message": "告警凭证与该异常不匹配，不能办结"}
        if entry.get("status") == "已办结":
            return {"ok": True, "duplicate": True, "message": "该次核实已办结", "entry": entry}
        now = datetime.now(timezone.utc).isoformat()
        entry.update({
            "status": "已办结",
            "pending": False,
            "办结时间": now,
            "核实结论": "凭证核对后办结",
            "办结说明": note or "值班员凭告警凭证办结",
        })
        append_timeline(entry, "凭告警凭证办结该次核实", 凭证=credential or entry.get("告警凭证", ""))
        fresh_id = self._linked_freshness_id(entry)
        missing_record = store.find("data_freshness", fresh_id) if fresh_id is not None else None
        if missing_record is not None:
            missing_record["异常办结时间"] = now
            append_timeline(missing_record, "关联缺测异常已凭凭证办结")
        return {"ok": True, "message": "已核对告警凭证并办结该次核实，证据保留可审阅", "entry": entry, "missing_record": missing_record}

    def retain_verification(
        self,
        entry_id: int,
        credential: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "异常事件不存在"}
        if not self._credential_matches(entry, credential):
            return {"ok": False, "http_status": 409, "code": "CREDENTIAL_MISMATCH", "message": "告警凭证与该异常不匹配，不能保留核实"}
        now = datetime.now(timezone.utc).isoformat()
        entry.update({
            "status": "保留核实",
            "pending": True,
            "核实保留": True,
            "保留时间": now,
            "核实结论": "保留该次核实",
            "保留说明": note or "值班员保留该次核实和全部证据",
        })
        append_timeline(entry, "保留该次核实，缺测证据继续可审阅")
        return {"ok": True, "message": "已保留该次核实，原始缺测证据和作业批次可继续追查", "entry": entry}

    def claim_linked_missing(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(context or {})
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "异常事件不存在"}
        fresh_id = self._linked_freshness_id(entry)
        if fresh_id is None:
            return {"ok": False, "message": "该异常没有关联的作业缺测记录"}
        # 延迟导入避免模块初始化期的循环依赖。
        from app.services.data_freshness import DataFreshnessService

        claim_result = DataFreshnessService().claim(fresh_id, context)
        if not claim_result.get("ok"):
            return claim_result
        owner = str(context.get("owner") or context.get("值班员") or "值班员")
        entry["领取人"] = owner
        entry["领取时间"] = datetime.now(timezone.utc).isoformat()
        entry["领取的缺测记录ID"] = fresh_id
        entry["告警凭证"] = claim_result["entry"].get("告警凭证", entry.get("告警凭证", ""))
        append_timeline(entry, "从异常处置入口领取关联作业缺测记录", 时效记录ID=fresh_id)
        job = claim_result.get("job") or {}
        batch = job.get("作业批次") or job.get("批次") or entry.get("作业批次") or "待补报"
        metric = claim_result["entry"].get("指标编码") or entry.get("指标编码") or ""
        window = claim_result["entry"].get("计算窗口") or entry.get("计算窗口") or ""
        claim_result["anomaly"] = entry
        claim_result["freshness"] = claim_result["entry"]
        claim_result["data_freshness"] = claim_result["entry"]
        claim_result["message"] = f"已领取 {metric}/{window} 关联作业批次 {batch} 的缺测记录，可继续核对完成时间"
        return claim_result

    def resolve_anomaly(self, entry_id: int, credential: str | None = None, note: str | None = None) -> dict[str, Any]:
        return self.resolve_with_credential(entry_id, credential, note)

    def close_anomaly(self, entry_id: int, credential: str | None = None, note: str | None = None) -> dict[str, Any]:
        return self.resolve_with_credential(entry_id, credential, note)

    def claim_missing_record(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.claim_linked_missing(entry_id, context)

    def keep_verification(self, entry_id: int, credential: str | None = None, note: str | None = None) -> dict[str, Any]:
        return self.retain_verification(entry_id, credential, note)

    def handle_claim(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.claim_linked_missing(entry_id, context)

    def run_action(  # type: ignore[override]
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        result = self.dispatch_action(entry_id, action, context or {})
        return result.get("entry"), str(result.get("message", ""))

    def dispatch_action(self, entry_id: int, action: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = dict(context or {})
        credential = ctx.get("告警凭证") or ctx.get("credential") or ctx.get("凭证")
        note = ctx.get("note") or ctx.get("备注") or ctx.get("remark")
        if action in {"办结", "办结核实", "凭证办结", "办结该次核实", "resolve", "close", "close_anomaly"}:
            return self.resolve_with_credential(entry_id, credential, note)
        if action in {"保留核实", "保留该次核实", "保留", "retain", "keep_verification"}:
            return self.retain_verification(entry_id, credential, note)
        if action in {"领取缺测记录", "领取", "领取处置", "领取关联缺测", "claim", "claim_missing"}:
            return self.claim_linked_missing(entry_id, ctx)
        if action == "确认异常":
            return self.confirm_anomaly(entry_id)
        if action == "标记误报":
            if "reason" in ctx and not str(ctx.get("reason") or "").strip():
                return {"ok": False, "message": "标记误报必须填写原因"}
            reason = str(ctx.get("reason") or ctx.get("remark") or "值班员标记误报")
            return self.mark_false_positive(entry_id, reason)
        return super().dispatch_action(entry_id, action, ctx)

    def detect_deviation(self, metric_code: str, observed: float, baseline: float) -> dict[str, Any]:
        """对比观测值与基线，偏差超阈值时登记新异常事件。"""
        if not metric_code:
            return {"ok": False, "message": "指标编码为空"}
        if baseline == 0:
            deviation = abs(observed)
        else:
            deviation = abs(observed - baseline) / abs(baseline)
        if deviation < 0.2:
            return {"ok": True, "message": f"偏差 {deviation:.2%} 未超阈值", "anomaly": False}
        rows = store.rows(self.MODULE)
        anomaly = {
            "id": next_id(rows),
            "异常编号": f"AN-DV-{metric_code}-{datetime.now(timezone.utc).timestamp():.0f}",
            "指标编码": metric_code,
            "基线区间": f"[{baseline}, {baseline}]",
            "观测值": str(observed),
            "异常评分": str(round(min(deviation, 1.0), 2)),
            "异常类型": "指标偏差",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        }
        rows.append(anomaly)
        return {"ok": True, "message": f"偏差 {deviation:.2%} 超阈值，异常已登记", "anomaly": True, "entry": anomaly}

    def batch_confirm(self) -> dict[str, Any]:
        """批量确认全部高分待确认异常。"""
        pending = [
            r for r in store.rows(self.MODULE)
            if r.get("status") in ("待确认", "保留核实")
            and float(str(r.get("异常评分", "0")) or 0) >= 0.7
        ]
        done = sum(1 for r in pending if self.confirm_anomaly(int(r.get("id", 0))).get("ok"))
        return {"ok": True, "message": f"高分异常批量确认 {done} 条"}

    def consume_from_metric_compute(self, metric_code: str) -> list[float]:
        """读取指标计算层最近作业产出，构造观测值序列。"""
        values = []
        for r in store.rows("metric_compute"):
            if r.get("指标编码") == metric_code and r.get("status") == "已完成":
                value = str(r.get("作业名称", "")).split("-")[-1]
                if value.replace(".", "").isdigit():
                    values.append(float(value))
        return values

    def consume_from_metric_catalog(self, metric_code: str) -> dict[str, Any]:
        """读取指标口径中心的口径版本，锁定异常检测的口径上下文。"""
        metric = next((m for m in store.rows("metric_catalog") if m.get("指标编码") == metric_code), None)
        if metric is None:
            return {"ok": False, "message": "口径不存在"}
        return {"ok": True, "口径版本": metric.get("口径版本", ""), "聚合窗口": metric.get("聚合窗口", "")}

    def export_confirmed(self, metric_code: str) -> list[dict[str, Any]]:
        """导出已确认异常清单，供报告生成器写入风险附注。"""
        return [
            {
                "异常编号": r.get("异常编号", ""),
                "观测值": r.get("观测值", ""),
                "异常评分": r.get("异常评分", ""),
                "确认时间": r.get("确认时间", ""),
                "告警凭证": r.get("告警凭证", ""),
            }
            for r in store.rows(self.MODULE)
            if r.get("指标编码") == metric_code and r.get("status") in ("已确认", "已办结")
        ]
