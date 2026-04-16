# OpenViking × OpenClaw LoCoMo10 Experiment Bundle

这个仓库是一个**严格对齐 v2 正式实验计划**的可执行实验编排包。配置好环境变量后，执行：

```bash
bash run.sh
```

即可自动完成整条正式流水线：

1. 预检依赖与环境变量
2. 拉取并记录上游仓库 commit
3. 安装并校验 **OpenClaw 2026.4.14**
4. 安装并校验 **OpenViking 0.3.8**
5. 生成 **G1 / G2 / G3** 三组锁定模板
6. 执行一次不计分 smoke check
7. 对 `OpenViking-LoCoMo10` 的 10 个 conversation sample 按 **三组轮转顺序**完成正式实验
8. 自动 judge，并保留 raw 审计信息
9. 自动生成主表、planned comparisons、sample/category/latency breakdown
10. 输出 sample 级与 task 级 metrics，保证 token / time 可回算

## 当前正式实验口径

本仓库默认以 `docs/EXPERIMENT_PLAN.md` 的 v2 版本为唯一准绳。正式主表只允许这三组：

| 组 ID | 组别 | memory | contextEngine | openviking.enabled | deny |
| --- | --- | --- | --- | --- | --- |
| G1 | OV / no-memory | `none` | `openviking` | `true` | `[]` |
| G2 | No-OV / stock | `memory-core` | `legacy` | `false` | `["openviking"]` |
| G3 | OV / stock | `memory-core` | `openviking` | `true` | `[]` |

仓库已经按 v2 方案落实以下约束：

- 正式数据集固定为 **1540-case** 清洗版 LoCoMo10
- 正式入口固定为 **OpenClaw Gateway `/v1/responses`**
- 每个 `sample × group × rerun` 使用独立临时工作区
- ingest 后和每题 QA 后都保留 session reset
- OV 组等待异步完成屏障；No-OV 组等待固定静默窗口
- 主表 token 成本按**系统总 input token**记账；若 OV 内部 usage 缺失，则该 run 不进入正式结果
- 逐题同时输出 **direct** 与 **amortized** 两套 metrics
- judge token / judge time 不进主表，但保留 raw 审计输出

## 目录

```text
run.sh
requirements.txt
VERSIONS.lock.json
docs/
scripts/
licenses/
vendor/              # vendored 的 openclaw-openviking-doubao plugin 快照
artifacts/           # 运行后自动生成
cache/               # 运行后自动生成
templates/           # 运行后自动生成
workspace/           # 运行时基线快照与临时工作区
runs/                # 可选保留的临时运行目录
```

## 最小使用方式

1. 复制环境变量模板

   ```bash
   cp .env.example .env
   ```

2. 至少填入实验主模型 API Key

   ```bash
   VOLCANO_ENGINE_API_KEY=...
   ```

3. 执行

   ```bash
   bash run.sh
   ```

## 主要输出

运行完成后，核心结果位于：

```text
artifacts/
  manifest.json
  preflight.json
  repo_commits.json

  configs/
    group-g1-ov-nomemory.openclaw.json
    group-g2-noov-stock.openclaw.json
    group-g3-ov-stock.openclaw.json
    group-g1-ov-nomemory.ov.conf
    group-g3-ov-stock.ov.conf

  raw/
    ingest/
    qa/
    judge/
    judge_raw/
    observer/

  logs/
    openclaw/
    openviking/

  manifests/
    *.json

  metrics/
    by_run/
      sample_ingest/
      task_direct/
      task_amortized/
    sample_ingest/
      g1-ov-nomemory.csv
      g2-noov-stock.csv
      g3-ov-stock.csv
    task_direct/
      g1-ov-nomemory.csv
      g2-noov-stock.csv
      g3-ov-stock.csv
    task_amortized/
      g1-ov-nomemory.csv
      g2-noov-stock.csv
      g3-ov-stock.csv
    sample_ingest_all_groups.csv
    task_metrics_direct_all_groups.csv
    task_metrics_amortized_all_groups.csv

  summary/
    main_table.md
    planned_comparisons.md
    sample_breakdown.md
    category_breakdown.md
    latency_breakdown.md
    per_task_schema.md
    summary.json
```

