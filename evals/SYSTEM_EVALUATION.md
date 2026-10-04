# 企业知识库分层评估体系

## 1. 已实现的结构

统一入口：`scripts/evaluate_system.py`。旧的 `scripts/evaluate_answers.py`（HTTP最终回答评估）和 `scripts/summarize_load_test.py`（Locust性能门禁）仍保留，不改变旧题集或历史报告。

| 评估层 | 当前自动检查 | 尚需人工或专项检查 |
|---|---|---|
| parsing | 文件哈希、目录清单、真实项目解析器、非空片段、来源/章节字段、标注证据是否保留；可选真实OCR | 完整版式、所有表格单元格及所有事实的准确性 |
| index | 真实授权范围内的文档ready状态、片段数量、向量维度、关键词非空、租户/库一致性、30份语料的DB哈希和缺失文件 | 存储文件字节校验、数据库搜索索引维护状态、更新/删除传播时限、备份恢复 |
| retrieval | 向量、BM25、RRF分别记录候选；按文档名+证据片段计算命中率、首个正确证据排名和证据覆盖 | 完整相关性标注、同义证据人工确认 |
| rerank | 与融合候选分开计算MRR和证据覆盖，检查重排后必要证据是否丢失 | 没有分级相关性标签时不伪造NDCG |
| context | 检查实际选入/截断后的上下文，不用全部候选代替；计算必要证据覆盖 | 标题与正文歧义、复杂跨句依赖 |
| answer | 回答/拒答判断、参考事实短语、所有必要来源引用、与实际上下文的引用编号/片段绑定、被引证据覆盖 | 语义正确性、所有结论忠实度、否定/单位推理、答案完整性；这些字段明确标记需人工复核 |
| conversations | 每组新会话、逐轮问答、读取实际retrieval_question、逐轮事实/引用评分，结束后清理本次创建的会话 | 改写是否改变意图、长期摘要质量、跨会话串记忆 |
| security | 离线真实测试代码+SQLite/模拟服务回归；在线按专用身份验证当前有效可读库，再执行正例、拒答和提示词越权负例 | 真实部署的撤权、会话拥有者、跨租户及空权限状态机仍需隔离环境专项验收，不自动标通过 |
| performance | 导入现有Locust汇总报告及门禁结果；旧的没有200成功请求校验的报告标记不完整 | 本入口不自动发起并发压测、压力故障注入或生产可用性认证 |

权限检查是横跨各层的要求，不只是表中的security。过程记录会标记越权结果并删除其正文/标题；普通问答响应不会包含过程记录。

## 2. 过程记录如何工作

`backend/app/evaluation/trace.py`使用ContextVar保持本地运行上下文隔离。只有在`capture_trace()`中才收集记录；未开启时立即返回，不改检索顺序、候选数、提示词或HTTP响应结构。

记录位置：

1. `HybridRetriever`：向量结果、BM25结果、RRF融合结果。
2. `RetrievalService`：已授权范围、检索问题、最终重排结果。
3. `AnswerService`：防御性授权过滤后的结果、实际ContextBuilder选中的内容。

没有新增调试API，没有迁移数据库，也不把过程记录保存到正常会话或业务数据库。`--save-traces`才把授权正文快照写到本次本地报告目录；文件里不写JWT、API密钥、数据库连接串或文档内部存储路径。

## 3. 三种运行模式

### 离线（默认，先用这个）

在项目根目录运行：

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode offline
```

执行真实文档解析/切分及隔离权限回归，不调用真实向量、重排或生成服务，不修改业务数据库/授权。其他层明确标记skipped。Windows接口测试需要本机回环socket权限；运行在受限沙箱中时可能超时，此时应按环境审批运行，而不是把环境错误算成安全通过。

只检查文档：

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode offline --layers parsing
```

加`--ocr`会执行本地真实OCR；没有OCR运行环境则标记skipped。不会拿扫描原文或参考答案冒充OCR结果。文档开头没有章节标题时，空section_path合法，不应误判解析失败。

### 真实分层质量评估（显式开启模型调用）

先将`evals/system_config.example.json`复制为`evals/system_config.local.json`，核对知识库编号。通过安全方式设置对应JWT环境变量，配置文件仅写环境变量名，不写JWT或密钥。

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --layers retrieval,rerank,context,answer --limit 3 --allow-model-calls --save-traces
```

这3道题只做小规模冒烟，不代表60道全部通过。`--limit`分别限制每个题集的案例数，不是总请求数/总模型次数；一组多轮还会产生多个回答和追问改写请求，SDK可能有额外重试，不能直接据此推算token费用。

质量层在本地调用当前正式后端服务，而不是开放原始片段的调试HTTP接口。JWT通过正式OIDC验签并映射到活动用户；每题按AuthorizationService重新取得范围。数据访问只读，失败会回滚读事务。检索、重排、上下文和答案使用同一次流水线结果；不会为每层重复生成答案。

注意：这是后端业务链质量评估，不含HTTP网关、限流、审计和网络耗时。真实接口最终回答仍可使用旧的`evaluate_answers.py`；多轮/权限用例通过真实HTTP执行，性能通过Locust执行。

“全范围测试用户”只是测试角色定义，不是已有账号。本地质量入口要求该测试身份有效可读库与配置一致；当前Alice/Bob不一定符合。不要为跑分擅自给业务用户扩大权限。请由获准管理员在隔离环境预先准备身份，或先使用离线/回放模式。

只读索引审计不产生模型请求，但需要真实认证及数据库连接：

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --layers index
```

