# Third-Party Notices

本 bundle 以实验编排代码为主；同时为了与 `openclaw-openviking-doubao` 保持一致，ZIP 内额外 vendored 了一份同步的 OpenViking OpenClaw plugin 快照：

- `vendor/openclaw-openviking-doubao/plugin/`
- `vendor/openclaw-openviking-doubao/NOTICE`
- `vendor/openclaw-openviking-doubao/VERSIONS.lock`

运行时仍会拉取固定版本的上游项目。

你在使用本 bundle 时，仍需分别遵守各上游项目的许可证与使用条款。常见涉及项目包括：

- OpenClaw — MIT License
- OpenViking — AGPL-3.0
- OpenViking examples / 部分示例文档 — Apache-2.0
- `openclaw-openviking-doubao` — 以仓库页面实际许可证与附带 NOTICE 为准
- `OpenViking-LoCoMo10` — 以仓库页面实际许可证与数据说明为准

本 ZIP 已随附和 vendored 插件快照直接相关的许可证文本：

- `licenses/THIRD_PARTY_LICENSES_OpenViking_AGPL-3.0.txt`
- `licenses/THIRD_PARTY_LICENSES_OpenViking_examples_Apache-2.0.txt`

建议你在正式分发实验包或发表附带材料前，再次核对：
- vendored 代码是否存在
- 是否需要附带 LICENSE / NOTICE
- 数据集与模型 API 的使用条款是否允许再分发与公开结果
