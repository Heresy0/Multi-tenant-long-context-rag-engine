# RAG 检索评估

本目录用于保存企业知识库检索层的评估数据、原始报告和实验结论。当前评估衡量证据召回、排序质量、分阶段延迟，以及基于 Top-1 重排分数的检索层无答案判断。该判断不等同于大模型最终答案的正确性、忠实度或真实拒答行为。

## 目录结构

```text
evals/
├── datasets/
│   ├── retrieval_dev.jsonl
│   └── retrieval_test.jsonl
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

## 运行方式

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_retrieval.py `
  --dataset evals\datasets\retrieval_dev.jsonl `
  --k 8 `
  --fetch-k 20 `
  --answerability-threshold 0.70 `
  --output evals\reports\retrieval_rerank_v2.json
```

独立测试集：

```powershell
.\.venv\Scripts\python.exe scripts\inspect_retrieval.py `
  --dataset evals\datasets\retrieval_test.jsonl `
  --k 8 `
  --fetch-k 30 `
  --answerability-threshold 0.70 `
  --output evals\reports\retrieval_test_rerank_fetch30_v1.json
```

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
