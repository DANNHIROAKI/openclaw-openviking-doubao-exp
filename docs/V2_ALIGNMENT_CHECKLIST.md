# V2 Alignment Checklist

这份清单用于快速核对本仓库是否已经落实 `docs/EXPERIMENT_PLAN.md` 的 v2 正式实验要求。

## 一、三组正式配置

已落实位置：`scripts/experiment_spec.py`

- [x] 只保留 `G1 / G2 / G3`
- [x] 旧版 `No-OV / no-memory` 不再进入正式 runner
- [x] 统一 short id / slug / label
- [x] 预注册 comparisons：`C1 / C2 / C3`

## 二、正式评测统一走 OpenClaw Gateway `/v1/responses`

已落实位置：`scripts/eval_harness.py`, `scripts/orchestrate.py`

- [x] ingest 通过 Gateway 发送
- [x] QA 通过 Gateway 发送
- [x] 不存在正式 runner 里的直写 `ov add-memory` 分支

## 三、版本硬锁定与运行时校验

已落实位置：`VERSIONS.lock.json`, `scripts/orchestrate.py`

- [x] OpenClaw 版本锁定为 `2026.4.14`
- [x] OpenViking 版本锁定为 `0.3.8`
- [x] 安装后做运行时版本检测
- [x] 版本不符直接终止

## 四、数据集正式口径与预检

已落实位置：`scripts/eval_harness.py`

- [x] 校验总题数是否为 `1540`
- [x] 校验 `category 5 = 0`
- [x] 校验 `sample_id` 完整
- [x] 校验 `case_uid` 唯一
- [x] 固定导出 `sample_question_counts`

## 五、`sample × group × rerun` 强隔离

已落实位置：`scripts/orchestrate.py`

- [x] 每个 run 从独立临时工作区启动
- [x] 跑完归档并默认销毁
- [x] 显式 deterministic `--user`
- [x] ingest 后与每题 QA 后保留 reset

## 六、三组轮转顺序

已落实位置：`scripts/experiment_spec.py`, `scripts/orchestrate.py`

- [x] Sample 1：`G1 -> G2 -> G3`
- [x] Sample 2：`G2 -> G3 -> G1`
- [x] Sample 3：`G3 -> G1 -> G2`
- [x] 多 rerun 时整体平移起始组

## 七、OV 异步完成屏障与 No-OV 固定静默窗口

已落实位置：`scripts/openviking_probe.py`, `scripts/orchestrate.py`

- [x] OV 组等待 `commit_count > 0`
- [x] OV 组等待 `latest_archive_overview` 存在
- [x] OV 组等待 `memories_extracted > 0`
- [x] No-OV 组等待固定 `EXP_NOOV_QUIET_WAIT_MS`
- [x] 等待时间计入 ingest elapsed

## 八、正式 metrics 产物

已落实位置：`scripts/metrics.py`

- [x] `sample_ingest_metrics`
- [x] `task_metrics_direct`
- [x] `task_metrics_amortized`
- [x] group CSV 导出
- [x] all-groups CSV 导出
- [x] token / elapsed 可回算校验

## 九、judge raw 审计增强

已落实位置：`scripts/judge_harness.py`

- [x] 保留 `judge_correct`
- [x] 保留 `judge_reasoning`
- [x] 保留 `judge_prompt_version`
- [x] 保留 `judge_provider_model_id`
- [x] 保留 `judge_result_raw`

## 十、summary 与正式结果表

已落实位置：`scripts/summary.py`

- [x] `main_table.md`
- [x] `planned_comparisons.md`
- [x] `sample_breakdown.md`
- [x] `category_breakdown.md`
- [x] `latency_breakdown.md`
- [x] `per_task_schema.md`
- [x] `summary.json`
- [x] 按 `sample_id` 聚类的 paired bootstrap

## 十一、manifest 与 artifact 审计

已落实位置：`scripts/orchestrate.py`

- [x] 记录 helper repo / dataset / official repos commit
- [x] 记录 openclaw-eval 当前仓库 commit
- [x] 记录运行时 OpenClaw / OpenViking 版本
- [x] 记录 provider model ids / judge provider model ids
- [x] 记录 metrics validation 结果
- [x] 记录日志与 observer snapshot 路径

## 十二、当前仍依赖真实运行环境验证的部分

这些部分已经在代码路径中实现，但只有连到真实 OpenClaw / OpenViking 环境后才能最终验证：

- [ ] 实际 OpenViking observer 格式与当前解析器完全匹配
- [ ] 官方 `openclaw/openclaw` 仓库的 tag / ref 与 `2026.4.14` 的 clone 策略在真实网络环境下可用
- [ ] 当前 provider 暴露的 `seed-2.0-code` alias 与实验环境一致
- [ ] smoke check 在你的目标主机上能稳定闭环ผ่าน

这四项不属于仓库结构缺失，而属于正式跑实验前的环境级确认。
