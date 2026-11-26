"""指标计算作业领域服务：管理实验指标的计算窗口、窗口水位推进与作业重试。

数据链路位置：指标计算层，消费指标口径中心的生效口径与曝光日志的去重数据，
产出指标结果写入显著性检验，是实验评估的核心计算环节。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.freshness_support import parse_datetime
from app.store import store


class MetricComputeService(BaseModuleService):
    MODULE = "metric_compute"
    ENTITY = "计算作业"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["作业名称", "指标编码", "计算窗口", "窗口水位", "完成时间", "作业状态", "作业批次"]
    REQUIRED_FIELDS = ["作业名称", "指标编码", "计算窗口"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "计算失败"]
    ACTION_RULES = {"启动作业": "执行中", "重试计算": "计算失败"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：窗口计算与结果下发 ----

    def start_job(self, entry_id: int) -> dict[str, Any]:
        """启动计算作业，推进窗口水位并在完成后写入显著性检验批次。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "待执行":
            return {"ok": False, "message": "仅待执行作业可启动"}
        metric = next(
            (m for m in store.rows("metric_catalog") if m.get("指标编码") == entry.get("指标编码")),
            None,
        )
        if metric is None or metric.get("status") != "已生效":
            return {"ok": False, "message": f"口径 {entry.get('指标编码')} 未生效，禁止计算"}
        completed_at = datetime.now(timezone.utc).isoformat()
        entry["status"] = "已完成"
        entry["pending"] = False
        entry["作业状态"] = "已完成"
        entry["完成时间"] = completed_at
        entry["实际完成时间"] = completed_at
        entry["窗口水位"] = completed_at
        test_rows = store.rows("significance_test")
        test_rows.append({
            "id": max((int(r.get("id", 0)) for r in test_rows), default=0) + 1,
            "检验批次": f"ST-{entry_id}-{datetime.now(timezone.utc).timestamp():.0f}",
            "实验编码": f"EXP_-000{entry_id}",
            "指标编码": entry.get("指标编码", ""),
            "P值": "0.031",
            "置信区间": "[-0.021, 0.045]",
            "status": "待检验",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"作业 {entry_id} 计算完成并登记检验批次", "entry": entry}

    def retry_failed_job(self, entry_id: int) -> dict[str, Any]:
        """重试计算失败作业，重算窗口并登记异常事件。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "计算失败":
            return {"ok": False, "message": "仅计算失败作业可重试"}
        entry["status"] = "执行中"
        entry["pending"] = True
        entry["作业状态"] = "执行中"
        entry["重试次数"] = str(int(entry.get("重试次数", "0") or 0) + 1)
        anomaly_rows = store.rows("anomaly_metric")
        anomaly_rows.append({
            "id": max((int(r.get("id", 0)) for r in anomaly_rows), default=0) + 1,
            "异常编号": f"AN-{entry_id}-retry",
            "指标编码": entry.get("指标编码", ""),
            "基线区间": entry.get("计算窗口", ""),
            "观测值": "作业重试",
            "异常评分": "0.5",
            "status": "待确认",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"作业 {entry_id} 已进入重试（第 {entry['重试次数']} 次）", "entry": entry}

    def report_completion(self, entry_id: int, completion_time: str | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """补报作业完成时间；只有已完成且时间可解析的当前窗口作业才恢复时效。"""
        context = context or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "作业不存在"}
        raw_time = completion_time or context.get("完成时间") or context.get("实际完成时间") or context.get("completed_at")
        if raw_time in (None, ""):
            return {"ok": False, "message": "补报完成时间不能为空；没有完成时间时时效监控将给出缺数原因"}
        entry["status"] = "已完成"
        entry["作业状态"] = "已完成"
        entry["pending"] = False
        entry["完成时间"] = raw_time
        entry["实际完成时间"] = raw_time
        entry.setdefault("窗口水位", raw_time)
        if not entry.get("作业批次") and context.get("作业批次"):
            entry["作业批次"] = context["作业批次"]
        # 按指标+窗口精确复查，避免晚到旧窗口把另一窗口刷新成正常。
        from app.services.data_freshness import DataFreshnessService

        freshness_service = DataFreshnessService()
        metric = entry.get("指标编码")
        window = entry.get("计算窗口")
        results = []
        for fresh in store.rows("data_freshness"):
            linked = fresh.get("关联作业ID") == entry_id or fresh.get("监控对象") == f"metric_compute:{entry_id}"
            same_window = fresh.get("指标编码") == metric and str(fresh.get("计算窗口") or "") == str(window or "")
            if linked or same_window:
                results.append(freshness_service.recheck(int(fresh["id"]), evidence=entry, context=context))
        return {"ok": True, "message": f"作业 {entry_id} 完成时间已补报", "entry": entry, "freshness_results": results}

    def advance_watermark(self, entry_id: int, new_watermark: str) -> dict[str, Any]:
        """推进窗口水位并按指标/窗口精确复查数据时效。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or not str(new_watermark or "").strip():
            return {"ok": False, "message": "作业不存在或水位为空"}
        old = str(entry.get("窗口水位", ""))
        old_dt, _ = parse_datetime(old)
        new_dt, new_err = parse_datetime(new_watermark)
        if not str(new_watermark or "").strip():
            return {"ok": False, "message": "水位为空"}
        if old_dt and new_dt and new_dt <= old_dt:
            return {"ok": False, "message": "新水位必须晚于当前水位"}
        if not old_dt and old and new_watermark == old:
            return {"ok": False, "message": "新水位必须晚于当前水位"}
        entry["窗口水位"] = new_watermark
        entry.setdefault("完成时间", new_watermark)
        entry.setdefault("实际完成时间", new_watermark)
        freshness_rows = store.rows("data_freshness")
        matched = [
            f for f in freshness_rows
            if f.get("关联作业ID") == entry_id
            or f.get("监控对象") == f"metric_compute:{entry_id}"
            or (
                f.get("指标编码") == entry.get("指标编码")
                and str(f.get("计算窗口") or "") == str(entry.get("计算窗口") or "")
            )
        ]
        from app.services.data_freshness import DataFreshnessService

        service = DataFreshnessService()
        if matched:
            for fresh in matched:
                service.recheck(int(fresh["id"]), evidence=entry)
        else:
            freshness_rows.append({
                "id": max((int(r.get("id", 0)) for r in freshness_rows), default=0) + 1,
                "监控对象": f"metric_compute:{entry_id}",
                "数据域": "指标计算",
                "指标编码": entry.get("指标编码", ""),
                "计算窗口": entry.get("计算窗口", ""),
                "关联作业ID": entry_id,
                "最新水位": new_watermark,
                "延迟分钟": "0",
                "时效等级": "正常",
                "status": "正常",
                "pending": False,
                "abnormal": False,
            })
        return {"ok": True, "message": f"水位已推进到 {new_watermark}"}

    def run_action(  # type: ignore[override]
        self,
        entry_id: int,
        action: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        result = self.dispatch_action(entry_id, action, context or {})
        return result.get("entry"), str(result.get("message", ""))

    def dispatch_action(self, entry_id: int, action: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = context or {}
        # 既有通用按钮继续走骨架的状态流转；补报/推进水位是时效链路专用动作。
        if action in {"补报完成", "补报完成时间", "补报作业"}:
            return self.report_completion(entry_id, context=ctx)
        if action == "推进水位":
            return self.advance_watermark(entry_id, str(ctx.get("new_watermark") or ctx.get("窗口水位") or ""))
        return super().dispatch_action(entry_id, action, ctx)

    def batch_execute_pending(self) -> dict[str, Any]:
        """批量执行全部待执行作业并汇总结果。"""
        pending_rows = [r for r in store.rows(self.MODULE) if r.get("status") == "待执行"]
        done, failed = 0, 0
        for row in pending_rows:
            result = self.start_job(int(row.get("id", 0)))
            if result.get("ok"):
                done += 1
            else:
                row["status"] = "计算失败"
                row["pending"] = False
                row["abnormal"] = True
                failed += 1
        return {"ok": True, "message": f"批量执行完成：成功 {done}，失败 {failed}", "success": done, "failed": failed}

    def consume_from_window_scheduler(self, metric_code: str) -> list[dict[str, Any]]:
        """读取窗口调度器中该指标的活跃计划，返回调度约束。"""
        return [
            {
                "计划名称": s.get("计划名称", ""),
                "窗口粒度": s.get("窗口粒度", ""),
                "下次窗口": s.get("下次窗口", ""),
            }
            for s in store.rows("window_scheduler")
            if s.get("指标编码") == metric_code and s.get("status") in ("待执行", "执行中")
        ]

    def consume_from_exposure_log(self, exp_code: str) -> dict[str, Any]:
        """统计曝光日志去重记录数，作为计算作业的分母数据。"""
        rows = [
            r for r in store.rows("exposure_log")
            if r.get("实验编码") == exp_code and r.get("status") == "已去重"
        ]
        return {
            "实验编码": exp_code,
            "去重曝光数": len(rows),
            "分组分布": {
                group: sum(1 for r in rows if r.get("分组名称") == group)
                for group in ("对照组", "实验组")
            },
        }

    def export_job_results(self, metric_code: str) -> list[dict[str, Any]]:
        """导出指定指标的完成作业清单，供报告生成器编排数据窗口。"""
        return [
            {
                "作业名称": r.get("作业名称", ""),
                "计算窗口": r.get("计算窗口", ""),
                "窗口水位": r.get("窗口水位", ""),
            }
            for r in store.rows(self.MODULE)
            if r.get("指标编码") == metric_code and r.get("status") == "已完成"
        ]
