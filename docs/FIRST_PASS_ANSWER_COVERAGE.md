# 首次回答完整性：低成本落地版

2026-10-06补充：已增加授权表格细条件清单、程序计算纠正、保守重复补全检测及r6评分修订，当前验证和复测说明见 [回答质量优化说明](ANSWER_QUALITY_REFINEMENT.md)。下文保留首版实现记录，不代表新版本实时模型指标。

## 目标与实际范围

在已有的一次回答生成调用内，先列出用户要求回答的要点，再输出关联的结论。优先保证有依据和完整，最后精简；旧答案补全保留为过渡兜底。

本版本不增加规划模型、裁判模型、自动修复生成或多查询检索循环。原有多轮追问改写调用保持不变；单轮仍一次生成，多轮原本有的改写成本不消失。上下文、候选和权限范围不扩大，不新增迁移，不需重新入库。

缺证据时保留整体拒答，不推出新的“部分回答”接口。自动定向补检索不是本版本的默认功能：只有评估显示证据缺口是主要问题，再针对具体缺口引入有预算上限的补检索。不能为了声称答案完整而猜测或默认多调用模型。

## 链路

1. 原有授权检索、重排、上下文选择和版本适用性判断。
2. 本地从用户问题中的问号、分号和换行建立Q1、Q2等原文锚点。不读题集、不读标准答案，最多8段；多出的尾部合并，不截掉问题。
3. 单次模型生成：coverage在结构化输出Schema中位于claims之前，鼓励先识别每段的必答要点。一个原文段可以展开多个语义要点；一条结论可以关联多个要点。
4. 程序检查问题编号覆盖、结论序号、状态一致性；继续执行既有引用、数字和受限计算校验。
5. 记录补全前的有效答案，必要时执行已有表格条件、阶段预算、事件根因补全。
6. 再校验最终答案、渲染原有API结果。coverage不出现在前端/API响应和业务对话存储中，仅在显式评估过程记录中保存。

程序不会独立理解所有并列语义。问号分段只是锚点，不是完整语义规划器；Schema字段顺序也只是生成约束，不保证模型执行顺序。模型还可能把一个要点标为answered但实际答非所问，因此“结构完整”不能当成“语义完整”。

## 内部结构

新增GenerationAnswerDraft用于真实生成，旧AnswerDraft保留以兼容原单元工具和旧数据。生成结构必须有coverage，不能悄悄省略该字段继续当成成功。

```json
{
  "coverage": [
    {"requirement_id": "Q1", "aspect": "审批角色", "status": "answered", "claim_indices": [1], "missing_information": null},
    {"requirement_id": "Q2", "aspect": "紧急情形的要求", "status": "answered", "claim_indices": [2], "missing_information": null}
  ],
  "answerable": true,
  "claims": [
    {"text": "这里必须是实际资料支持的审批结论", "citations": ["资料1"], "calculation": null},
    {"text": "这里必须是实际资料支持的紧急情形处理要求", "citations": ["资料1"], "calculation": null}
  ],
  "refusal_reason": null
}
```

claim_indices从1开始，必须是严格正整数，不能是true或字符串。相同编号下不能出现空白或重复要点；结论关联不能为空、越界或重复。

有必要要点证据不足时，answerable=false、claims=[]：该要点status=insufficient；其他因整体拒答没有输出的要点可标withheld，注明未输出原因。可回答时所有要点必须answered。漏编号、未回答项与answerable冲突等会拒答，不允许旧补全掩盖结构缺口。

编号齐全、引用存在、数字能对上，并不独立证明每句话与证据的语义关系成立。复杂归因、否定和数值对象绑定仍需人工审查。

## 具体文件

- backend/app/knowledge/answer_coverage.py：原文锚点、提示清单、结构校验和诊断快照。
- backend/app/knowledge/answer_models.py：新增生成专用coverage结构，不改变公开AnswerResult。
- backend/app/knowledge/prompts.py：先覆盖再精简，逐要点作答，不以指路或背景代替答案。
- backend/app/knowledge/answer_service.py、answer_validation.py：接入必答项验证、首次回答和最终补全分开记录；保持一次生成。
- backend/app/evaluation/trace.py、metrics.py、runner.py、reporting.py：首次结果评分及补全诊断，报告明确缺少观察的情况。
- backend/app/evaluation/conversation_runner.py、http_runner.py：本地多轮能记录首次生成，纯HTTP多轮不暴露内部数据，显示未观察。
- evals/first_pass_generalization：8道新组合问法；同一语料的新问法回归，不是独立语料盲测。

