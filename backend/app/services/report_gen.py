"""报告生成器领域服务：管理实验分析报告的模板编排、数据窗口聚合与生成重试。

数据链路位置：报告生产层，消费归因分析与显著性检验的结论数据，按模板聚合成
分析报告，并把成品交给报告分发推送给业务接收方。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ReportGenService(BaseModuleService):
    MODULE = "report_gen"
    ENTITY = "分析报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编码", "实验编码", "报告模板", "数据窗口", "生成状态"]
    REQUIRED_FIELDS = ["报告编码", "实验编码", "报告模板"]
    STATUS_ORDER = ["排队中", "生成中", "已完成", "生成失败", "已办结"]
    ACTION_RULES = {"开始生成": "生成中", "重试生成": "生成失败"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：报告编排与成品分发 ----

    def generate_report(self, entry_id: int) -> dict[str, Any]:
        """聚合检验与归因结论生成报告，完成后登记分发批次。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "排队中":
            return {"ok": False, "message": "仅排队中报告可生成"}
        exp_code = str(entry.get("实验编码", ""))
        significant = [
            r for r in store.rows("significance_test")
            if r.get("实验编码") == exp_code and r.get("status") == "已检验"
        ]
        attributed = [
            r for r in store.rows("attr_analysis")
            if r.get("实验编码") == exp_code and r.get("status") == "已归因"
        ]
        if not significant:
            entry["status"] = "生成失败"
            entry["生成状态"] = "生成失败"
            entry["abnormal"] = True
            entry["失败原因"] = "无已检验结论"
            return {"ok": False, "message": "实验无已检验结论，报告生成失败"}
        entry["status"] = "已完成"
        entry["生成状态"] = "已完成"
        entry["pending"] = False
        entry["结论条数"] = str(len(significant) + len(attributed))
        dist_rows = store.rows("report_dist")
        dist_rows.append({
            "id": max((int(r.get("id", 0)) for r in dist_rows), default=0) + 1,
            "分发批次": f"RD-{entry_id}-{datetime.now(timezone.utc).timestamp():.0f}",
            "报告编码": entry.get("报告编码", ""),
            "接收方": "实验负责人",
            "分发渠道": "站内信",
            "送达状态": "排队中",
            "status": "排队中",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": f"报告已生成（结论 {entry['结论条数']} 条）并排队分发"}

    def retry_failed(self, entry_id: int) -> dict[str, Any]:
        """重试生成失败报告，重新校验数据依赖后回到排队。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "生成失败":
            return {"ok": False, "message": "仅生成失败报告可重试"}
        exp_code = str(entry.get("实验编码", ""))
        has_result = any(
            r.get("实验编码") == exp_code and r.get("status") == "已检验"
            for r in store.rows("significance_test")
        )
        if not has_result:
            return {"ok": False, "message": "依赖的检验结论仍未就绪，暂不能重试"}
        entry["status"] = "排队中"
        entry["生成状态"] = "排队中"
        entry["pending"] = True
        entry["abnormal"] = False
        entry["重试次数"] = str(int(entry.get("重试次数", "0") or 0) + 1)
        return {"ok": True, "message": f"报告已回到排队（第 {entry['重试次数']} 次重试）"}

    def batch_generate(self) -> dict[str, Any]:
        """批量生成全部排队中报告。"""
        queued = [r for r in store.rows(self.MODULE) if r.get("status") == "排队中"]
        done, failed = 0, 0
        for row in queued:
            if self.generate_report(int(row.get("id", 0))).get("ok"):
                done += 1
            else:
                failed += 1
        return {"ok": True, "message": f"批量生成完成：成功 {done}，失败 {failed}"}

    def consume_from_attr_analysis(self, exp_code: str) -> list[dict[str, Any]]:
        """读取归因分析层的因子权重，编排报告归因章节。"""
        return [
            {"贡献因子": r.get("贡献因子", ""), "贡献权重": r.get("贡献权重", "")}
            for r in store.rows("attr_analysis")
            if r.get("实验编码") == exp_code and r.get("status") == "已归因"
        ]

    def consume_from_exp_design(self, exp_code: str) -> dict[str, Any]:
        """读取实验设计层方案信息，写入报告背景章节。"""
        exp = next((r for r in store.rows("exp_design") if r.get("实验编码") == exp_code), None)
        if exp is None:
            return {"ok": False, "message": f"实验方案 {exp_code} 不存在"}
        return {
            "ok": True,
            "实验名称": exp.get("实验名称", ""),
            "分组策略": exp.get("分组策略", ""),
            "状态": exp.get("status", ""),
        }

    def compose_summary(self, entry_id: int) -> dict[str, Any]:
        """合成报告摘要文本，包含显著结论与归因因子数量。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "报告不存在"}
        exp_code = str(entry.get("实验编码", ""))
        sig_count = sum(
            1 for r in store.rows("significance_test")
            if r.get("实验编码") == exp_code and r.get("status") == "已检验"
        )
        attr_count = sum(
            1 for r in store.rows("attr_analysis")
            if r.get("实验编码") == exp_code and r.get("status") == "已归因"
        )
        return {
            "ok": True,
            "摘要": f"实验 {exp_code} 共 {sig_count} 条显著结论、{attr_count} 个归因因子",
            "数据窗口": entry.get("数据窗口", ""),
        }

    def archive_finished(self, entry_id: int) -> dict[str, Any]:
        """把完成报告的结论写入实验归档库。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已完成":
            return {"ok": False, "message": "仅已完成报告可归档"}
        archive_rows = store.rows("exp_archive")
        archive_rows.append({
            "id": max((int(r.get("id", 0)) for r in archive_rows), default=0) + 1,
            "实验编码": entry.get("实验编码", ""),
            "结束时间": datetime.now(timezone.utc).isoformat(),
            "结论摘要": f"报告 {entry.get('报告编码', '')} 结论 {entry.get('结论条数', '0')} 条",
            "归档位置": f"report/{entry.get('报告编码', '')}",
            "保留等级": "标准",
            "status": "待归档",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "报告结论已写入归档库"}

    def investigate_historical_missing(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "报告申请不存在"}
        expected_token = context.get("claim_token")
        if entry.get("claim_token") and expected_token and expected_token != entry.get("claim_token"):
            return {"ok": False, "http_status": 409, "code": "CLAIM_CONFLICT", "message": "报告申请领取令牌已变更，请刷新后选择处置方向", "entry": entry}
        now = datetime.now(timezone.utc).isoformat()
        entry.update({
            "status": "生成中",
            "生成状态": "历史缺测影响调查中",
            "办理状态": "调查历史缺测及其影响",
            "办结类型": "历史缺测影响调查",
            "办理结论": "本申请用于调查 W 窗口历史缺测及其实际影响",
            "pending": True,
        })
        timeline = entry.setdefault("办理轨迹", [])
        if isinstance(timeline, list):
            timeline.append({"时间": now, "事件": "明确按历史缺测及其影响继续调查"})
        entry["最近办理"] = "调查历史缺测及其影响"
        return {"ok": True, "message": "报告申请已明确为历史缺测及其影响调查", "entry": entry}

    def close_as_recovery(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "报告申请不存在"}
        expected_token = context.get("claim_token")
        if entry.get("claim_token") and expected_token and expected_token != entry.get("claim_token"):
            return {"ok": False, "http_status": 409, "code": "CLAIM_CONFLICT", "message": "报告申请领取令牌已变更，请刷新后再按恢复办结", "entry": entry}
        fresh_id = entry.get("时效记录ID")
        if fresh_id not in (None, ""):
            from app.services.data_freshness import DataFreshnessService

            result = DataFreshnessService().confirm_recovery(int(fresh_id), context=context)
            if not result.get("ok"):
                return result
            return {"ok": True, "message": "已通过时效记录按恢复办结", "entry": result.get("report") or entry, "freshness": result.get("entry")}
        now = datetime.now(timezone.utc).isoformat()
        entry.update({"status": "已办结", "生成状态": "恢复办结", "办理状态": "按恢复办结", "办结类型": "恢复办结", "pending": False, "办结时间": now})
        return {"ok": True, "message": "报告申请已按恢复办结", "entry": entry}

    def generate_missing_impact(self, entry_id: int) -> dict[str, Any]:
        """把待确认缺测凭证计算成可审阅的实际缺测影响报告数据。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "报告申请不存在"}
        if entry.get("办结类型") == "恢复办结":
            return {"ok": False, "code": "RECOVERY_CLOSED", "message": "该申请已按恢复办结，不再计算缺测影响"}
        fresh_id = entry.get("时效记录ID") or entry.get("freshness_id")
        fresh = store.find("data_freshness", int(fresh_id)) if str(fresh_id or "").isdigit() else None
        cert = entry.get("告警凭证") or entry.get("缺测凭证") or (fresh or {}).get("告警凭证")
        anomaly = next((a for a in store.rows("anomaly_metric") if a.get("告警凭证") == cert), None)
        metric = entry.get("指标编码") or (fresh or {}).get("指标编码", "")
        window = entry.get("计算窗口") or entry.get("数据窗口") or (fresh or {}).get("计算窗口", "")
        related_jobs = [
            j for j in store.rows("metric_compute")
            if j.get("指标编码") == metric and str(j.get("计算窗口") or "") == str(window)
        ]
        now = datetime.now(timezone.utc).isoformat()
        impact_data = {
            "report_type": "missing_observation_impact",
            "metric_code": metric,
            "window": window,
            "freshness_id": fresh.get("id") if fresh else fresh_id,
            "certificate": cert,
            "anomaly_id": anomaly.get("id") if anomaly else None,
            "missing_reason": (fresh or {}).get("缺数原因") or entry.get("申请原因", ""),
            "delay_minutes": (fresh or {}).get("延迟分钟", ""),
            "related_job_count": len(related_jobs),
            "job_batches": [j.get("作业批次") or j.get("id") for j in related_jobs],
            "generated_at": now,
        }
        entry.update({
            "status": "已完成",
            "生成状态": "已完成",
            "报告数据状态": "已计算",
            "报告数据": impact_data,
            "影响范围": f"指标 {metric} 的窗口 {window}；关联作业 {len(related_jobs)} 个",
            "办理状态": "历史缺测影响已生成",
            "办结类型": entry.get("办结类型") or "历史缺测影响调查",
            "pending": False,
            "abnormal": False,
            "生成时间": now,
        })
        timeline = entry.setdefault("办理轨迹", [])
        if isinstance(timeline, list):
            timeline.append({"时间": now, "事件": "已根据真实缺测凭证计算历史缺测及影响数据"})
        entry["最近办理"] = "缺测影响报告数据已生成"
        return {"ok": True, "message": "缺测影响报告数据已生成，历史凭证和影响范围可审阅", "entry": entry, "report_data": impact_data}

    def claim_application(self, entry_id: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "http_status": 404, "message": "报告申请不存在"}
        version = int(entry.get("version") or 1) + 1
        entry["version"] = version
        entry["claim_token"] = f"CLAIM-RPT-{entry_id}-v{version}"
        entry["领取人"] = context.get("owner") or context.get("值班员") or "值班员"
        entry["领取时间"] = datetime.now(timezone.utc).isoformat()
        return {"ok": True, "message": "已领取报告申请", "entry": entry, "claim_token": entry["claim_token"]}

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
        if action in {"开始生成", "计算报告数据", "生成影响数据"}:
            row = store.find(self.MODULE, entry_id)
            text = f"{(row or {}).get('报告类型', '')}{(row or {}).get('报告模板', '')}"
            if row is not None and ("缺测影响" in text or row.get("时效记录ID") or row.get("告警凭证")):
                return self.generate_missing_impact(entry_id)
            return super().dispatch_action(entry_id, action, ctx)
        if action in {"调查历史缺测", "调查历史缺测及其影响", "继续影响调查", "investigate", "investigate_history"}:
            return self.investigate_historical_missing(entry_id, ctx)
        if action in {"恢复办结", "按恢复办结", "close_recovery", "recover"}:
            return self.close_as_recovery(entry_id, ctx)
        if action in {"领取", "领取申请", "领取报告", "claim"}:
            return self.claim_application(entry_id, ctx)
        return super().dispatch_action(entry_id, action, ctx)
