"""Create a department-routed, fictional 30-document corpus without API calls.

Read the original 20-document pack, preserve it, and write a separate edition.
Run with the bundled document runtime. This never uploads or indexes documents.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from collections import Counter
from pathlib import Path

from docx import Document
from docx.table import Table as WordTable
from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import CondPageBreak, SimpleDocTemplate, TableStyle

from generate_xinghai_corpus import (
    COMPANY, FONT_NAME, FONT_PATH, NOTICE, ROOT, PAGE_W, PAGE_H, MARGIN,
    dump_json, iter_blocks, make_pdf, paragraph, pdf_table, page_furniture,
)

SOURCE = ROOT / "output/pdf/xinghai-tech-corpus"
DEFAULT_OUTPUT = ROOT / "output/pdf/xinghai-department-corpus-v2"
GROUPS = {
    "公司公共": {"folder": "01_公司公共", "knowledge_base": "公司公共知识库", "department": "跨部门公共资料", "access": "本公司已授权员工"},
    "人力资源部": {"folder": "02_人力资源部", "knowledge_base": "HR知识库", "department": "人力资源部", "access": "人力资源部成员及显式授权人员"},
    "技术部": {"folder": "03_技术部", "knowledge_base": "技术知识库", "department": "技术部", "access": "技术部成员及显式授权人员"},
    "平台研发部": {"folder": "04_平台研发部", "knowledge_base": "平台研发知识库", "department": "平台研发部", "access": "平台研发部成员及显式授权人员"},
}
# A public knowledge base is an access scope, not a fourth department.
OLD_ROUTING = {
    1: ("人力资源部", "人力资源部"),
    2: ("公司公共", "人力资源部财务支持组"),
    3: ("公司公共", "技术部信息安全组"),
    4: ("公司公共", "人力资源部采购支持组"),
    5: ("技术部", "技术部项目交付组"),
    6: ("技术部", "技术部客户支持组"),
    7: ("公司公共", "人力资源部"),
    8: ("公司公共", "人力资源部财务支持组"),
    9: ("技术部", "技术部信息安全组"),
    10: ("平台研发部", "平台研发部"),
    11: ("平台研发部", "平台研发部"),
    12: ("平台研发部", "平台研发部"),
    13: ("技术部", "技术部项目交付组"),
    14: ("平台研发部", "平台研发部"),
    15: ("平台研发部", "平台研发部"),
    16: ("公司公共", "人力资源部与技术部知识运营组"),
    17: ("平台研发部", "平台研发部"),
    18: ("技术部", "技术部项目交付组"),
    19: ("技术部", "技术部客户支持组"),
    20: ("人力资源部", "人力资源部培训支持组"),
}


def new_document(number, title, extension, group, code, sections, facts, features=()):
    return dict(number=number, title=title, extension=extension, group=group,
                code=code, sections=sections, facts=facts, features=list(features))


NEW_DOCUMENTS = [
    new_document(21, "新员工入职与试用期评估操作手册", "pdf", "人力资源部", "XH-HR-2026-21", [
        ("1 适用范围与职责", [
            "本手册用于办理新员工入职和试用期过程评估。人力资源专员核对材料并维护节点；直属经理安排岗位任务和导师；技术部只执行已批准的办公账号与设备申请，不自行决定员工身份。",
            "入职清单不包含真实身份证号、银行卡号或薪资。上述材料进入受控人事系统，不上传到问答知识库。试用期长度以个人劳动合同为准，本手册不设统一长度，也不替代合同或适用法律。",
        ]),
        ("2 入职节点", [["节点", "责任人", "完成时限", "留存证据"],
            ["材料核对", "人力资源专员", "到岗前2个工作日", "材料完成状态，不附敏感原件"],
            ["办公账号申请", "直属经理", "到岗前1个工作日", "岗位与知识库授权范围"],
            ["入职培训", "培训支持组", "到岗后5个工作日内", "安全培训与制度确认记录"],
            ["岗位任务与导师", "直属经理", "到岗后3个工作日内", "书面岗位目标和导师姓名"],
            ["阶段回访", "人力资源专员", "到岗后第30个自然日", "回访问题与处理人"],
        ]),
        ("3 试用期评估", [
            "直属经理在约定试用期结束前10个工作日提交评估，说明任务完成情况、证据和改进事项。导师意见属于输入材料，不能替代直属经理的结论。人力资源部检查记录是否完整，再按合同及适用法律处理。",
            "延期、转岗或不通过的处理不能由本手册自动决定；需要人力资源部核查依据和授权，完成必要沟通。评估不包含未核实的健康、家庭或其他与岗位无关的信息。",
        ]),
        ("4 异常与权限", "到岗日期改变时，专员在1个工作日内更新任务单并通知技术部。生产访问须另走平台研发部审批，不随办公账号自动发放。部门版清单中的人员状态仅供本部门授权角色查阅，普通员工不得检索他人的入职材料。"),
    ], [("新员工的岗位任务和导师应在到岗后多久确定？", "到岗后3个工作日内，由直属经理确定。", ["3个工作日", "直属经理"])]),
    new_document(22, "季度绩效评估与复核流程", "md", "人力资源部", "XH-HR-2026-22", [
        ("1 评估边界", [
            "本流程用于组织季度工作目标评估，适用于已纳入当季考核的岗位。项目验收、客户投诉和故障复盘可以作为事实证据，但不能直接等同于个人绩效等级。本文不提供真实员工评级、薪酬或奖金系数。",
            "每季开始后5个工作日内，员工与直属经理确认3至5项目标，记录验收标准及证据来源。目标变更须双方书面确认并保留原版本，不能在期末单方面改写目标。",
        ]),
        ("2 工作分工", [["角色", "工作内容", "不能替代的责任"],
            ["员工", "提交结果与证据，说明外部依赖", "不能自行审批自己的等级"],
            ["直属经理", "核实证据并进行反馈面谈", "不能省略书面反馈"],
            ["人力资源部", "组织校准并检查流程一致性", "不能凭部门预算直接改等级"],
            ["部门负责人", "复核异常分布与争议处理", "不能只凭印象否定证据"],
        ]),
        ("3 复核时限", [
            "员工对结果有异议时，应在收到书面反馈后5个工作日内提交复核申请，列明争议目标、证据与请求。人力资源部在收到完整材料后7个工作日内组织复核；材料缺失时先告知补充项，不能将不完整材料的等待期算作办理完成。",
            "存在利益冲突的评估人应回避。复核结论说明是否调整及其依据，并由申请人确认已收到；确认收到不代表同意结论。超期申请由人力资源负责人决定是否受理并记录理由。",
        ]),
        ("4 资料使用", "面谈记录与个体评级存放在人事系统。知识库只收录制度与脱敏范例；完整评级、排序和奖金数据不放入公司公共库。季度材料按内部测试设定保留24个月，超期由人力资源部审批清理，不由问答系统自行删除。"),
    ], [("绩效结果复核申请要在什么时候提交，HR多久组织复核？", "收到书面反馈后5个工作日内申请；HR收到完整材料后7个工作日内组织复核。", ["5个工作日", "完整材料", "7个工作日"])], ["Markdown标题", "职责表格", "条件与期限"]),
    new_document(23, "内部培训与学习费用审批指引", "md", "人力资源部", "XH-HR-2026-23", [
        ("1 培训类型", "本指引区分必修培训、部门技能培训和外部课程。必修培训由培训支持组安排，包括入职信息安全、权限使用与来源核对。参加培训不自动获得生产账号、客户数据访问或知识库权限。"),
        ("2 费用审批", [["情形", "审批要求", "证据"],
            ["内部免费培训", "直属经理确认时间安排", "报名与出席记录"],
            ["外部课程每人每次不超过2000元", "直属经理批准并由HR登记", "课程说明与岗位相关性"],
            ["超过2000元至8000元", "部门负责人及HR负责人批准", "报价、目标与预算来源"],
            ["超过8000元", "部门负责人、HR负责人及财务支持组复核", "书面预算及必要性说明"],
        ]),
        ("3 申请与结项", [
            "付费课程应在开课前5个工作日提交申请；审批未完成前不得预付费用。费用表中的边界按单人单次含税总额计算，恰好2000元属于第一档，恰好8000元属于第二档；不得拆分课程以规避审批。",
            "培训结束后10个自然日内提交完成证明、实际费用和学习总结。出席证明只证明参加，不证明已掌握相关技能；需要上岗验证的岗位由技术负责人另行安排。报销票据与提交时限按现行XH-FIN-2026-03版本1.4执行。",
        ]),
        ("4 请假与资料", "无法参加必修培训时，在课程开始前向培训支持组说明原因并申请补课，原则上在15个自然日内完成。授课截图、名单和反馈不得用于公开比较员工；评测语料只保留虚构记录。采购外部培训服务仍须遵循采购准入流程，个人费用审批不替代供应商审批。"),
    ], [("恰好2000元的外部课程需要谁审批？", "直属经理批准并由HR登记，按单人单次含税总额计算。", ["2000元", "直属经理", "HR"])], ["金额边界", "跨文档报销关联"]),
    new_document(24, "部门调动与账号权限交接清单", "txt", "人力资源部", "XH-HR-2026-24", [
        ("1 调动审批", "HR记录调出部门、调入部门、岗位、生效时间和双方负责人批准。只有已批准的调动单才可以触发权限变更，员工口头申请和聊天截图不能替代审批。本文不列真实员工身份或账号。"),
        ("2 权限切换", [
            "调动生效时先撤销原部门不再需要的访问，再授予新部门批准的范围。技术部负责办公系统执行，平台研发部负责生产与研发系统核验。公司公共知识库访问按当前有效授权保留，不能因为换部门自动扩大为全公司资料。",
            "确有交接需求时，可批准最多5个工作日的原部门临时只读访问，须写明知识库、批准人和自动到期时间。旧授权不能因为历史问答中引用过文档而永久保留，后续追问也应重新判断权限。",
        ]),
        ("3 执行与验收", "账号管理员记录撤销和授予的实际时间。调动生效后1个工作日内，HR、调入负责人和执行人共同核对知识库清单、未结工单及设备责任人。发现仍可检索原部门资料时应先停止相应访问，再排查权限和历史会话展示，不修改审计记录掩盖异常。"),
        ("4 示例记录", "虚构单号MOVE-2026-004，生效日为2026年10月8日09:00北京时间。员工由技术部调入平台研发部，原项目客户材料不随调动迁移。获批临时只读范围为原项目知识库，有效至2026年10月14日18:00；该例没有批准技术部所有知识库的持续访问。"),
    ], [("部门调动需要保留原部门临时只读权限时，最长可以批准多久？", "最多5个工作日，必须明确知识库、批准人和自动到期时间。", ["5个工作日", "自动到期"])], ["权限撤销", "明确例外", "纯文本编号"]),
    new_document(25, "员工离职交接与访问撤销流程", "pdf", "人力资源部", "XH-HR-2026-25", [
        ("1 流程范围", "本流程规定已确认离职人员的交接与访问撤销，不设定辞职通知期、补偿或薪资结算规则。劳动关系事项由人力资源部依据实际合同与适用法律处理，本文只是虚构内部操作测试资料。"),
        ("2 交接清单", [["事项", "执行方", "完成节点"],
            ["项目与待办", "直属经理与接收人", "最后工作日结束前"],
            ["办公设备与资产", "技术部资产管理员", "最后工作日结束前"],
            ["部门与公共知识库访问", "账号管理员", "离职生效时"],
            ["生产与代码仓库权限", "平台研发部", "离职生效时"],
            ["审计核对", "HR与信息安全组", "撤权后1个工作日内"],
        ]),
        ("3 撤销原则", [
            "HR在最后工作日前2个工作日发起撤权任务，列出实际离职生效时间。到期时撤销账号会话、直接授权、部门授权与临时授权；不得只移除部门成员关系而遗漏个人授予的权限。撤权时间不以设备归还时间替代。",
            "离职员工不能继续查阅自己的历史问答或通过分享链接访问内部资料。审计与业务资料由公司授权人员按保留策略处理，不因为账号失效而自动删除所有业务记录。账号管理员不向接收人提供离职人员的个人密码或令牌。",
        ]),
        ("4 异常处理", "存在延迟交接时，直属经理指定在职接收人，不恢复离职账号访问。对已确认异常访问，信息安全负责人先止用相关凭据并保留审计证据，再按事件流程处理。HR核验任务完成状态和异常项，不能凭已发送通知就认定撤权成功。"),
    ], [("离职撤权任务由谁在什么时候发起？", "HR在最后工作日前2个工作日发起，实际撤权在离职生效时执行。", ["2个工作日", "生效时"])]),
    new_document(26, "项目测试验收与缺陷分级规范", "md", "技术部", "XH-TECH-2026-26", [
        ("1 适用范围", "本规范用于项目交付的系统测试、用户验收和缺陷登记。技术部测试负责人维护测试基线；平台研发部提供产品修复证据；客户授权代表确认项目验收结果。产品版本发布不能替代具体项目的验收签署。"),
        ("2 缺陷级别", [["级别", "定义", "验收处理"],
            ["D1阻断", "越权、数据错误或核心流程不能完成", "不得进入正式验收"],
            ["D2重大", "关键功能异常且无可接受替代方案", "修复并通过回归后验收"],
            ["D3一般", "存在可接受替代方案，不阻断核心流程", "客户书面接受并明确修复日期后可验收"],
            ["D4建议", "提示或体验改进，不改变业务正确性", "登记产品评估，不自动承诺发布日期"],
        ]),
        ("3 测试证据", [
            "每条缺陷记录包含项目编号、环境、版本、复现步骤、实际结果、期望结果和脱敏附件。回归必须使用批准的测试账号；截图不得包含生产令牌、客户完整数据或与问题无关的人员信息。",
            "正式验收前核验权限撤销、跨部门数据访问、关键审批、导出与Webhook结果。单项测试通过不代表全部范围通过，验收报告应写明覆盖范围和未覆盖项。客户未回复不能视为默认接受。",
        ]),
        ("4 海岚项目示例", "项目XH-PRJ-026的DEF-026-08为导出按钮提示文字优化，属于D3一般缺陷，客户在2026年9月29日书面接受，计划2026年10月9日修复。该计划不代表已经修复。最终验收见XH-PMO-2026-14，CR-026-03的代理权限变更仍需保留回归证据。"),
        ("5 关闭与变更", "测试负责人验证修复版本并记录证据后关闭缺陷。新增需求不能伪装成缺陷免费加入范围；超过2人日或影响里程碑时，按客户项目交付流程取得双方书面批准。故障P1/P2是服务事件级别，与缺陷D1/D2不是同一套编号。"),
    ], [("海岚项目DEF-026-08计划什么时候修复，是否已经修复？", "计划2026年10月9日修复；资料仅记载计划，不能认定已修复。", ["2026年10月9日", "计划"])], ["状态区分", "跨文档关联", "表格"]),
    new_document(27, "报表导出重复提交事件复盘", "pdf", "技术部", "XH-TECH-2026-27", [
        ("1 事件概要", "虚构事件INC-2026-1002发生于2026年10月2日，客户为海岚制造有限公司，涉及生产报表导出重复提交。级别为P2，影响部分导出任务出现重复条目，未发现其他租户数据进入该客户结果。本结论仅针对已核查范围，不证明所有未来事件都没有越权风险。"),
        ("2 已确认时间线", [["北京时间", "事项", "确认依据"],
            ["09:12", "支持台收到重复导出工单", "工单创建记录"],
            ["09:25", "技术部首次响应并收集请求编号", "工单回复记录"],
            ["09:38", "确认重试未复用幂等键", "脱敏调用日志"],
            ["10:05", "客户修正调用配置并停止重复提交", "客户端配置与验证记录"],
            ["10:20", "复核新增导出结果正常", "抽样核对记录"],
        ]),
        ("3 根因与影响", [
            "客户端在HTTP 429后立即创建新请求，并在每次重试时生成新的幂等键，服务端因此将其视为不同任务。已确认的首次响应耗时为13分钟，停止重复提交耗时为53分钟，复核结束耗时为68分钟；三个指标的截止事件不同。",
            "日志显示问题来自调用方重试配置，不是本次数据库迁移，也不意味着所有429都由相同原因引起。影响窗口内共确认6个重复任务，支持人员与客户按任务编号确认取消，未批量删除未知任务。",
        ]),
        ("4 整改与资料使用", [
            "整改项ACT-1002-01由技术部支持负责人顾言维护，截止2026年10月8日，要求上线前检查Retry-After处理、幂等键复用和脱敏日志。平台研发部补充SDK示例并由测试组验证。本文状态为整改跟踪中，不将截止日期写成完成日期。",
            "本复盘只进入技术知识库，不进入公司公共库或HR知识库。客户名称、工单号和人员均为虚构，案例不得用于推断真实客户的事件。外发脱敏日志仍按XH-SEC-2026-09审批并限制接收权限，不能因为是复盘材料就省略审批。",
        ]),
    ], [("INC-2026-1002的首次响应和复核结束分别耗时多久？", "首次响应13分钟，复核结束68分钟；两者都从09:12起算。", ["13分钟", "68分钟"])], ["时间线", "指标区分", "权限隔离"]),
    new_document(28, "知识库检索与多轮问答设计说明", "md", "平台研发部", "XH-RD-2026-28", [
        ("1 设计范围", "本说明描述星海研发测试环境的目标配置，不证明任何真实生产部署已启用。问答先识别已认证用户及租户，再计算可读知识库集合。用户未选择单库时，只在当前授权集合内查询；没有可读知识库时拒绝访问，不能降级为全租户无过滤检索。"),
        ("2 检索配置", [["阶段", "目标配置", "注意事项"],
            ["向量候选", "最多30条", "先做租户、知识库和ready状态过滤"],
            ["关键词候选", "PostgreSQL BM25最多30条", "参数绑定，不拼接用户输入为SQL"],
            ["融合", "向量0.6、关键词0.4，RRF取30条", "跨授权库共享候选预算，不是每库30条"],
            ["重排", "最多保留8条", "重排失败保留融合结果作为降级"],
            ["生成", "仅使用本轮授权证据", "没有支持证据时说明资料不足"],
        ]),
        ("3 多轮与记忆", [
            "会话保存用户问题、回答、引用和每轮授权范围快照。追问重写仅帮助补全指代和检索意图，不能制造证据或扩大权限；原问题与重写后的检索问题应区分记录。无论历史是否包含某部门资料，本轮都按当前权限重新检索。",
            "发生部门调动或授权缩减时，不再向用户展示可能涉及已撤权范围的历史内容。过滤历史只影响本次读取和模型输入，不自动删除数据库原记录；保留与删除由治理策略决定。会话必须同时绑定租户和拥有者，知道会话编号不代表可读取。",
        ]),
        ("4 引用与拒答", "引用记录文档名、页码或章节以及知识库来源。只有检索到资料但不能支持答案时仍应拒答，不能将相似标题当证据。跨库回答只引用可访问来源，不透露被过滤的文档标题、片段或命中数量。权限过滤必须在候选限制和生成之前执行，不能只隐藏前端链接。"),
        ("5 验证方法", "使用独立题集验证事实、金额边界、历史版本、跨页条件、扫描OCR和多轮追问；题集及参考答案不能上传知识库。切分检查不等于检索召回率或回答准确率，模型质量评估需另行授权调用并核对引用证据。"),
    ], [("跨授权知识库检索的候选和重排预算是多少，是每个库各一份吗？", "向量最多30条、BM25最多30条，融合取30条，重排最多8条；预算跨授权库共享，不是每库各一份。", ["30条", "8条", "共享"])], ["技术配置表", "权限边界", "多轮追问"]),
    new_document(29, "文档入库验收与知识更新规范", "pdf", "平台研发部", "XH-RD-2026-29", [
        ("1 入库责任", "文档归口部门确认内容与可见范围，知识运营人员选择目标知识库，平台研发部维护解析与索引链路。目录中的部门名只用于整理，真正的权限由数据库授权和后端检索过滤决定。不得把全部部门文件一次性上传公司公共库。"),
        ("2 格式验收", [["格式", "应保留的信息", "异常处理"],
            ["文本PDF", "标题、页码、跨页段落和表格", "检查页眉清洗与阅读顺序"],
            ["扫描PDF", "真实OCR文本及识别来源", "缺少OCR时明确失败，不用答案替代"],
            ["Markdown", "标题路径、表格、代码围栏", "不将代码与条件段任意拆开"],
            ["TXT", "段落、编号、时间线", "保留工单号与时间上下文"],
        ]),
        ("3 状态与重试", [
            "上传成功不等于可检索。文档需经过解析、切分、关键词构建和向量索引，完成后才标记ready。failed文档不得进入候选集合；重试需保留原失败原因、次数和执行时间，不能用手工改状态代替实际索引。",
            "内容变更时核对编号、版本、生效日期和归档标记。纯排版变化与政策变更应区分；旧差旅标准保留历史用途，但回答当前费用时必须引用现行版本。新旧版都已入库时，不能仅以相似度决定当前有效版本。",
        ]),
        ("4 权限与测试", "入库前执行单部门可读、跨授权可读和未授权拒答三类检查。每个部门至少抽查2份文档的归属与引用来源；敏感材料先脱敏，真实密钥、工资明细和个人身份材料不得用于演示。删除或撤权后核验旧分块、关键词索引和历史展示是否仍可被不当访问。"),
        ("5 运维记录", "知识运营人员登记文件哈希、目标知识库、归口部门、入库批次与失败项。结构验收、OCR验收、检索质量及端到端问答质量应分别记录；不得用文件能打开或生成器运行成功代表质量达标。"),
    ], [("文档上传成功后是否就可以检索，失败文档能否参与候选？", "不能；完成解析和索引并标记ready后才可检索，failed文档不得进入候选。", ["ready", "failed"])], ["入库状态", "格式验收表", "归属与权限"]),
    new_document(30, "Webhook投递重试与幂等处理规范", "txt", "平台研发部", "XH-RD-2026-30", [
        ("1 接口边界", "本规范规定星海开放平台Webhook回调的重试与去重，示例不含真实密钥。接收方先验证签名和事件归属，再处理消息；不能因为接收URL属于本公司就跳过租户核对。签名头和签名算法以XH-TECH-2026-06正式接口手册为准，本文不另设签名算法。"),
        ("2 成功与重试", [
            "首次投递发生后，收到HTTP 2xx视为成功。网络超时、HTTP 429或5xx触发重试；其他4xx直接进入失败待核查，不无限重试。接收方应在5秒内确认，耗时业务应写入自己的队列后返回，不等待完整业务结束才响应。",
            "最多追加5次自动重试，默认间隔依次为30秒、2分钟、10分钟、30分钟和2小时，间隔从上一次失败起算。收到有效Retry-After时，等待不少于该值与默认间隔中的较大者；重试次数不因此重置。总投递次数上限是6次，包含首次投递。",
        ]),
        ("3 幂等与手工重放", "接收方以tenant_id与event_id组合去重，去重记录保留7日。相同事件重放仍使用原event_id，不因delivery_attempt变化再次执行业务。自动重试耗尽后进入死信队列，授权操作员核查后手工重放；手工重放不会改变原事件归属，也不能恢复已撤销的订阅授权。"),
        ("4 时间与日志", "日志保留事件编号、尝试次数、状态码、耗时及脱敏错误摘要，不记录签名密钥或完整客户正文。事件时间均标明时区；无法确认的接收方处理结果记为待核实，不把网络超时直接当作业务未执行。"),
        ("5 故障边界", "Webhook回调重试是平台向客户回调的机制，客户调用开放API时的HTTP 429处理是另一条链路。INC-2026-1002的重复报表提交应参照技术部复盘，不应直接套用Webhook重试次数解释其根因。"),
    ], [("Webhook默认最多投递几次，去重记录保留多久？", "最多6次，包含首次及追加5次重试；去重记录保留7日。", ["6次", "5次", "7日"])], ["错误码", "重试边界", "幂等编号"]),
]


def metadata(group, owner):
    return f"归口部门：{owner}；入库范围：{group}；可见范围：{GROUPS[group]['access']}。"


def make_department_pdf(path, title, code, sections):
    """Readable 11pt body with tighter spacing to avoid one-paragraph final pages."""
    compact = code.startswith("保留原制度编号")
    def p(text, kind="body"):
        result = paragraph(text, kind)
        result.style.spaceAfter = 4 if compact else 6
        if kind == "body":
            result.style.leading = 16 if compact else 17
        if kind == "heading":
            result.style.spaceBefore = 8 if compact else 10
            if compact:
                result.style.fontSize = 13
                result.style.leading = 20
        return result
    story = [p(title, "title")]
    if not compact:
        story.append(p(COMPANY))
    story += [p(f"文档编号：{code} | 状态：现行", "small"), p(NOTICE, "small")]
    for heading, content in sections:
        if isinstance(content, list) and content and isinstance(content[0], list):
            story.append(CondPageBreak(110 if compact else 140))
            if heading:
                heading_p = p(heading, "heading")
                heading_p.keepWithNext = False
                story.append(heading_p)
            table = pdf_table(content)
            if compact:
                table.setStyle(TableStyle([
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]))
            story.append(table)
        else:
            if heading:
                story.append(p(heading, "heading"))
            story += [p(str(text)) for text in (content if isinstance(content, list) else [content])]
    SimpleDocTemplate(str(path), pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN,
                      rightMargin=MARGIN, topMargin=52, bottomMargin=48,
                      title=title, author=COMPANY, subject=NOTICE).build(
                          story, onFirstPage=page_furniture, onLaterPages=page_furniture)


def word_to_sections(path, owner):
    blocks = list(iter_blocks(Document(path)))
    title = blocks[0].text
    sections = []
    heading, content = "适用说明", []
    for block in blocks[1:]:
        if isinstance(block, WordTable):
            if content:
                sections.append((heading, content))
                content = []
            rows = [[cell.text for cell in row.cells] for row in block.rows]
            for row in rows:
                if row[0] == "归口部门":
                    row[1] = owner
                if path.name.startswith("03_") and "密级" in row:
                    row[row.index("密级") + 1] = "内部使用"
            sections.append(("文档信息" if rows[0][0] == "文档编号" else heading + "记录", rows))
        elif block.text.strip():
            text = (block.text.strip().replace("财务部", "财务支持组")
                    .replace("运营管理部", "人力资源部采购支持组").replace("法务部", "合同审查负责人"))
            if text in (COMPANY, "企业 RAG 测试语料") or text.startswith("文件说明：模拟企业文件"):
                continue
            if re.match(r"^\d+(?:\.\d+)*\s+", text) and len(text) < 65:
                if content:
                    sections.append((heading, content))
                heading, content = text, []
            else:
                content.append(text)
    if content:
        sections.append((heading, content))
    # A Word section may have prose, a table, and more prose. Keep its heading
    # once instead of inventing duplicate numbered headings around the table.
    previous_heading = None
    normalized = []
    for section_heading, section_content in sections:
        base_heading = section_heading.removesuffix("记录")
        normalized.append((None if base_heading == previous_heading else base_heading, section_content))
        previous_heading = base_heading
    return title, normalized


def to_text(spec, group, markdown):
    prefix = "# " if markdown else ""
    lines = [prefix + spec["title"], "", COMPANY, "", metadata(group, group),
             f"文档编号：{spec['code']}；版本：1.0；生效日期：2026年10月1日；状态：现行。", "", NOTICE, ""]
    for heading, content in spec["sections"]:
        lines += [("## " if markdown else "") + heading, ""]
        if isinstance(content, list) and content and isinstance(content[0], list):
            lines.append("| " + " | ".join(content[0]) + " |")
            lines.append("| " + " | ".join(["---"] * len(content[0])) + " |")
            lines += ["| " + " | ".join(row) + " |" for row in content[1:]]
        else:
            lines += content if isinstance(content, list) else [content]
        lines.append("")
    return "\n".join(lines)


def text_from(path):
    if path.suffix == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_text(encoding="utf-8")


def build(output, render=False):
    # New edition only; refuse overwrite so a reviewed pack cannot silently change.
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(f"Choose a new empty output folder: {output}")
    if not FONT_PATH.is_file():
        raise FileNotFoundError(FONT_PATH)
    original = json.loads((SOURCE / "manifest.json").read_text(encoding="utf-8"))
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH), subfontIndex=0))
    output.mkdir(parents=True, exist_ok=True)
    evaluation = output / "evaluation"
    evaluation.mkdir()
    records, texts, by_old_name = [], {}, {}

    def register(path, group, owner, features, origin, status="current", source_text=None):
        text = text_from(path)
        scan = "OCR必需" in features
        texts[path.name] = source_text if scan else text
        record = dict(file=path.relative_to(output).as_posix(), format=path.suffix[1:],
                      group=group, owner_department=owner, target_knowledge_base=GROUPS[group]["knowledge_base"],
                      visibility=GROUPS[group]["access"], status=status, features=features,
                      origin=origin, bytes=path.stat().st_size,
                      sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                      text_characters=len(text), source_characters=len(source_text or text))
        if path.suffix == ".pdf":
            record.update(pages=len(PdfReader(path).pages), embedded_text_characters=len(text))
        records.append(record)

    scan_truth = json.loads((SOURCE / "evaluation/scan_ground_truth.json").read_text(encoding="utf-8"))
    for old in original["documents"]:
        source = SOURCE / old["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != old["sha256"]:
            raise ValueError(f"Original pack was changed: {source}")
        number = int(source.name.split("_", 1)[0])
        group, owner = OLD_ROUTING[number]
        folder = output / "documents" / GROUPS[group]["folder"]
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (source.stem + ".pdf" if source.suffix == ".docx" else source.name)
        by_old_name[source.name] = target
        features = list(old["features"]) + ["部门目录", "入库路由清单"]
        if source.suffix == ".docx":
            title, sections = word_to_sections(source, owner)
            make_department_pdf(target, title, "保留原制度编号与版本 | " + metadata(group, owner), sections)
            features = ["原Word内容重新排版为PDF", "标题层级", "原生表格", "部门目录"]
        elif number == 7:
            make_pdf(target, "公司组织与产品服务说明", "XH-ORG-2026-01 | 部门版2.0 | 2026年10月1日生效", [
                ("1 公司与资料范围", "星海科技有限公司为虚构企业软件服务公司，模拟规模180人，提供星海协作云、开放平台及实施服务。公司公共知识库是全体已授权员工的资料范围，不是第四个部门。产品包含审批、报表和权限模块，不提供薪资计算服务。"),
                ("2 部门与责任", [["部门", "人数", "主要责任"],
                    ["人力资源部", "24", "人事、培训及财务采购支持"],
                    ["技术部", "92", "项目交付、客户支持与信息安全"],
                    ["平台研发部", "64", "产品研发、平台可靠性与API版本维护"]]),
                ("3 历史团队称谓", "旧流程中的项目交付部和客户服务部在本部门版中分别对应技术部项目交付组和客户支持组；综合管理部的人事、财务与采购职责归入人力资源部支持组。系统台账中的历史归口名称按此映射理解，不代表需额外创建这些部门。"),
                ("4 公共与内部范围", "公共资料仅收录组织说明、面向员工的制度、公开流程和FAQ。人事操作、绩效复核、调动离职、客户事件及研发配置分别进入对应部门知识库。公司公共权限不等于可以读取所有部门资料，也不能替代文档外发审批。"),
                ("5 产品与版本", "协作云产品版本2.6.0与开放API主版本/v1是不同维度。当前制度和历史归档文档按各自生效日期判断，项目签署范围以最终验收与批准变更为准。本文组织口径替代旧20份包中的组织说明，不改变既有工单事实。"),
            ])
            features += ["组织口径更新", "公共范围与部门分离"]
        elif source.suffix in (".md", ".txt"):
            lines = source.read_text(encoding="utf-8").splitlines()
            lines[1:1] = ["", metadata(group, owner)]
            target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            shutil.copy2(source, target)
        register(target, group, owner, features, source.relative_to(ROOT).as_posix(), old["status"],
                 scan_truth["text"] if "OCR必需" in features else None)

    new_paths = {}
    for spec in NEW_DOCUMENTS:
        group = spec["group"]
        path = output / "documents" / GROUPS[group]["folder"] / f"{spec['number']:02d}_{spec['title']}.{spec['extension']}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if spec["extension"] == "pdf":
            code = f"{spec['code']} | 版本1.0 | 2026年10月1日生效 | " + metadata(group, group)
            make_department_pdf(path, spec["title"], code, spec["sections"])
        else:
            path.write_text(to_text(spec, group, spec["extension"] == "md"), encoding="utf-8")
        register(path, group, group, ["部门内部"] + spec["features"], "offline_authored_department_scenarios")
        new_paths[spec["number"]] = path

    counts = dict(Counter(record["format"] for record in records))
    group_counts = dict(Counter(record["group"] for record in records))
    assert counts == {"pdf": 18, "md": 8, "txt": 4}, counts
    assert group_counts == {"人力资源部": 7, "公司公共": 6, "技术部": 8, "平台研发部": 9}, group_counts
    assert len(records) == len(set(record["sha256"] for record in records)) == 30

    cases = [json.loads(line) for line in (SOURCE / "evaluation/questions.jsonl").read_text(encoding="utf-8").splitlines() if line]
    for case in cases:
        for evidence in case["expected_evidence"]:
            if evidence["document"].startswith("07_") and evidence["text"] == "组织规模为180人":
                evidence["text"] = "模拟规模180人"
            evidence["document"] = by_old_name[evidence["document"]].name
        case["source_groups"] = sorted({record["group"] for record in records
                                      if Path(record["file"]).name in {item["document"] for item in case["expected_evidence"]}})
    for spec in NEW_DOCUMENTS:
        for question, answer, facts in spec["facts"]:
            cases.append(dict(id=f"XH-D-{spec['number']:02d}", question=question,
                              reference_answer=answer, answerable=True, required_facts=facts,
                              expected_evidence=[dict(document=new_paths[spec["number"]].name, text=facts[0])],
                              category="部门资料事实与边界", difficulty="medium", source_groups=[spec["group"]]))
    (evaluation / "questions.jsonl").write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases), encoding="utf-8")
    conversations = json.loads((SOURCE / "evaluation/conversation_cases.json").read_text(encoding="utf-8"))
    for case in conversations:
        case["sources"] = [by_old_name[name].name for name in case["sources"]]
    conversations += [
        dict(id="XH-D-MT-04", source_groups=["人力资源部", "公司公共"], sources=[new_paths[23].name, by_old_name["02_差旅与费用报销管理制度.docx"].name], turns=[
            dict(question="2000元的外部课程由谁审批？", expected_answer="直属经理批准并由HR登记。"),
            dict(question="培训结束后多久提交总结？", expected_answer="10个自然日内。"),
            dict(question="如果同时有出差费用，回来后多久报销？", expected_answer="行程结束或费用发生后10个自然日内，按现行报销制度。")]),
        dict(id="XH-D-MT-05", source_groups=["技术部", "平台研发部"], sources=[new_paths[27].name, new_paths[30].name], turns=[
            dict(question="INC-2026-1002首次响应耗时多久？", expected_answer="13分钟。"),
            dict(question="是Webhook的重试问题吗？", expected_answer="不是该链路；已确认是客户报表调用重试未复用幂等键。"),
            dict(question="那Webhook默认最多会投递几次？", expected_answer="最多6次，含首次投递和5次自动重试。")]),
    ]
    dump_json(evaluation / "conversation_cases.json", conversations)
    scan_truth["document"] = by_old_name[scan_truth["document"]].name
    dump_json(evaluation / "scan_ground_truth.json", scan_truth)
    dump_json(evaluation / "permission_cases.json", {
        "purpose": "权限测试定义，不代表已创建用户或执行问答；缺少授权证据时应拒答，不泄漏被过滤文档。",
        "profiles": {
            "仅公共测试用户": ["公司公共"], "HR测试用户": ["公司公共", "人力资源部"],
            "技术测试用户": ["公司公共", "技术部"], "研发测试用户": ["公司公共", "平台研发部"],
            "跨库测试用户": ["公司公共", "技术部", "平台研发部"], "全范围测试用户": list(GROUPS),
        },
        "cases": [
            dict(id="ACL-01", profile="仅公共测试用户", question="现在普通员工去上海住宿上限多少？", expected="可回答650元", source_groups=["公司公共"]),
            dict(id="ACL-02", profile="仅公共测试用户", question="季度绩效复核需要几天组织？", expected="资料不足，不泄漏HR流程内容或标题", blocked_groups=["人力资源部"]),
            dict(id="ACL-03", profile="HR测试用户", question="绩效复核申请多久提交？", expected="收到书面反馈后5个工作日内", source_groups=["人力资源部"]),
            dict(id="ACL-04", profile="HR测试用户", question="INC-2026-1002首次响应用了多久？", expected="资料不足，不引用技术部复盘", blocked_groups=["技术部"]),
            dict(id="ACL-05", profile="研发测试用户", question="Webhook默认最多投递多少次？", expected="6次，含首次", source_groups=["平台研发部"]),
            dict(id="ACL-06", profile="技术测试用户", question="Webhook去重记录保留几日？", expected="资料不足，不泄漏研发内部规范", blocked_groups=["平台研发部"]),
            dict(id="ACL-07", profile="跨库测试用户", question="INC-2026-1002首次响应耗时多久，Webhook最多投递几次？", expected="13分钟及6次，分别引用技术部与平台研发部", source_groups=["技术部", "平台研发部"]),
            dict(id="ACL-08", profile="跨库测试用户", operation="撤销技术部授权后恢复此前引用技术复盘的会话", expected="隐藏可能涉及已撤权范围的历史；新追问不再使用技术资料", blocked_groups=["技术部"]),
        ],
    })
    manifest = dict(company=COMPANY, fictional=True, edition="department-offline-v2", total_documents=30,
                    counts=counts, group_counts=group_counts, groups=GROUPS,
                    source_characters=sum(record["source_characters"] for record in records),
                    external_model_calls=0, automatically_ingested=False, new_documents=10,
                    reused_or_adapted_documents=20, single_turn_questions=len(cases),
                    multi_turn_conversations=len(conversations), permission_cases=8,
                    scale_note="30份功能与权限测试文档，不代表中小企业真实数据量或检索质量达标。",
                    documents=sorted(records, key=lambda row: row["file"]))
    dump_json(output / "manifest.json", manifest)
    with (output / "入库清单.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["相对路径", "资料分组", "归口部门", "建议目标知识库", "可见范围", "状态", "格式", "SHA256"])
        for row in manifest["documents"]:
            writer.writerow([row[key] for key in ("file", "group", "owner_department", "target_knowledge_base", "visibility", "status", "format", "sha256")])
    write_readme(output, manifest)
    if render:
        poppler = Path("C:/Users/Gabriel/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe")
        scratch = ROOT / "tmp/xinghai-department-corpus-qa-v2"
        for row in records:
            if row["format"] != "pdf":
                continue
            target = scratch / Path(row["file"]).stem
            target.mkdir(parents=True, exist_ok=True)
            subprocess.run([str(poppler), "-r", "120", "-png", str(output / row["file"]), str(target / "page")], check=True)
    dump_json(evaluation / "generation_checks.json", dict(document_counts="passed", source_hashes="passed",
             unique_output_hashes="passed", department_counts="passed", pdf_visual_review="pending",
             actual_ocr="pending", parser_validation="pending", external_model_calls=0,
             word_rendering="Not required: user chose PDF, Markdown and TXT; no DOCX output."))
    package(output)
    print(json.dumps({key: manifest[key] for key in ("total_documents", "counts", "group_counts", "source_characters", "single_turn_questions")}))


def write_readme(output, manifest):
    rows = "\n".join(f"| {group} | {manifest['group_counts'][group]} | {value['knowledge_base']} | {value['access']} |"
                     for group, value in GROUPS.items())
    inventory = "\n".join(f"| {Path(row['file']).name} | {row['group']} | {row['owner_department']} | {row['status']} |"
                           for row in manifest["documents"])
    text = f"""# 星海科技有限公司部门版知识库文档包