全语料索引审计要求对30份语料所在库都有读权限，不把无权限资料当成“缺失”。检测到其他旧资料不会删除它们。

本地运行需`.env`的DATABASE_URL、OIDC/JWKS及模型配置可从本机访问。容器中的主机名`postgres`等不一定可从宿主机解析；不要为运行评估覆盖现有服务配置。也可在包含新代码的容器中挂载题集/语料/配置/输出运行，本次没有自动重建或重启容器。

### 回放（不再调用模型）

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode replay --layers retrieval,rerank,context,answer --trace-input evals/reports/system/某次运行/traces.jsonl
```

按题号和问题哈希对齐，缺失记录或改过的问题标记skipped。回放只重新评分已保存的观察，不证明当前部署仍有同样质量；报告明确标记replay及incomplete。

## 4. 多轮与权限执行

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --layers conversations --limit 1 --allow-model-calls --allow-conversation-writes
```

每组使用新建的自己的会话，保持组内上下文，记录每轮正式接口回答和持久化的retrieval_question。只删除本次新建的会话，不清理用户旧会话。正常的追加审计事件会保留；清理失败会在报告中标error。

```powershell
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --layers security --allow-model-calls
```

在线安全测试不会自动创建用户、改部门或撤权。缺少环境变量、授权范围不符、401、429、503等均不能算权限通过。负例必须在隔离测试环境确认“私有资料已存在且当前用户可见资料没有等价答案”，再在本地配置把对应`no_equivalent_visible_evidence_verified`设为true；否则跳过。该标记只是操作者准备条件的声明，不是程序自行证明。

ACL-08/10/11/12涉及状态变化、真实他人会话、跨租户及完全无可读库，需要独立准备和专项验收。本入口当前会明确跳过在线案例；离线单元回归覆盖相关代码路径。原题集仍保留这些验收项，不将它们删掉或当成已通过。

## 5. 指标与报告口径

默认输出到`evals/reports/system/<UTC时间戳>/`：

- `report.md`：分层结果与非通过项。
- `report.json`：每题指标、原始回答、配置/题集口径和状态。
- `security_unit.xml`：隔离单元测试明细（离线安全检查时）。
- `traces.jsonl`：向量/BM25/融合/重排/实际上下文快照（仅显式保存时）。

报告目录和本地配置已加入Git忽略。报告也可能包含业务回答，请按敏感资料保管，不上传知识库。

证据命中要求文档名与证据文本同时匹配，不把“文档标题正确但段落不支持答案”算命中。允许文件名有/无扩展名、Unicode全半角和表格分隔符的规范化。Hit Rate表示至少命中一个标注证据；evidence_recall表示必要标注证据被找全的比例；all_evidence_rate表示每题所需证据全部出现的比例。它们不能互相替代，也不是针对整个语料穷尽标注的召回率。

MRR按第一个含正确标注证据的结果排名计算。向量/BM25/融合按各阶段实际候选列表计算，重排按最终列表计算。没有正例证据的不可回答题不计算检索命中率，标为not_applicable而不是未测试；它们由最终拒答指标评价，不把检索空结果自动判定为高质量回答。

答案检查比旧脚本更严格：跨文档题要求所有必要来源，而非只命中一个；有过程记录时，引用编号、文档名、片段ID、库ID必须与实际上下文匹配，引用内容还需包含标注证据。但这仍不等于对所有自然语言结论完成语义验证。

不同身份、语料版本、日期和题集范围必须分别记录，不能混合比较。当前XH-E-057以扫描原件的“周宁”为标准，不能因OCR索引错误改成“周末”。实际OCR/模型错误应保留为失败，而非删除题目提升分数。

退出码：0表示自动流程结束且未发现失败（仍可能有未测试）；1表示出现失败/错误；加`--strict`时，未测试或部分结果退出2。`passed_automated_checks_not_production_certification`只说明请求的自动检查通过，不是生产安全认证。

## 6. 基线对比与性能复用

加`--baseline-report 上次report.json`可以比较相同模式、题集哈希、案例集合、语料清单、授权配置及报告版本下的指标变化。模型可以不同以便A/B，但需检查runtime记录。口径不同则标记incompatible，不硬算提升；指标差值是诊断，不是自动部署审批。

旧Chroma检索报告只能作为历史记录，不能直接当成当前PostgreSQL和30份语料的基线。

性能仍先用现有Locust压测和`summarize_load_test.py`生成汇总，再用`--performance-report <汇总.json>`导入统一报告。不自动在生产环境压测。旧报告如果没有验证成功200问答请求，将标记partial；不能把全部401而响应很快的报告当成正常问答性能。

## 7. 本次验收范围

实现并检查了评估程序、默认关闭的过程记录和现有问答行为回归。没有执行真实模型质量跑分、创建测试身份、调整业务授权或重启线上服务。

功能回归与真实生产质量不同：下一步应在已授权隔离环境执行带模型的冒烟与完整题集，再由业务人员复核语义、权限状态变化及实际并发性能。
