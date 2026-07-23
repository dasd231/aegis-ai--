# RAG 分层评测

该目录的评测不是单独实现的 dense-cosine 演示，而是复用在线链路：

1. `build_retrieval_plan`：按 RAG 版本执行单查询或审核过的多查询扩展；
2. `VectorStore.query_many`：在一次 Qdrant batch 中执行 dense、lexical sparse 和 weighted RRF；
3. `rank_evidence`：实体边界、置信度门控、关键规则保护和近重复抑制；
4. `maybe_rerank`：可选的线上 reranker；
5. `build_context`：按预算恢复父级上下文。

评测不会调用答案生成模型。每次运行会在系统临时目录创建独占的本地 Qdrant collection，索引完成后关闭客户端并删除整个临时目录，不读取或修改线上 Stable/Canary collection。

## 运行

在 `backend` 目录执行：

```powershell
python -m scripts.evaluate_rag
python -m scripts.evaluate_rag --rag-version multi_rrf_v3 --embedding-pool canary
python -m scripts.evaluate_rag --rag-version multi_rrf_v3 --embedding-pool canary --with-reranker
python -m scripts.evaluate_rag --output evaluation/report.json
```

`--embedding-pool` 会选择对应的模型、维度和版本，但评测索引始终是一次性的物理隔离索引。`--with-reranker` 调用线上 `maybe_rerank`；报告中的 `attempt_rate`、`apply_rate` 和逐例 `reason` 可以区分实际应用、确定性跳过及依赖降级，不能仅凭命令行参数声称 reranker 生效。

## 用例标注

每条用例至少包含：

- `answerable`：知识库是否足以回答；
- `expected_sources_any`：命中其中任意一个即满足的替代来源组；
- `expected_sources_all`：可选，必须全部覆盖的多来源事实；
- `required_phrases`：必须进入最终上下文的原文事实，不是模型生成答案；
- `critical_rule`：是否属于安全、承诺或合规关键规则；
- `tags`：可选的困难近邻、部分覆盖、父级末尾、口语改写等切片。

新增答案只能来自 `sample_data` 中实际存在的原文。拒答用例不得声明来源或答案短语，避免把期望答案反向塞给系统。

## 指标解释

报告按层输出：

- Candidate：Top-K 候选 Recall@K、候选数和来源数；
- Fusion：RRF 后窗口的 Recall@K、MRR、Precision@K 和来源数；
- Rerank：最终证据的 Recall@K、MRR、Precision@K、来源数与 reranker trace；
- Context：Context Precision、来源召回、关键短语召回、父级扩展增益、字符数和来源数；
- Answerability：准确率，以及以“应拒答”为正类的 abstention precision / recall / F1；
- Critical recall：关键用例同时满足来源、原文事实和高优先级规则的比例；
- Latency：planner、`query_many`、确定性排序、rerank、context 和总耗时的 mean/P95/max。

候选检索与 weighted RRF 在生产 `query_many` 内是同一次调用，因此延迟字段明确记为 `candidate_and_fusion`，不会虚构无法观测的两个时钟。`expected_sources_*` 是正例标签而不是对所有片段的穷尽相关性判断，所以严格 Context Precision 会把未标注但可能有帮助的辅助片段计为假阳性；质量门将它作为保守下界，同时对召回、关键事实和拒答 F1 使用 0.95 的严格阈值。

`ablation_id` 由 RAG 策略 SHA、查询模式、Embedding pool/模型/版本、reranker 开关、索引 schema、语料和用例指纹共同生成，可用于稳定对比 Stable/Canary，不包含临时路径或密钥。

## 2026-07-23 真实服务基线

在同一份 30 例语料和一次性本地 Qdrant 索引上，使用项目现有百炼配置完成了三组顺序评测。以下延迟包含远程 API 网络时间，是单机功能基线，不是并发压测 SLA：

| 消融组 | 质量门 | Candidate Recall@K | Abstention F1 | Critical Recall | 关键变化 | P95 |
|---|---:|---:|---:|---:|---|---:|
| `text-embedding-v4 + hybrid_rrf_v2`，无 reranker | 通过 | 1.0000 | 1.0000 | 1.0000 | Context Precision 0.7267 | candidate+fusion 7150 ms |
| `qwen3.7-text-embedding + multi_rrf_v3`，无 reranker | 通过 | 1.0000 | 1.0000 | 1.0000 | Context Precision 0.7267 | candidate+fusion 7212 ms |
| 上述 Canary + `qwen3-rerank` | 通过 | 1.0000 | 1.0000 | 1.0000 | Fusion MRR 0.9800 → Rerank MRR 1.0000；Precision 0.4540 → 0.7067；实际 Apply 83.33% | rerank 4838 ms |

这组结果说明 reranker 确实改善了候选纯度和首条命中，不只是“接了接口”；代价是远程 rerank P95 约 4.84 秒。生产默认应优先使用条件触发、缓存和候选裁剪，将 `always` 保留在 Canary/离线评测，不应在没有延迟预算的情况下直接全量开启。

完整逐例 JSON（均为 30 例、索引 schema v4）分别保存在：

- `dashscope-text-embedding-v4.json`：Stable；
- `dashscope-qwen37-text-embedding.json`：Canary；
- `dashscope-qwen37-text-embedding-qwen3-rerank.json`：Canary + Rerank。

`latest-report.json` 与第三份报告逐字节一致，固定指向本轮最新真实 Canary + Rerank 结果；更新报告时必须先通过质量门，再以原子替换方式刷新该文件，不能让旧 26 例结果继续冒充 latest。