本包包含30份虚构文档，18份PDF、8份Markdown、4份TXT。复用或改编原包20份资料，新增10份部门业务资料；原包保持不变。本包用于检索、切分、多轮问答及权限测试，不具有实际管理或法律效力，不是企业真实规模压测集。未自动上传、生成向量或调用外部模型。

## 部门与入库范围

| 分组 | 文档数 | 建议目标知识库 | 可见范围 |
| --- | --- | --- | --- |
{rows}

公共库是可见范围，不是部门。公共制度的维护部门仍写在入库清单中。财务与采购仅提供面向员工的公开流程，不包含账务明细、薪酬、供应商报价或真实合同；内部人事操作、客户事件和研发资料分开存放。

部门版采用三个部门的组织口径。旧资料中的项目交付部、客户服务部对应技术部内部工作组，综合管理部的人事财务采购职责对应人力资源部支持组；以07号组织说明的映射为准，不要求新增这些历史称谓的数据库部门。所有人数、规则和业务事件均为虚构设定。

## 上传操作

1. 只上传 `documents/` 下各分组内的业务文件；不要上传本说明、CSV、manifest或evaluation，避免参考答案泄漏。
2. 登录前端，选择与分组匹配的单个知识库，再上传该目录内文件。实际知识库名字以平台显示为准，CSV提供建议映射。
3. 全部可访问模式用于问答，不是上传目标。不能把部门文件统一上传公司公共库，目录名也不会自动创建权限。
4. 新包替代原20份包用于测试，不要把两个包一起上传同一范围，否则会形成近重复文档。既有原包已入库时，应先核对编号与版本，是否替换由管理员决定；本次不删除任何已有资料。
5. 08号2025差旅标准为归档版，用于历史版本区分；基础测试可先暂不上传，涉及当前报销须引用现行制度。
6. 12号维护通知为图片型PDF，必须使用真实中文OCR。缺少OCR时明确失败，不能把 `scan_ground_truth.json` 当作识别结果入库。
7. 等待索引完成并确认ready，再执行授权范围问答测试。文件上传成功不等于已可检索。

