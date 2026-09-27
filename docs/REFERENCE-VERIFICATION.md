# 本次验证记录

日期：2026-09-27。

## 实际执行范围

Python 3.13.5；Playwright 1.57.0；Chromium 144.0.7559.96 built on Debian GNU/Linux 13 (trixie).
真实 Chromium，离线 HTML/JavaScript 网页沙盒，规则策略＋手写子任务调度器；没有请求真实 Jev 或生成式模型。

12 项测试通过：9 项实际浏览器测试＋3 项纯协议测试。完整输出见 test-report.txt。

| 运行 | 记录数 | 模式 | 弹窗 | 原子调用 | 严格成功 |
|---|---:|---|---|---:|---|
| verified-flat | 12 | flat | True | 34 | True |
| verified-long | 32 | hierarchical | False | 94 | True |
| verified-popup | 12 | hierarchical | True | 34 | True |
| verified-small | 4 | hierarchical | False | 10 | True |

所有运行的环境判分：无违规、无重复保存、100% 详情访问覆盖。最长一条包含 94 次原子调用与 95 次决策循环（含 finish）。

**这些数字仅说明代码按预定规则完成了合成测试，不能用于声称 Jev 长任务能力、分层架构提升或模型成本优势。**

## 未验证或未实现

- 本地 HTTP 模式：受管 Chromium 返回 ERR_BLOCKED_BY_ADMINISTRATOR，未修改管理策略；改用无网络的 inline fixture。
- 真实 Jev API 调用、真实 LLM/VLM 规划器、BrowserGym/Browser Use 接口、公共 benchmark：未执行。
- 多标签、iframe、shadow DOM、canvas、视觉定位、自动未知写入回查：尚未实现。
- 规则 baseline 对 flat/hierarchical 的相同表现只是烟雾测试，不用于架构效果推论。

轨迹、最终截图和 Playwright trace 位于 runs/verified-*。
