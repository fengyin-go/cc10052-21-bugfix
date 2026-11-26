"""时效、缺测凭证和跨模块处置共用的无状态工具。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> tuple[datetime | None, str | None]:
    """解析检查/完成时间。

    返回 (datetime, None) 或 (None, 原因)。解析失败绝不静默当成零或当前时间。
    """
    if value is None or value == "":
        return None, "时间为空"
    if isinstance(value, datetime):
        dt = value
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc), None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 10_000_000_000:  # 毫秒时间戳
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc), None
        except (OverflowError, OSError, ValueError) as exc:
            return None, f"时间戳无法解析：{value}（{exc}）"

    text = str(value).strip()
    if not text:
        return None, "时间为空"
    if text.isdigit() and len(text) in (10, 13):
        return parse_datetime(int(text))
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None, f"时间格式无法解析：{text}"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, None


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def first_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def as_evidence(raw: Any) -> dict[str, Any] | None:
    """把补报响应/作业对象/资源 envelope 统一成证据字典。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, dict):
        for key in ("evidence", "作业证据", "job", "作业", "entry", "data", "补报响应", "response"):
            nested = raw.get(key)
            if isinstance(nested, dict) and any(
                marker in nested
                for marker in ("id", "作业批次", "批次", "指标编码", "计算窗口", "完成时间", "status")
            ):
                merged = {k: v for k, v in raw.items() if k not in (key,)}
                merged.update(nested)
                return merged
        return dict(raw)
    if isinstance(raw, (int, str)) and str(raw).strip().isdigit():
        return {"id": int(str(raw).strip())}
    return None


def append_timeline(row: dict[str, Any], event: str, *, at: str | None = None, **detail: Any) -> None:
    timeline = row.setdefault("办理轨迹", [])
    if not isinstance(timeline, list):
        timeline = []
        row["办理轨迹"] = timeline
    item = {"时间": at or iso(), "事件": event}
    item.update(detail)
    timeline.append(item)
    # 表格和旧调用方使用字符串摘要，列表/接口仍保留结构化轨迹。
    row["最近办理"] = event
    row["办理轨迹摘要"] = "；".join(str(x.get("事件", "")) for x in timeline[-5:] if isinstance(x, dict))


def next_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1


def is_completed_job(job: dict[str, Any]) -> bool:
    status = str(first_value(job, "status", "作业状态", "state", "状态") or "").strip().lower()
    return status in {"已完成", "完成", "completed", "complete", "succeeded", "success", "done"}
