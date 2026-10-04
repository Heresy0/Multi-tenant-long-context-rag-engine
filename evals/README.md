# RAG 检索评估

本目录用于保存企业知识库检索层的评估数据、原始报告和实验结论。当前评估衡量证据召回、排序质量、分阶段延迟，以及基于 Top-1 重排分数的检索层无答案判断。该判断不等同于大模型最终答案的正确性、忠实度或真实拒答行为。

## 目录结构

```text
evals/
├── datasets/
│   ├── retrieval_dev.jsonl
│   ├── retrieval_test.jsonl
│   ├── answer_dev.jsonl
│   └── answer_test.jsonl
├── reports/
│   ├── retrieval_baseline.json
│   ├── retrieval_hybrid_v1.json
│   ├── retrieval_hybrid_v2.json
│   ├── retrieval_rerank_v1.json
│   ├── retrieval_rerank_v2.json
│   ├── retrieval_test_rerank_v1.json
│   └── retrieval_test_rerank_fetch30_v1.json
└── README.md
```

## 开发集

`retrieval_dev.jsonl` 当前包含 48 条可回答问题，8 份模拟企业文档各 6 题。题目覆盖制度数值、时间限制、审批条件、例外规则、项目流程、合同条款、API 参数和 FAQ。

每条样本至少标注一个来源文件和原文证据。评估时要求检索结果来源文件一致，并且分块包含对应证据文本。

当前开发集没有无答案问题，因此报告中的 `unanswerable` 为 0，现阶段指标不能反映系统的拒答能力。

## 独立测试集

`retrieval_test.jsonl` 包含 32 条未参与检索参数调整的问题，其中 24 条可回答、8 条无答案。可回答问题覆盖 8 份企业文档，并与开发集没有重复问题；无答案问题包含远程办公天数、密码长度、API 超时和合同管辖地等语义相关但语料未明确给出的信息。

## 答案级评估集

`answer_dev.jsonl` 和 `answer_test.jsonl` 各包含 32 条问题，其中均为 24 条可回答、8 条无答案。可回答问题在 HR、财务、安全、采购、项目、技术、合同和 FAQ 八个类别中各 3 条。两个集合的问题、样本 ID 完全隔离。

- `answer_dev.jsonl`：用于开发答案提示词、引用策略和自动评分规则。
- `answer_test.jsonl`：作为独立测试集，不应用于调整提示词或评分阈值。

每条样本包含：

- `reference_answer`：人工编写的参考答案，不要求模型逐字一致。
- `required_facts`：答案必须覆盖的最小事实短语，用于事实覆盖率评估。
- `expected_evidence`：正确来源文件及原文证据，用于引用来源和证据准确率评估。
- `answerable`：知识库是否足以回答，用于计算回答接受率和无答案拒绝率。
- `category`、`difficulty`：用于按业务类别和难度分组分析。

答案评估不应只计算字符串完全匹配。建议至少报告：回答判断准确率、关键事实覆盖率、引用来源准确率、无答案拒绝率、错误接受率和端到端延迟。

`required_facts` 应拆分为可以独立匹配的原子事实。例如，把“按 event_id 去重”标注为 `event_id` 和“去重”，避免正确答案仅因词序不同而被误判。

### 运行答案评估

先启动 FastAPI 服务，并把当前用户的访问令牌放入环境变量。令牌不会写入报告：

```powershell
$env:ENTERPRISE_KB_ACCESS_TOKEN = $token
```

一次评估固定在一个知识库授权范围内。技术部知识库首次运行可以只执行 1 条技术类问题进行冒烟测试：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_answers.py `
  --dataset evals\datasets\answer_dev.jsonl `
  --knowledge-base-id "8e4af664-22c1-480e-9393-36ac401da0f5" `
  --category technical `
  --limit 1 `
  --output evals\reports\answer_dev_technical_smoke.json
```

冒烟测试通过后可以运行该知识库全部技术类开发题：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_answers.py `
  --dataset evals\datasets\answer_dev.jsonl `
  --knowledge-base-id "8e4af664-22c1-480e-9393-36ac401da0f5" `
  --category technical `
  --category unanswerable `
  --timeout 120 `
  --output evals\reports\answer_dev_technical_v1.json
```

开发策略和评分规则稳定后，再运行相同分类的独立测试集：

```powershell
.\.venv\Scripts\python.exe scripts\evaluate_answers.py `
  --dataset evals\datasets\answer_test.jsonl `
  --knowledge-base-id "8e4af664-22c1-480e-9393-36ac401da0f5" `
  --category technical `
  --category unanswerable `
  --timeout 120 `
  --output evals\reports\answer_test_technical_v1.json

