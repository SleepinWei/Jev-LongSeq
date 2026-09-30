# 原版 jev-ultrafast · SaaS-Bench 首轮试跑

2026-09-29 在 Mac mini 完成两项试跑。**两项均未完成业务操作；一项有效判分失败，另一项因上游评分器异常不纳入成功率。** 这不是全量 106 项成绩。

## 任务范围

| 领域 | 数量 | 典型任务 |
| --- | ---: | --- |
| 商业 | 15 | 报销、入离职、会计结算、CRM、活动运营 |
| 医疗 | 16 | 医疗记录、表单、文档处理 |
| 软件工程 | 31 | 开发、项目管理、数据与报表 |
| 团队协作 | 12 | 文档、聊天、文件、邮件 |
| 农业 | 12 | 农场记录、库存、食谱、产品标签 |
| 媒体 | 20 | 知识库、图书、照片与视频管理 |

共 106 项，74 项位于上游 `uni-m`、32 项位于 `multi-m`。完整 ID、应用与公开任务描述见 [任务目录](SAAS-BENCH-TASKS.md)。当前已安装 HRMS、BigCapital、Twenty 三个官方镜像。

## 本轮结果

| 任务 | 原版 Agent 结果 | 浏览器动作 | HTTP 请求 | 官方原始得分 | 评分是否有效 |
| --- | --- | ---: | ---: | --- | --- |
| `business_023` 员工报销 | 登录页输入辅助模型抛出 `FieldTextError` | 0 | 2 | 2/20（10%） | 是，任务失败 |
| `business_031` 员工离职结算 | 填写用户名 4 次后触发停滞保护，`blocked` | 4 | 8 | 0/15（0%） | 否，上游 SQL 异常 |

- `business_023` 的 2 分来自镜像初始状态中已有的三条报销明细；不是 Agent 完成的工作。
- `business_031` 官方脚本查询 BigCapital 不存在的 `CONTACT_NORMAL_NAME` 与 `PUBLISHED` 字段，将异常包装为普通 FAIL。适配器现在识别该情况，保留原始得分和检查详情，但设置 `data_valid=false`、`strict_success=null`，从成功率分母排除。
- 有效评分为 1 项，严格成功 0 项。不能将第二项的原始 0 分当作正常测量值，也不能据此估计全套成功率。
- 原版 `snapshot.js` 第 9 行排除了 password/file/hidden 类型输入，因此观察到的登录动作列表中没有密码输入框。原版也没有跨应用 URL 导航或切换标签页动作。这些能力限制保持原样，本轮没有修改原版源码。
- 两项共 10 次 HTTP 请求，均返回 200；记录 44,044 input tokens、748 output tokens。HTTP 成功不代表 Agent 操作成功。价格未配置，费用未知。
- Agent 总耗时 17.70 秒；含两次容器准备、判分与清理约 164.64 秒。两个任务均已清理容器，无残留 `jevsaas` 容器。

## 可复现配置与证据

- 上游版本：`48c22deb18b98c0ed78f81e7f3be82bc162de2c8`。
- 控制器：原始 `jev_ultrafast.Agent`，没有 LongSeq planner 调用。
- 模型：`jev-latest`，文本输入辅助：`deepseek-flash`。
- 限制：原版 60 动作、外层每任务 600 秒；两项均提前停止。
- 原版五个核心源码文件合并 SHA-256：`cbcf8f75faed99f816edb9b6b3c15bed67b390270c652285915573816121d67c`。
- Mac mini 结果目录：`/Users/octopusz/CodeProjects/Jev-LongSeq/runs/saas-ultrafast-20260929-01`。
- 原始官方 verifier 输出完整保留；评分有效性审计前的 report/summary 另存为 `*.pre-validity-audit.json`，审计说明写入 `evaluation_audit`。
- Mac mini 完整回归：290 passed、3 skipped；判分有效性修正后的相关回归：24 passed。

通过现有 8768 SSH 转发回放：

- [员工报销轨迹](http://127.0.0.1:8768/ultrafast?run=saas-saas-bench-business_023-1790671866183043000)
- [离职结算轨迹](http://127.0.0.1:8768/ultrafast?run=saas-saas-bench-business_031-1790671951302494000)

复现命令（输出目录必须未使用）：

```bash
cd /Users/octopusz/CodeProjects/Jev-LongSeq
bash scripts/saas-bench.sh benchmark --saas-agent jev-ultrafast \
  --saas-task-ids business_023 business_031 --max-seconds 600 \
  --output runs/saas-ultrafast-new-run
```

`business_031` 在该固定上游版本/镜像组合下会因评分器字段不匹配而标为无效；修复或更换版本后的结果应作为不同评测配置单独记录。