## 测试资料

- `evaluation/questions.jsonl`：45道单轮测试题，含参考答案、证据和来源分组；需具备对应权限才能回答。
- `evaluation/conversation_cases.json`：5组多轮案例，共15轮，含HR与公共资料、技术与研发资料跨库追问。
- `evaluation/permission_cases.json`：8项权限测试与建议测试角色，不代表已经创建真实账号或授予权限。
- `evaluation/scan_ground_truth.json`：独立的扫描原文，仅用于对照真实OCR。
- `evaluation/generation_checks.json`及后续切分检查：记录文件与解析检查，不是检索召回率或回答准确率。

测试角色只是方案；请根据实际用户的有效授权验证。不选择部门也只能读取后端计算出的授权知识库集合，不能把全部可访问理解为全部公司资料。

## 文件清单

| 文件 | 分组 | 归口部门 | 状态 |
| --- | --- | --- | --- |
{inventory}

## 验收与限制

文档包含跨页条件、长表格、双栏布局、扫描OCR、编号查询、历史版本、事件状态和跨库关联。PDF视觉检查、实际OCR和项目切分检查结果见evaluation中的记录；未运行外部模型回答质量评估。

按照用户选择，本次不新增DOCX。原包6份Word的内容重新排版为PDF，并检查PDF页图；这些PDF不是Word实际分页的证明。新部门版没有覆盖真实劳动规则、薪酬或生产服务承诺，应由实际企业的专业负责人审核后才可作为制度。

