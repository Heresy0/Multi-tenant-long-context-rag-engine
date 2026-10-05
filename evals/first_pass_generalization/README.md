# 首次回答完整性补充题集

8道新组合问法，覆盖人力资源部、技术部、平台研发部。不是原60题的替换，不改原题答案或降低原评分标准。题目与标注不会进入生成提示词。

这仍使用同一批语料，属于新问法/多要点回归，不是独立新企业语料上的泛化证明，也不是从未参与开发的严格盲测。人工检查否定、条件和数值对象绑定；关键词全部出现并不保证语义正确。

2026-10-06：仅FP-008标注修订为v2，增加“不应重新执行业务”的已审核等价表达及逐题有限否定/矛盾检查。其他7题仍为v1，原问题、标准答案和必答事实不变；历史报告未重写。

运行需要已有有效JWT及获准权限，不自动增加用户权限：

```powershell
$env:REDIS_URL="redis://127.0.0.1:6379/0"
.venv\Scripts\python.exe scripts/evaluate_system.py --mode live --config evals/system_config.local.json --dataset-dir evals/first_pass_generalization --layers retrieval,rerank,context,answer --allow-model-calls --save-traces
```

只选择这些层；该目录没有多轮和权限状态变化题集，不能使用默认all。报告位于evals/reports/system的新时间戳目录。与原60题的题集哈希和样本集合不同，不能直接比较总分。
