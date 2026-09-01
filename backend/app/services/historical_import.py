from __future__ import annotations

import csv
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import PurePosixPath
import re
from typing import Any
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

from .catalog import ROUND_CATALOG


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_ROWS = 500
POSITIVE_OUTCOMES = {"offer_approval", "hired", "probation_passed"}

NAME_ALIASES = ("姓名", "候选人姓名", "候选人", "应聘者", "name")
OUTCOME_ALIASES = (
    "最终结果", "招聘结果", "录用结果", "候选人状态", "流程结果", "最终结论", "offer结果", "结果", "状态",
)
EVALUATION_MARKERS = ("评价", "意见", "结论", "备注", "优点", "风险", "面试反馈", "面试记录")
IGNORED_PII_MARKERS = ("手机号", "电话", "邮箱", "身份证", "简历", "住址", "微信")

NEGATIVE_MARKERS = ("不足", "较弱", "欠缺", "较差", "不通过", "未达到", "风险", "缺乏")
POSITIVE_MARKERS = ("优秀", "良好", "突出", "较强", "通过", "推荐", "符合", "胜任")


class HistoricalImportError(ValueError):
    pass


def preview_historical_export(content: bytes, filename: str) -> dict[str, Any]:
    if not content:
        raise HistoricalImportError("文件为空")
    if len(content) > MAX_FILE_BYTES:
        raise HistoricalImportError("文件超过 10 MB")
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".csv":
        headers, rows = _parse_csv(content)
    elif suffix == ".xlsx":
        headers, rows = _parse_xlsx(content)
    else:
        raise HistoricalImportError("历史样本当前支持 .xlsx 和 .csv")
    if not headers:
        raise HistoricalImportError("没有识别到表头")
    if not rows:
        raise HistoricalImportError("表格中没有数据行")
    if len(rows) > MAX_ROWS:
        raise HistoricalImportError(f"一次最多导入 {MAX_ROWS} 行")

    normalized_headers = {_normalize_header(item): item for item in headers if item}
    outcome_header = _match_alias(normalized_headers, OUTCOME_ALIASES)
    name_header = _match_alias(normalized_headers, NAME_ALIASES)
    evaluation_headers = [
        original
        for normalized, original in normalized_headers.items()
        if any(marker in normalized for marker in EVALUATION_MARKERS)
    ]
    ignored_headers = [
        original
        for normalized, original in normalized_headers.items()
        if any(marker in normalized for marker in IGNORED_PII_MARKERS)
    ]
    if not outcome_header:
        raise HistoricalImportError("没有识别到招聘结果列，请使用“最终结果 / 招聘结果 / 录用结果”等表头")

    file_hash = hashlib.sha256(content).hexdigest()
    items = []
    for index, row in enumerate(rows, start=2):
        raw_outcome = _cell_text(row.get(outcome_header))
        outcome = _normalize_outcome(raw_outcome)
        evaluation_text = "。".join(
            _cell_text(row.get(header))
            for header in evaluation_headers
            if _cell_text(row.get(header))
        )
        signals = _extract_signals(headers, row, evaluation_text)
        flags = []
        if outcome == "unknown":
            flags.append("招聘结果无法确认")
        if not signals:
            flags.append("未识别到能力信号")
        display_name = _mask_name(_cell_text(row.get(name_header))) if name_header else "匿名样本"
        record_material = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        record_hash = hashlib.sha256(
            f"{file_hash}|{index}|{record_material}".encode("utf-8")
        ).hexdigest()
        items.append({
            "row_number": index,
            "display_ref": f"第 {index} 行 · {display_name}",
            "outcome": outcome,
            "outcome_label": _outcome_label(outcome),
            "competency_signals": signals,
            "quality_flags": flags,
            "eligible_for_profile": outcome in POSITIVE_OUTCOMES and bool(signals),
            "record_hash": record_hash,
        })

    eligible = sum(item["eligible_for_profile"] for item in items)
    return {
        "filename": PurePosixPath(filename).name[:256],
        "file_hash": file_hash,
        "headers": headers,
        "mapping": {
            "outcome": outcome_header,
            "name": name_header,
            "evaluation_columns": evaluation_headers,
            "ignored_pii_columns": ignored_headers,
        },
        "items": items,
        "summary": {
            "total_rows": len(items),
            "eligible_rows": eligible,
            "needs_review": sum(bool(item["quality_flags"]) for item in items),
            "ignored_pii_columns": len(ignored_headers),
        },
        "privacy": "姓名仅在浏览器中掩码显示；联系方式、简历和评价原文不会写入历史样本池。",
    }


