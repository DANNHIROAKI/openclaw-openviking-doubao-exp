# Implementation Notes

这个 bundle 已经按照 `docs/EXPERIMENT_PLAN.md` 的 v2 三组正式方案，把实验计划落实成可执行编排代码。与初稿相比，核心补强点如下。

## 1. 实验编排从旧四组切换到 v2 三组

`scripts/experiment_spec.py` 现在集中定义三组正式配置：

- `g1-ov-nomemory` / `G1`
- `g2-noov-stock` / `G2`
- `g3-ov-stock` / `G3`

并同时定义：

- 3 组轮转顺序
- rerun 起始组平移逻辑
- planned comparisons（C1/C2/C3）
- 确定性的 `user` 命名规则

`scripts/orchestrate.py` 不再走旧版四组 `A/B` 主路径，而是按 v2 方案执行正式 benchmark。

## 2. `openclaw-eval` 级别的正式实验改造已经内联到 harness

`scripts/eval_harness.py` 已补齐：

- `--openclaw-home` / `--workdir` 工作区参数
- 清洗数据集校验：1540 条、无 category 5、`sample_id` 完整、`case_uid` 唯一
- 更稳定的 `case_uid` 生成与冻结逻辑
- ingest 与 QA 阶段 usage / elapsed / retry / raw response 记录
- 单题 `qa_elapsed_ms`、`qa_retry_count`、`qa_error_flag`
- OV 组的 per-task observer usage delta 捕获（若可见）

也就是说，runner 现在不仅能跑总分，还能为后续 artifact 生成逐题 telemetry。

## 3. OpenViking 观测与屏障等待增强

`scripts/openviking_probe.py` 已支持：

- 异步完成屏障等待：
  - `commit_count > 0`
  - `latest_archive_overview` 存在
  - `memories_extracted > 0`
- observer status 表解析
- observer/VLM token totals 抽取
- ingest 前、ingest 后、run 后 snapshot 保存

`scripts/orchestrate.py` 会把这些 snapshot 写入 `artifacts/raw/observer/`，并把解析得到的 OV 内部 usage 纳入 metrics 计算。

## 4. 新增正式 metrics 管线

`scripts/metrics.py` 是这次补强的核心新增文件，负责生成三类正式产物：

- `sample_ingest_metrics`
- `task_metrics_direct`
- `task_metrics_amortized`

其中：

- `task_metrics_direct` 反映单题直接 QA 成本与耗时
- `task_metrics_amortized` 把 sample 共享 ingest token / time 按题量均摊到每道题
- 分摊采用整数精确拆分，保证组级总量可由逐题结果精确回算

此外，`metrics.py` 还会自动导出：

- `metrics/sample_ingest/{group}.csv`
- `metrics/task_direct/{group}.csv`
- `metrics/task_amortized/{group}.csv`
- `metrics/sample_ingest_all_groups.csv`
- `metrics/task_metrics_direct_all_groups.csv`
- `metrics/task_metrics_amortized_all_groups.csv`

## 5. judge 审计信息增强

`scripts/judge_harness.py` 现在除了最终对错外，还会持久化：

- `judge_correct`
- `judge_reasoning`
- `judge_reasoning_raw`
- `judge_prompt_version`
- `judge_provider_model_id`
- `judge_parsed_json`
- `judge_result_raw`
- `judge_usage`

这满足 v2 对 raw judge audit 的要求。

## 6. 汇总逻辑已改成 v2 的主表与 planned comparisons

`scripts/summary.py` 现在只围绕 G1/G2/G3 生成正式结果，并输出：

- `summary/main_table.md`
- `summary/planned_comparisons.md`
- `summary/sample_breakdown.md`
- `summary/category_breakdown.md`
- `summary/latency_breakdown.md`
- `summary/per_task_schema.md`
- `summary/summary.json`

主检验采用按 `sample_id` 聚类的 paired bootstrap，围绕以下三组预注册比较：

- `C1: G1 vs G2`
- `C2: G3 vs G2`
- `C3: G3 vs G1`

## 7. manifest 与 run 级审计信息更完整

每个 `sample × group × rerun` 的 manifest 现在会记录：

- 运行时 OpenClaw / OpenViking 版本
- group 锁定配置快照
- 数据集 sample / case 数
- ingest stage timing
- sample ingest metrics 汇总
- task metrics 路径
- metrics reconciliation 结果
- provider model ids
- judge provider model ids
- 日志与 observer snapshot 路径

总 manifest 则额外记录：

- helper repo / dataset / official repos commit SHA
- openclaw-eval 当前仓库 commit
- OS / Python / Node 版本
- 数据校验摘要
- 本次选中的 rerun / groups / samples

## 8. 当前实现边界

- 这是一个 **thin bundle**：正式运行时仍会按锁定版本拉取上游仓库
- vendored plugin snapshot 仅用于减小 helper repo 漂移风险，不替代运行时 commit 记录
- OV 内部 token 统计仍依赖 observer / telemetry 可见性；但和初稿不同的是，现在若正式 run 拿不到这部分 usage，会直接被判为不满足正式主表要求，而不是悄悄降级
