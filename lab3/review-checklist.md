# 提交前复核记录

复核日期：2026-11-11。复核人：诸葛皓昀。

| 复核事项 | 解释、发现或修改 | 日期 |
| --- | --- | --- |
| 从干净环境安装并构建成功 | 复核 `evidence/build.txt` 与 `environment.json`：`pip install -e`、`verify.py` 中 Ruff、格式检查、测试、compileall、build 和 demo 各步退出码为 0；本机以 `PYTHONPATH=src` 重跑 42 项测试通过。 | 2026-11-11 |
| 亲自走通发布、审核、预约、取消和完成 | 运行 `tools/demo.py` 在隔离数据库走通：卖家草稿→送审→管理员通过→买家搜索/预约/取消→重新预约→卖家确认→买家完成→重放。重放结果 `replayed=true`，统计为 1 笔成交、记录金额 9950 分，`check` 无不变量错误。 | 2026-11-11 |
| 解释为何只允许一条有效预约及数据库如何保证 | 同一商品在待确认或已确认状态下只能有一条预约。数据库建立 `one_active_reservation` 部分唯一索引，仅覆盖 `pending`、`confirmed`；写入使用 `BEGIN IMMEDIATE` 串行化，服务层同时检查活动预约、商品状态和版本。并发测试 `test_concurrent_buyers_one_winner` 验证一胜一败。 | 2026-11-11 |
| 解释原结果与当前状态、重放与新请求的区别 | `requests` 以“用户 + 操作 + 请求标识”为主键，保存请求指纹与首次结果。重放先重新鉴权，再比对指纹，返回 `original_status` 并重读 `current_status`；新请求使用新标识，重新执行状态与版本校验。重复完成不会增加成交笔数。 | 2026-11-11 |
| 解释为何到期释放必须独立提交 | `expire()` 在独立事务中扫描到期的待确认预约，关闭预约并把商品恢复为已发布；随后用户操作再开启新事务。独立提交保证到期状态先落库，后续确认、取消或新预约都基于最新状态判断，不会因本次业务失败而回滚已到期的事实。 | 2026-11-11 |
| 对照实验二状态机检查至少一条正常和一条禁止路径 | 正常路径：draft→pending_review→published→reserved→trading→sold。禁止路径：published 直接 complete、reserved 直接 take_down、confirmed 在到期后 confirm，均返回业务错误且商品与预约保持不变。 | 2026-11-11 |
| 阅读并确认本地化方案及待确认项实验策略 | 采用本机 CLI、SQLite 本地事务和 outbox；待确认项按 README 的实现策略执行：联系方式自愿维护、封禁停止业务、到期先超时再处理后续动作、请求结果不自动清理、失败请求不写成功去重记录、审核复查分类启用、归档草稿不再编辑。 | 2026-11-11 |
| 阅读故障注入测试，确认没有把异常当成成功 | 阅读 `test_audit_insert_failure_rolls_back`、`test_audit_failure_rolls_back_business`，并在测试中注入审计写入失败；断言商品和预约均未改变。`Core.mutate` 捕获异常后写失败审计并抛出 `BusinessError`，不返回成功结果。 | 2026-11-11 |
| 检查远程分支是否实际可访问 | 对照 `evidence/git.txt`：实现提交 `ceb8ecf`、打包提交 `431cba5`，分支为 `codex/lab3-software-implementation`；本地 `git log` 与分支名一致。远程页面地址见报告第 5 节。 | 2026-11-11 |
| 记录本人对模型建议的接受、拒绝或修改理由 | 接受模块化单体、部分唯一索引、`BEGIN IMMEDIATE`、outbox 和请求结果表。修改 2.1 节 `ItemReview` 的说明，使其覆盖送审、撤回、审核、下架、归档和整改。拒绝把 `reviews` 仅用于审核，因为代码实际把它作为商品操作留痕。 | 2026-11-11 |