## 核心实现点

### 1. 三组锁定模板与运行时校验

`scripts/orchestrate.py` 会生成并校验三组模板，只允许以下 group slug 进入正式运行：

- `g1-ov-nomemory`
- `g2-noov-stock`
- `g3-ov-stock`

每个 run 在启动前都会再次检查：

- `openclaw --version == 2026.4.14`
- `openviking --version == 0.3.8`
- `memory/contextEngine/openviking.enabled/plugins.deny` 与锁定配置一致
- `/v1/responses` 已启用

### 2. sample 级与 task 级双层 telemetry

仓库会自动生成三类正式 metrics：

- `sample_ingest_metrics`：每个 `sample × group × rerun` 一行
- `task_metrics_direct`：每个 `case × group × rerun` 一行
- `task_metrics_amortized`：把 sample 共享 ingest 成本均摊到每道题后的逐题视角

`task_metrics_amortized` 使用**整数精确分摊**，保证组级总 token / 总时间能被逐题求和回算。

### 3. OV 使用量与异步屏障

OV 组会在 ingest 前后和 run 结束后抓取 observer snapshot，并尽可能解析内部 VLM usage。正式 run 必须满足：

- `commit_count > 0`
- `latest_archive_overview` 存在
- `memories_extracted > 0`

若 OV 内部 usage 无法取到或 token / time 无法对账，则该 run 会被标记为无效，不能进入正式主表。

### 4. judge 审计增强

judge 阶段除了 `grade` 之外，还会保存：

- `judge_correct`
- `judge_reasoning`
- `judge_prompt_version`
- `judge_provider_model_id`
- `judge_parsed_json`
- `judge_result_raw`

便于后续误判复核与 artifact 审计。

## 常用环境变量

`.env.example` 已给出默认模板，调试时最常用的是：

- `OPENCLAW_PRIMARY_MODEL_REF=seed-2.0-code`
- `EXP_SAMPLE_FILTER=0,1`
- `EXP_GROUP_FILTER=g1-ov-nomemory,g3-ov-stock`
- `EXP_RERUNS=1`
- `EXP_SKIP_SMOKE=0`
- `EXP_KEEP_RUNS=0`
- `EXP_RESUME=1`
- `EXP_FORCE_RECLONE=0`
- `EXP_FORCE_REBOOTSTRAP=0`
- `EXP_NOOV_QUIET_WAIT_MS=3000`
- `EXP_OPENVIKING_INSTALL_MODE=auto`
- `EXP_SUMMARY_RERUN=1`

## 说明

这个 ZIP 仍然以 **thin bundle** 为主，但为了与 `openclaw-openviking-doubao` 对齐，额外内置了一份同步插件快照：

- ZIP 内包含实验编排代码、评测 harness、judge harness、metrics materialization 与汇总逻辑
- `vendor/openclaw-openviking-doubao/plugin/` 会被优先用于安装 OpenViking OpenClaw 插件，避免远端 helper repo 漂移影响实验
- 运行时仍会拉取并记录上游仓库：
  - `DANNHIROAKI/openclaw-openviking-doubao`
  - `DANNHIROAKI/OpenViking-LoCoMo10`
  - `volcengine/OpenViking`
  - `openclaw/openclaw`
- API Key 只通过环境变量注入，不会写死在仓库里

## OpenViking 0.3.8 安装说明

默认模式是 `EXP_OPENVIKING_INSTALL_MODE=auto`：

1. 先尝试安装 PyPI 的 `openviking==0.3.8`
2. 立即用 CLI / server 自检验证运行时版本
3. 若 wheel 在当前环境不可用，则自动回退到源码构建

源码构建需要 Rust、Go 和 C/C++ 编译器。