生成工具可运行 `scripts/generate_xinghai_department_corpus.py --output <新的空目录> --render`，需使用带python-docx、ReportLab、pypdf的文档运行时和本地微软雅黑字体。生成器拒绝覆盖已存在的非空目录，预览页图不进入压缩包。旧工具和旧20份包均保留。
"""
    (output / "README.md").write_text(text, encoding="utf-8")


def package(output):
    archive = output.parent / f"{COMPANY}_部门知识库30份_v2.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(output.rglob("*")):
            if path.is_file():
                bundle.write(path, Path(output.name) / path.relative_to(output))
    return archive


def refresh_converted_pdfs(output):
    """Repair only owned generated PDFs; preserve copied PDFs and original inputs."""
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["edition"] == "department-offline-v2"
    assert manifest["total_documents"] == 30
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH), subfontIndex=0))
    for row in manifest["documents"]:
        word_source = row["origin"].endswith(".docx")
        new_pdf = row["origin"] == "offline_authored_department_scenarios" and row["format"] == "pdf"
        if not word_source and not new_pdf:
            continue
        path = output / row["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], path
        if word_source:
            source = ROOT / row["origin"]
            title, sections = word_to_sections(source, row["owner_department"])
            make_department_pdf(path, title, "保留原制度编号与版本 | " + metadata(row["group"], row["owner_department"]), sections)
        else:
            number = int(path.name.split("_", 1)[0])
            spec = next(spec for spec in NEW_DOCUMENTS if spec["number"] == number)
            code = f"{spec['code']} | 版本1.0 | 2026年10月1日生效 | " + metadata(row["group"], row["owner_department"])
            make_department_pdf(path, spec["title"], code, spec["sections"])
        text = text_from(path)
        row.update(bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                   pages=len(PdfReader(path).pages), text_characters=len(text),
                   embedded_text_characters=len(text), source_characters=len(text))
        scratch = ROOT / "tmp/xinghai-department-corpus-qa-v2" / path.stem
        poppler = "C:/Users/Gabriel/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe"
        subprocess.run([poppler, "-r", "120", "-png", str(path), str(scratch / "page")], check=True)
    manifest["source_characters"] = sum(row["source_characters"] for row in manifest["documents"])
    dump_json(output / "manifest.json", manifest)
    with (output / "入库清单.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["相对路径", "资料分组", "归口部门", "建议目标知识库", "可见范围", "状态", "格式", "SHA256"])
        for row in manifest["documents"]:
            writer.writerow([row[key] for key in ("file", "group", "owner_department", "target_knowledge_base", "visibility", "status", "format", "sha256")])
    write_readme(output, manifest)
    checks_path = output / "evaluation/generation_checks.json"
    if checks_path.exists():
        checks = json.loads(checks_path.read_text(encoding="utf-8"))
        checks.update(pdf_visual_review="pending_after_refresh", parser_validation="pending_after_refresh")
        dump_json(checks_path, checks)
    print(json.dumps({"refreshed_generated_pdfs": 10, "source_characters": manifest["source_characters"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--package-only", action="store_true", help="Package existing files and their current validation reports")
    parser.add_argument("--refresh-generated-pdfs", "--refresh-converted-pdfs", dest="refresh_converted_pdfs", action="store_true", help="Refresh only owned generated PDFs, preserving original source files")
    args = parser.parse_args()
    if args.package_only:
        print(json.dumps({"archive": str(package(args.output.resolve()))}))
    elif args.refresh_converted_pdfs:
        refresh_converted_pdfs(args.output.resolve())
    else:
        build(args.output.resolve(), args.render)