Remove-Item Env:ENTERPRISE_KB_ACCESS_TOKEN
```

`--category` 可以重复使用，但所有选中题目都必须属于本次指定知识库能够访问的语料。当前 Keycloak 访问令牌有效期较短，长评估应按知识库和分类分批执行并在每批开始前更新令牌。

答案报告包括：

- `answerability_accuracy`：可回答与不可回答判断的整体准确率。
- `answerable_accept_rate`：可回答问题被系统接受回答的比例。
- `unanswerable_rejection_rate`：无答案问题被正确拒答的比例。
- `required_fact_coverage`：参考关键事实被答案覆盖的微平均比例。
- `fact_complete_rate`：关键事实全部覆盖的可回答问题比例。
- `citation_source_precision`：返回的引用来源中正确来源的比例。
- `citation_source_recall`：期望引用来源中实际被引用的比例。
- `citation_case_hit_rate`：至少命中一个正确来源的可回答问题比例。
- `mean/p50/p95_latency_ms`：包含检索、重排序、生成和校验的端到端延迟。
- `stage_latency`：服务端各阶段的样本数、平均耗时、P50 和 P95，包含混合检索、重排序、检索总耗时、上下文构建、模型生成、结果校验、答案渲染和服务端总耗时。

其中，顶层 `latency_ms` 是评估客户端观察到的完整 HTTP 请求耗时；`stage_latency.total_ms` 是服务端 `AnswerService` 内部耗时。两者的差值主要包含 HTTP 传输、FastAPI 请求解析和响应序列化等开销。没有执行的阶段使用 `null`，例如检索不到资料时不会产生模型生成和校验耗时。

关键事实覆盖使用 NFKC 归一化后的短语匹配，适合作为可重复的自动基线，但不等同于语义正确性或忠实度。复杂改写和跨句推理仍需人工抽检或单独的评审模型。

## pgvector 答案层独立测试结果

2026-09-29 在技术部知识库上运行 `answer_test.jsonl` 的 3 条技术题和 8 条无答案题。11 次请求全部成功，可回答问题全部接受，无答案问题全部拒绝，且引用均命中预期技术手册。

| 指标 | 结果 |
|---|---:|
| 请求成功率 | 11/11（100%） |
| 回答判断准确率 | 100% |
| 可回答接受率 | 100% |
| 无答案拒绝率 | 100% |
| 引用来源准确率 / 召回率 | 100% / 100% |
| 自动短语事实覆盖率 | 92.86% |
| 人工复核事实覆盖率 | 100% |
| 平均服务端总耗时 | 8516.80ms |
| P95 服务端总耗时 | 13478.56ms |

自动评分唯一未命中的事实来自 `ans-test-tech-003`：参考短语为“不得超过5分钟”，实际答案为“超过5分钟的请求应被拒绝，即允许最多相差5分钟”。两者语义一致，属于精确短语匹配的假阴性，而不是检索或回答错误。人工复核后，3 条可回答问题均完整覆盖关键事实。数据集已将该项改为原子事实“5分钟”，后续重复评估不会再受否定句式变化影响。

延迟主要来自答案生成：平均生成耗时 7402.71ms，占平均服务端总耗时约 86.9%；检索与重排序合计平均 1113.87ms。因此后续性能优化应优先关注生成模型和输出长度，而不是降低 pgvector 召回候选数。

原始报告：[技术部知识库独立答案测试](reports/answer_test_technical_v1.json)。

## 当前分层评估入口

新增 `scripts/evaluate_system.py`，支持离线解析/隔离权限回归、真实授权后端分层评估、真实HTTP多轮/部分权限评估、过程记录回放及性能报告汇总。默认不调用真实模型、不修改业务授权。具体范围、运行命令与未覆盖项见 [分层评估说明](SYSTEM_EVALUATION.md)。

星海部门版30份语料对应题集位于 `evals/xinghai_v3/`；题集与标准答案不要入库。当前分层入口与下面保留的历史Chroma实验不是同一个基线。

## 历史检索实验

以下检索报告来自切换 pgvector 之前的 Chroma 基线实验，仅用于保存算法演进记录。旧的 `scripts/inspect_retrieval.py` 已随 Chroma 正式链路删除，不能再用这些命令运行当前系统。当前 pgvector 链路应通过受保护的 `/api/qa` 和上述答案评估脚本进行测量。

主要指标：

- `Hit Rate@8`：前 8 个检索结果中至少包含一个正确证据的问题比例。
- `MRR@8`：正确证据排名倒数的平均值，越接近 1 表示正确证据排名越靠前。
- `Mean Latency`：单次检索平均耗时，不包含大模型生成答案的时间。
- `P50 Latency`：50% 的成功请求耗时不高于该值。
- `P95 Latency`：95% 的成功请求耗时不高于该值，用于观察慢请求。
- `Retrieval Latency`：向量检索、BM25 和 RRF 候选融合耗时。
- `Rerank Latency`：调用远程重排序模型的耗时。
- `Answerable Accept Rate`：可回答样本被检索置信门判为可回答的比例。
- `Unanswerable Rejection Rate`：无答案样本被置信门拒绝的比例。
- `Errors`：嵌入接口或检索调用失败的样本数量。

## 实验结果

| 方案 | 检索方式 | Hit Rate@8 | MRR@8 | 平均延迟 | P50 | P95 | 错误数 |
|---|---|---:|---:|---:|---:|---:|---:|
| 向量 MMR 基线 | Chroma MMR，`k=8`，`fetch_k=30` | 0.9583 | 0.8333 | 109.05ms | — | — | 0 |
| 混合检索 v1 | Chroma MMR + BM25 + 加权 RRF | 1.0000 | 0.8594 | 157.67ms | — | — | 0 |
| 混合检索 v2 | 检索器生命周期与并发改造，检索策略不变 | 1.0000 | 0.8594 | 146.51ms | — | — | 0 |
| 重排序 v1 | Qwen3 Rerank，`fetch_k=20` | 1.0000 | 0.9583 | 959.58ms | — | — | 0 |
| 重排序 v2 | Qwen3 Rerank，`fetch_k=20`，增加分位数 | 1.0000 | 0.9583 | 944.34ms | 931.84ms | 1087.16ms | 0 |

原始报告：

- [向量 MMR 基线](reports/retrieval_baseline.json)
- [混合检索 v1](reports/retrieval_hybrid_v1.json)
- [混合检索 v2](reports/retrieval_hybrid_v2.json)
- [重排序 v1](reports/retrieval_rerank_v1.json)
- [重排序 v2](reports/retrieval_rerank_v2.json)

## 独立测试集 A/B 结果

| 候选数 | Hit Rate@8 | MRR@8 | 平均延迟 | P50 | P95 | 可回答接受率 | 无答案拒绝率 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 20 | 0.9583 | 0.9375 | 999.15ms | 979.87ms | 1136.53ms | 0.9583 | 0.8750 |
| 30 | 1.0000 | 0.9792 | 1011.59ms | 1002.62ms | 1092.19ms | 1.0000 | 0.8750 |

30 个候选比 20 个候选平均只增加 12.44ms，但恢复了 `test-hr-001` 的轻微迟到证据，使 `Hit Rate@8` 从 95.83% 恢复到 100%。因此当前生产默认保留 `fetch_k=30`。

分阶段计时显示，30 个候选时混合召回平均耗时 134.45ms，远程重排序平均耗时 876.38ms，约占总延迟的 86.6%。

独立测试报告：

- [20 个候选](reports/retrieval_test_rerank_v1.json)
- [30 个候选](reports/retrieval_test_rerank_fetch30_v1.json)

## 排名分布

| 方案 | 第 1 名命中 | 前 2 名命中 | 前 8 名命中 | 未命中 |
|---|---:|---:|---:|---:|
| 向量 MMR 基线 | 34/48 | 46/48 | 46/48 | 2/48 |
| 混合检索 v1 | 35/48 | 47/48 | 48/48 | 0/48 |
| 混合检索 v2 | 35/48 | 47/48 | 48/48 | 0/48 |
| 重排序 v1 | 44/48 | 48/48 | 48/48 | 0/48 |
| 重排序 v2 | 44/48 | 48/48 | 48/48 | 0/48 |

## 实验结论

混合检索将 `Hit Rate@8` 从 95.83% 提升到 100%，并将 `MRR@8` 从 0.8333 提升到 0.8594。检索器生命周期与并发改造后，混合检索 v2 保持相同质量，平均延迟为 146.51ms。

重排序在保持 `Hit Rate@8 = 1.0` 的同时，将开发集 `MRR@8` 从 0.8594 提升到 0.9583，Top-1 命中从 35/48 提升到 44/48，Top-2 实现 48/48 全部命中。48 次查询返回的 384 个结果全部标记为 `rerank_status=success`，没有触发 RRF 降级。

向量基线未命中的两个样本均被混合检索召回：

- `tech-006`：旧 API 主版本停止服务的提前通知期限。正式技术手册在混合检索中排名第 2，FAQ 排名第 1。
- `leg-002`：99.2% 月度可用性对应的服务抵扣比例。正确合同表格在混合检索中排名第 2，FAQ 排名第 1。

重排序后仍有 4 题的正确证据位于第 2 名：

- `hr-003`：未休年假的结转天数与使用期限。
- `fin-002`：A 类城市不同岗位的住宿上限。
- `fin-004`：报销提交时限。
- `tech-005`：P1 故障的首次响应与更新频率。

当前仍需关注：

- 重排序开发集的平均延迟约为 944ms，独立测试集约为 1012ms，主要开销来自远程重排序调用。
- 候选数从 30 降为 20 会在独立测试集漏掉 1 条证据，不应只根据开发集结果减少候选。
- 在 `0.70` 阈值下，8 条无答案问题正确拒绝 7 条；`test-na-007` 因合同中存在“管辖地和适用法律待填写”的高相关片段，被误判为可回答。
- 重排分数是单次请求内的相对分数，`0.70` 仅作为当前语料上的实验性置信门，不应直接视为通用生产阈值。
- 当前指标只评估检索层的“建议回答/建议拒答”，尚未评估大模型最终是否遵守拒答要求。

## 当前检索链

```text
向量召回 + BM25
        ↓
加权 RRF 融合
        ↓
候选结果
        ↓
重排序
        ↓
最终上下文
```

## 下一阶段

- 新建独立的阈值校准集，不使用 `retrieval_test.jsonl` 调整置信阈值。
- 为检索层加入可观测性，持续记录召回、重排序、降级和总延迟。
- 在检索评估稳定后进入上下文去重、相邻块扩展、引用和答案层评估。
