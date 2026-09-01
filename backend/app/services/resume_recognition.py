from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any


PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)


def _first_match(patterns: list[str], text: str) -> tuple[str, str] | tuple[None, None]:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip(), match.group(0).strip()
    return None, None


def _filename_name(filename: str) -> str | None:
    stem = PurePath(filename).stem
    stem = re.sub(r"(?i)(resume|cv)", "", stem)
    stem = re.sub(r"(个人)?简历|应聘|求职|附件|最新版|候选人", "", stem)
    stem = re.sub(r"[_\-—()（）\[\]\d]+", " ", stem).strip()
    for token in stem.split():
        if re.fullmatch(r"[\u4e00-\u9fff]{2,4}", token):
            return token
    return None


def _looks_like_company(value: str) -> bool:
    return bool(re.search(r"公司|集团|科技|银行|事务所|中心|研究院|学校|医院|传媒|咨询|网络|实业|有限", value))


def _split_company_title(value: str) -> tuple[str | None, str | None]:
    cleaned = re.sub(r"^[|｜·•\s]+|[|｜·•\s]+$", "", value).strip()
    parts = [part.strip() for part in re.split(r"\s*[|｜·•]\s*|\s{2,}", cleaned) if part.strip()]
    if len(parts) >= 2:
        company_index = next((index for index, part in enumerate(parts) if _looks_like_company(part)), 0)
        company = parts[company_index]
        title = next((part for index, part in enumerate(parts) if index != company_index), None)
        return company, title
    company_match = re.search(r"(.{2,40}?(?:有限公司|股份公司|公司|集团|银行|事务所|研究院|中心|医院|学校|科技))\s+(.{2,30})$", cleaned)
    if company_match:
        return company_match.group(1).strip(), company_match.group(2).strip()
    return (cleaned, None) if _looks_like_company(cleaned) else (None, None)


def _role_from_work_history(text: str) -> tuple[str | None, str | None, str | None, float]:
    """Read the latest/current role from common Chinese resume layouts."""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    current_marker = re.compile(r"(?:20\d{2}(?:[./年-]\d{1,2}月?)?)\s*(?:[-—–~至到]+)\s*(?:至今|现在|present)", re.IGNORECASE)
    for index, line in enumerate(lines):
        marker = current_marker.search(line)
        if not marker:
            continue
        tail = line[marker.end():].strip(" ：:|｜·•")
        company, title = _split_company_title(tail)
        lookahead = lines[index + 1:index + 4]
        if not company:
            company = next((value for value in lookahead if _looks_like_company(value)), None)
        if not title:
            title = next((value for value in lookahead if value != company and not re.search(r"工作内容|职责|项目|教育经历", value)), None)
        if company or title:
            proof = " / ".join([line, *lookahead[:2]])
            return company, title, proof, 0.9

    work_heading = next((index for index, line in enumerate(lines) if re.fullmatch(r"(?:工作|职业|任职)经历", line)), None)
    if work_heading is not None:
        block = lines[work_heading + 1:work_heading + 8]
        company = next((value for value in block if _looks_like_company(value)), None)
        if company:
            company_index = block.index(company)
            combined_company, combined_title = _split_company_title(company)
            company = combined_company or company
            title = combined_title or next(
                (value for value in block[company_index + 1:company_index + 3] if not re.search(r"20\d{2}|工作内容|职责", value)),
                None,
            )
            return company, title, " / ".join(block[:4]), 0.72
    return None, None, None, 0


def recognize_resume(text: str, filename: str) -> dict[str, Any]:
    """Extract reviewable resume facts without making a hiring judgment."""
    compact = re.sub(r"[ \t]+", " ", text)
    fields: dict[str, Any] = {
        "name": None,
        "phone": None,
        "email": None,
        "years_experience": None,
        "highest_education": None,
        "current_company": None,
        "current_title": None,
        "location": None,
    }
    confidence: dict[str, float] = {key: 0 for key in fields}
    evidence: dict[str, str] = {}

    name, proof = _first_match(
        [r"(?:姓名|候选人)\s*[:：]\s*([\u4e00-\u9fff·]{2,8})"], compact
    )
    if name:
        fields["name"], confidence["name"], evidence["name"] = name, 0.98, proof
    else:
        name = _filename_name(filename)
        if name:
            fields["name"], confidence["name"] = name, 0.68
            evidence["name"] = f"来自文件名：{PurePath(filename).name}"

    phone_match = PHONE_RE.search(compact)
    if phone_match:
        fields["phone"], confidence["phone"] = phone_match.group(1), 0.99
        evidence["phone"] = phone_match.group(0)
    email_match = EMAIL_RE.search(compact)
    if email_match:
        fields["email"], confidence["email"] = email_match.group(0).lower(), 0.99
        evidence["email"] = email_match.group(0)

    years, proof = _first_match(
        [r"(\d{1,2})\s*年(?:以上)?(?:工作|从业|相关)?经验", r"工作年限\s*[:：]\s*(\d{1,2})\s*年"],
        compact,
    )
    if years:
        fields["years_experience"], confidence["years_experience"] = int(years), 0.92
        evidence["years_experience"] = proof

    education, proof = _first_match(
        [r"(?:最高学历|学历)\s*[:：]\s*(博士|硕士|本科|大专|高中)", r"(博士|硕士|本科|大专)\s*(?:学历|毕业)"],
        compact,
    )
    if education:
        fields["highest_education"], confidence["highest_education"] = education, 0.9
        evidence["highest_education"] = proof

    mappings = {
        "current_company": [r"(?:目前公司|当前公司|所在公司|公司)\s*[:：]\s*([^\n，,]{2,40})"],
        "current_title": [r"(?:目前职位|当前职位|应聘职位|职位)\s*[:：]\s*([^\n，,]{2,30})"],
        "location": [r"(?:现居地|所在地|所在城市|城市)\s*[:：]\s*([^\n，,]{2,20})"],
    }
    for field, patterns in mappings.items():
        value, proof = _first_match(patterns, compact)
        if value:
            fields[field], confidence[field], evidence[field] = value, 0.86, proof

    if not fields["current_company"] or not fields["current_title"]:
        company, title, proof, role_confidence = _role_from_work_history(text)
        if company and not fields["current_company"]:
            fields["current_company"] = company
            confidence["current_company"] = role_confidence
            evidence["current_company"] = proof
        if title and not fields["current_title"]:
            fields["current_title"] = title
            confidence["current_title"] = role_confidence
            evidence["current_title"] = proof

    required_scores = [confidence["name"], max(confidence["phone"], confidence["email"])]
    overall = round(sum(required_scores) / len(required_scores), 2)
    warnings = []
    if not fields["name"]:
        warnings.append("未识别姓名，请人工补充")
    elif confidence["name"] < 0.8:
        warnings.append("姓名来自文件名，请核对")
    if not fields["phone"] and not fields["email"]:
        warnings.append("未识别手机号或邮箱，请核对联系方式")
    if not fields["current_company"] or not fields["current_title"]:
        warnings.append("未完整识别当前公司或职位，请结合工作经历核对")

    return {
        "fields": fields,
        "confidence": confidence,
        "evidence": evidence,
        "overall_confidence": overall,
        "warnings": warnings,
        "recognition_version": "resume-rules-v0.2",
        "decision": None,
        "boundary": "仅识别候选人资料，不进行筛选、排序或录用判断",
    }