## 新指标怎么读

report.md的answer层和本地conversations层新增“首次生成与补全诊断”，report.json的generation_metrics给出汇总，单题generation中有首次回答、缺失事实及验证错误。

| 字段 | 含义与分母 |
|---|---|
| observed_cases / unobserved_cases | 观察到首次生成的数量 / 未观察数量 |
| first_pass_proxy_pass_rate | 首次有效回答通过现有事实、来源、引用代理检查的比例；分母为观察到的生成，包括正例和负例 |
| first_pass_fact_complete_rate | 首次有效回答可回答且必要事实全部出现的比例；只计算有事实标注的正例 |
| fact_labelled_positive_cases | 上述事实完整率的分母 |
| completion_change_rate | 旧补全追加或改变拒答状态的比例；分母为观察到的生成 |
| supplement_append_rate | 旧补全实际追加结论的比例；分母为观察到的生成 |

补全前答案已经做了必要的程序计算渲染和证据/结构校验；错误计算不能凭模型提出的数字得到高分。最后的answer通过率仍按最终回答计算，与上述指标分别展示。最终通过但首次漏答的题会在Markdown中列出。

没发生生成的空上下文/未来规则预拒答、运行错误、历史trace和纯HTTP多轮内部结果都不凭空计为首次生成通过。没有可观察样本时比例为null。新增指标不能从旧报告追溯重建。

append rate是兜底触发频率，不是漏答率：已有预算/根因补全含字面匹配，即使原答案语义正确，改写措辞也可能触发原文追加。不要单独以触发率判断质量或立即删除兜底。

## 复测

在项目根目录运行，使用获准身份的有效JWT；该环境变量只作用于设置它的终端，不能把星号或截断令牌当成JWT。本次实施没有获取或伪造业务身份令牌。

```powershell
$env:REDIS_URL="redis://127.0.0.1:6379/0"
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --layers retrieval,rerank,context,answer --allow-model-calls --save-traces
```

新组合题：

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --dataset-dir evals/first_pass_generalization --layers retrieval,rerank,context,answer --allow-model-calls --save-traces
```

本地多轮诊断（临时创建并删除评估自己创建的会话，不删除用户会话）：

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --layers conversations --conversation-backend local --allow-model-calls --allow-conversation-writes --save-traces
```

默认HTTP多轮评估仍可以使用，但拿不到内部首次回答，因此不能计算上述首次生成指标。不要拿本地模式的时延当作HTTP性能。

报告默认位于evals/reports/system/<UTC时间戳>/。新题集与原60题不同口径，不能直接比较总分。应先在相同旧题集比较最终质量，再用新问法抽检；后续再由业务人员提供未参与开发的新文档和盲测题。

## 成本与验收边界

没有默认新增模型请求；新增清单、Schema和coverage会增加输入和输出token，实际费用取决于模型及问题复杂度，不能说完全免费。程序验证、记录和既有补全本身不调用模型。

单元测试验证结构契约、一次生成调用、权限过滤、引用、计算、补全前后隔离和报告兼容性，不证明真实模型首次完整率提高。本次环境没有可用评估JWT，未自动运行付费全题集；需要按上面命令复测后才能报告量化质量变化。

本次隔离回归：580 passed、22 deselected，2条既有依赖弃用提醒。22项真实数据库/外部接口集成测试未执行，不能视为通过。代码检查无空白错误；旧trace回放已确认首次生成无观察时显示0个观察、比例null。回放诊断移至evals/reports/system/diagnostics/first-pass-legacy-replay-20261006，未改写原报告，也不是新模型质量跑分。

API和worker已构建并更新；保留所有数据卷。核心问答文件哈希与运行容器一致，就绪检查数据库和Redis均up。未修改迁移、业务权限或语料内容，未提交/推送GitHub。
