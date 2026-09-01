from __future__ import annotations

from typing import Any


ROUND_CATALOG: dict[str, list[dict[str, Any]]] = {
    "hr": [
        {
            "id": "motivation",
            "name": "求职动机",
            "description": "对岗位、行业和本次机会的理解与真实动机",
            "keywords": ["动机", "选择", "岗位", "行业", "机会", "发展"],
            "question": "你为什么考虑这个岗位？哪些因素对你的选择最重要？",
            "follow_up": "能结合一个具体选择说明你当时如何权衡吗？",
        },
        {
            "id": "communication",
            "name": "沟通表达",
            "description": "结构化、准确并能根据听众调整表达",
            "keywords": ["沟通", "表达", "汇报", "共识", "协调", "反馈"],
            "question": "请讲一次你需要推动不同意见者达成共识的经历。",
            "follow_up": "对方最初的分歧是什么，你具体改变了哪种沟通方式？",
        },
        {
            "id": "values",
            "name": "价值观与协作",
            "description": "行为方式与组织基本原则是否一致",
            "keywords": ["价值", "原则", "协作", "团队", "冲突", "责任"],
            "question": "请讲一次你在结果压力与团队原则之间做取舍的经历。",
            "follow_up": "你当时坚持了什么，又放弃了什么？",
        },
        {
            "id": "stability",
            "name": "经历连贯性",
            "description": "职业选择的逻辑、预期和风险是否可解释",
            "keywords": ["离职", "跳槽", "稳定", "职业", "规划", "原因"],
            "question": "回看最近两次职业选择，它们背后的共同逻辑是什么？",
            "follow_up": "什么情况可能导致这次选择不符合你的预期？",
        },
    ],
    "business": [
        {
            "id": "domain_expertise",
            "name": "专业能力",
            "description": "岗位所需专业知识与真实实践深度",
            "keywords": ["项目", "业务", "专业", "方案", "指标", "用户"],
            "question": "选一个与你应聘岗位最相关的项目，讲清目标、约束和你的职责。",
            "follow_up": "其中哪一个关键判断是你独立做出的，依据是什么？",
        },
        {
            "id": "problem_solving",
            "name": "问题解决",
            "description": "定义问题、验证假设和复盘结果的能力",
            "keywords": ["问题", "分析", "假设", "验证", "原因", "复盘"],
            "question": "讲一次信息不完整但必须快速解决的复杂问题。",
            "follow_up": "你排除了哪些假设，使用了什么证据？",
        },
        {
            "id": "ownership",
            "name": "结果担当",
            "description": "主动承担、推进闭环并对结果负责",
            "keywords": ["负责", "推动", "结果", "目标", "上线", "交付"],
            "question": "讲一次结果可能失控时你主动接手并完成闭环的经历。",
            "follow_up": "失控风险最早出现在哪里？你当时作出了哪个关键判断？",
        },
        {
            "id": "collaboration",
            "name": "跨团队协作",
            "description": "在边界和利益不一致时建立合作",
            "keywords": ["协作", "跨部门", "资源", "冲突", "共识", "配合"],
            "question": "讲一次需要跨部门争取资源的经历。",
            "follow_up": "如果对方没有配合义务，你用什么交换条件促成合作？",
        },
    ],
    "ceo": [
        {
            "id": "strategic_alignment",
            "name": "战略理解",
            "description": "理解业务方向、关键约束和长期价值",
            "keywords": ["战略", "长期", "业务", "增长", "竞争", "价值"],
            "question": "如果加入，你认为这个岗位未来一年最应创造的业务价值是什么？",
            "follow_up": "在资源减半的情况下，你会保留哪一件事？",
        },
        {
            "id": "learning_agility",
            "name": "学习敏捷",
            "description": "快速修正认知并把学习转化为结果",
            "keywords": ["学习", "改变", "复盘", "错误", "迭代", "认知"],
            "question": "讲一次事实证明你原判断错误，并快速修正的经历。",
            "follow_up": "什么证据真正改变了你的判断？",
        },
        {
            "id": "leadership_potential",
            "name": "影响力与领导潜力",
            "description": "在没有正式权力时影响他人并承担艰难决定",
            "keywords": ["影响", "决策", "团队", "带领", "推动", "责任"],
            "question": "讲一次你没有正式权力但仍推动关键改变的经历。",
            "follow_up": "谁承担了成本，你如何获得对方支持？",
        },
        {
            "id": "risk_judgement",
            "name": "风险判断",
            "description": "识别关键风险、权衡速度与质量",
            "keywords": ["风险", "取舍", "成本", "质量", "速度", "底线"],
            "question": "讲一次你面对高收益但高风险机会时的取舍。",
            "follow_up": "你设置了什么止损条件？",
        },
    ],
}


def competencies_for(round_type: str, job_competencies: list[dict]) -> list[dict]:
    if round_type == "custom" and job_competencies:
        return job_competencies
    return ROUND_CATALOG.get(round_type, ROUND_CATALOG["business"])