def _parse_csv(content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    text = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise HistoricalImportError("CSV 编码无法识别，请导出 UTF-8 CSV")
    reader = csv.reader(StringIO(text))
    matrix = [list(row) for row in reader if any(str(value).strip() for value in row)]
    return _matrix_to_rows(matrix)


def _parse_xlsx(content: bytes) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            shared = _shared_strings(archive)
            sheet_path = _first_sheet_path(archive)
            root = ElementTree.fromstring(archive.read(sheet_path))
    except (BadZipFile, KeyError, ElementTree.ParseError) as error:
        raise HistoricalImportError(f"Excel 文件无法解析：{error}") from error

    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    matrix: list[list[str]] = []
    for row_node in root.iter(namespace + "row"):
        values: dict[int, str] = {}
        for cell in row_node.findall(namespace + "c"):
            reference = cell.attrib.get("r", "A1")
            column = _column_index(reference)
            cell_type = cell.attrib.get("t")
            value_node = cell.find(namespace + "v")
            if cell_type == "inlineStr":
                text_nodes = cell.findall(".//" + namespace + "t")
                value = "".join(node.text or "" for node in text_nodes)
            else:
                raw = value_node.text if value_node is not None else ""
                if cell_type == "s" and raw.isdigit():
                    position = int(raw)
                    value = shared[position] if position < len(shared) else ""
                elif cell_type == "b":
                    value = "是" if raw == "1" else "否"
                else:
                    value = raw
            values[column] = value
        if values:
            maximum = max(values)
            matrix.append([values.get(index, "") for index in range(maximum + 1)])
    return _matrix_to_rows(matrix)


def _shared_strings(archive: ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read(path))
    namespace = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    return [
        "".join(node.text or "" for node in item.iter(namespace + "t"))
        for item in root.iter(namespace + "si")
    ]


def _first_sheet_path(archive: ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relation_namespace = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    package_namespace = "{http://schemas.openxmlformats.org/package/2006/relationships}"
    sheet = next(workbook.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"), None)
    if sheet is None:
        raise HistoricalImportError("Excel 中没有工作表")
    relation_id = sheet.attrib.get(relation_namespace + "id")
    target = None
    for relation in relationships.iter(package_namespace + "Relationship"):
        if relation.attrib.get("Id") == relation_id:
            target = relation.attrib.get("Target")
            break
    if not target:
        raise HistoricalImportError("Excel 工作表关系缺失")
    target_path = PurePosixPath(target.lstrip("/"))
    normalized = target_path if target_path.parts[:1] == ("xl",) else PurePosixPath("xl") / target_path
    parts = []
    for part in normalized.parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def _matrix_to_rows(matrix: list[list[Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    if not matrix:
        return [], []
    header_index = next(
        (index for index, row in enumerate(matrix) if sum(bool(_cell_text(value)) for value in row) >= 2),
        0,
    )
    raw_headers = [_cell_text(value) or f"未命名列{index + 1}" for index, value in enumerate(matrix[header_index])]
    seen: dict[str, int] = {}
    headers = []
    for header in raw_headers:
        seen[header] = seen.get(header, 0) + 1
        headers.append(header if seen[header] == 1 else f"{header}_{seen[header]}")
    rows = []
    for values in matrix[header_index + 1:]:
        row = {header: _cell_text(values[index]) if index < len(values) else "" for index, header in enumerate(headers)}
        if any(row.values()):
            rows.append(row)
    return headers, rows


def _extract_signals(
    headers: list[str], row: dict[str, Any], evaluation_text: str
) -> list[dict[str, Any]]:
    competencies = []
    seen = set()
    for round_items in ROUND_CATALOG.values():
        for item in round_items:
            if item["id"] not in seen:
                seen.add(item["id"])
                competencies.append(item)
    signals: dict[str, dict[str, Any]] = {}
    for competency in competencies:
        matching_headers = [
            header for header in headers
            if competency["name"] in _normalize_header(header)
            or competency["id"].replace("_", "") in _normalize_header(header).lower()
        ]
        for header in matching_headers:
            value = _cell_text(row.get(header))
            if value:
                signals[competency["id"]] = {
                    "competency_id": competency["id"],
                    "competency_name": competency["name"],
                    "direction": _signal_direction(value),
                    "confidence": 0.9,
                    "source": "structured_column",
                }
                break
    compact_evaluation = evaluation_text.replace(" ", "")
    for competency in competencies:
        if competency["id"] in signals:
            continue
        matched_keywords = [item for item in competency.get("keywords", []) if item in compact_evaluation]
        if competency["name"] in compact_evaluation or len(matched_keywords) >= 2:
            signals[competency["id"]] = {
                "competency_id": competency["id"],
                "competency_name": competency["name"],
                "direction": _signal_direction(compact_evaluation),
                "confidence": 0.65,
                "source": "evaluation_text",
            }
    return list(signals.values())[:8]


def _signal_direction(value: str) -> str:
    compact = value.strip().lower()
    numeric = re.search(r"(?<!\d)([1-5])(?:\.0)?(?!\d)", compact)
    if numeric:
        score = int(numeric.group(1))
        return "positive" if score >= 4 else "negative" if score <= 2 else "mentioned"
    if any(marker in compact for marker in NEGATIVE_MARKERS):
        return "negative"
    if any(marker in compact for marker in POSITIVE_MARKERS):
        return "positive"
    return "mentioned"


def _normalize_outcome(value: str) -> str:
    compact = re.sub(r"\s+", "", value).lower()
    if any(marker in compact for marker in ("试用期未通过", "试用不通过", "未转正")):
        return "probation_failed"
    if any(marker in compact for marker in ("试用期通过", "转正", "试用通过")):
        return "probation_passed"
    if any(marker in compact for marker in ("不录用", "淘汰", "拒绝", "未通过", "放弃")):
        return "rejected"
    if any(marker in compact for marker in ("已入职", "入职", "已到岗")):
        return "hired"
    if any(marker in compact for marker in ("拟录用", "录用", "offer", "通过", "推荐")):
        return "offer_approval"
    return "unknown"


def _outcome_label(outcome: str) -> str:
    return {
        "offer_approval": "进入录用 / 通过",
        "hired": "已入职",
        "probation_passed": "试用期通过",
        "probation_failed": "试用期未通过",
        "rejected": "未录用",
        "unknown": "待确认",
    }[outcome]


def _match_alias(headers: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    normalized_aliases = [_normalize_header(item) for item in aliases]
    for alias in normalized_aliases:
        if alias in headers:
            return headers[alias]
    for normalized, original in headers.items():
        if any(alias in normalized for alias in normalized_aliases):
            return original
    return None


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-—/（）()【】\[\]：:]+", "", str(value or "")).strip().lower()


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _mask_name(value: str) -> str:
    compact = value.strip()
    if not compact:
        return "匿名样本"
    if len(compact) == 1:
        return compact + "*"
    return compact[0] + "*" * min(2, len(compact) - 1)


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    result = 0
    for character in letters.group(0):
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1
