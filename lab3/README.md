# 实验三：软件实现与构建

诸葛皓昀 · 241220153 · 二手交易系统

基于实验二 UML，实现面向本机使用的 Python 命令行应用。普通账号同时承担买家、卖家角色，管理员只管理。预约不涉及线上付款，付款、见面和交付在线下协商。

## 安装、构建与验证

在本目录打开 PowerShell，要求 Python 3.11 或以上：

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python tools/verify.py
.venv/Scripts/python -m secondhand shell
```

验证入口顺序执行 Ruff、格式检查、unittest、compileall、PEP 517 构建、模拟业务演示和 CLI 启动。任何一项失败立即退出；真实输出及退出码写入 `evidence/`。`python -m build` 输出 sdist 和 wheel 到 `dist/`，可用 `python -m pip install dist/nju_secondhand-1.0.0-py3-none-any.whl` 安装。

Python 是解释执行语言，`compileall` 检查并生成字节码；`build` 才是本项目采用的标准发布构建工具。Ruff 同时负责风格检查与格式化；Pylint 不在本项目工具链内。

## 从零操作

```powershell
python -m secondhand --db data/secondhand.db shell
```

以下命令在 `secondhand>` 提示符内输入，密码另行隐藏输入。用户名、密码由自己设置，无内置部署密码。示例数字只适用于空数据库；每次以实际输出的编号、`current_version` 或详情中的 `version` 为准。

```text
bootstrap admin --nickname 管理员
login admin
category 数码
logout
register seller --nickname 卖家
register buyer --nickname 买家
login seller
draft --title 机械键盘 --description 功能正常 --price 99.50 --category-id 1 --condition 九成新 --location 学校南门
image 1 2 "E:/photos/keyboard.png"
item-action 1 3 submit
logout
login admin
item-action 1 4 approve
logout
login buyer
browse --keyword 键盘 --minimum 90 --maximum 100
reserve 1 5
reservation 1
logout
login seller
trade 1 1 confirm
logout
login buyer
trade 1 2 complete
messages
logout
login admin
stats 2026-01-01 2026-12-31
audit --operation complete --result success
check
exit
```

图片必须换为自己的真实 PNG/JPEG 文件；路径用正斜杠。首个完整草稿保存返回版本 2，添加图片、送审、审核依次使版本递增。预约的版本独立计数，从 1 开始。

查找→预约→取消的简短练习：买家执行 `browse`，再按实际商品编号和版本执行 `reserve`，用返回的预约编号执行 `trade <预约编号> 1 cancel`。查看 `help`、`browse --help`、`trade --help` 获取其他参数。

## 请求重试和权限

每次写入前输出请求标识。需要重试时，在命令前提供原标识，并保持参数完全一致：

```text
--request-id my-reserve-001 reserve 1 5
--request-id my-reserve-001 reserve 1 5
result reserve my-reserve-001
```

返回值分开显示 `original_status`、`current_status`、`replayed`。同标识不同内容会被拒绝，旧请求不会影响商品后续新预约。旧页面版本冲突后先查询详情，再由操作者确认新动作。登录失效后须重新登录和刷新，不自动执行原交易命令。不要把密码或会话令牌写入命令行脚本、报告或 Git。

## 功能与源码

| 实验二对象/组件 | 实现位置 | 行为 |
| --- | --- | --- |
| User、AdminInvite | `accounts.py`、`security.py` | 注册、登录、封禁、受控邀请、密码哈希 |
| Category、Item、ItemImage、ItemReview | `catalog.py` | 不完整草稿、图片解码、审核、版本校验、分类、整改 |
| Reservation、ReleaseConfirmation | `trading.py` | 预约七状态、双方同意解除、违规同步关闭、24小时到期 |
| 幂等、统一鉴权、审计、消息 | `core.py` | 原结果持久化、鉴权后重放、同事务审计、outbox |
| 查询和统计 | `queries.py` | 交集查询、20条分页、精确金额、北京时间日期 |
| 数据存储 | `database.py` | 外键、有效预约部分唯一索引、事务、不变量检查 |
| 客户端/运维 | `cli.py`、`operations.py` | 内存会话、30秒维护、在线备份 |

`tests/` 使用隔离临时数据库进行集成测试；`tools/demo.py` 是模型编写的用户操作模拟脚本，可独立运行，不修改真实数据。`evidence/source-counts.json` 只计算 `src/`，排除测试、工具、空行、注释及 docstring；未使用 GUI 设计工具。

## 实现取舍与边界

- 保留实验二的模块化单体和状态机，用 SQLite 本地事务代替设计中的服务器数据库，以命令行代替 Web UI。模块通过 Application 组合，服务组共享受控数据层；未实现原设计严格的跨模块独立仓储接口。
- 金额使用整数分；分类、标题和价格在预约时保存快照。已售出商品被标记违规仍计成交，公开隐藏。
- 活跃预约唯一索引配合 `BEGIN IMMEDIATE` 串行写入及版本检查；适合本机实验，并非多机服务方案。
- `shell` 存活时每30秒运行到期释放、outbox投递和按UTC自然日备份。没有启动客户端时不在后台运行；重新启动或访问有关数据时补处理。备份不自动删除，保留至少7份需持续运行或自行调度。可用 `backup` 手动备份。
- 恢复时先退出所有客户端，将备份文件复制到一个新的数据库路径，再用 `--db` 启动并执行 `check`。不要覆盖运行中的数据库，也不要只复制正在使用的主库而忽略 WAL。
- 原待确认事项采用明确实验策略：联系方式自愿维护；不允许封禁账号继续业务；封禁卖家暂停新预约；到期后先超时再处理取消/拒绝；请求结果不自动清理；失败请求不写成功去重记录；审核时再次检查分类启用；归档草稿不再编辑；管理员凭据沿用普通格式。以上为实现决策，报告中按实现策略表述。
- 本地程序没有网络传输，未实现 HTTPS、浏览器响应超时提示、Web 断线表单恢复或响应式界面。未执行原需求的 1万商品/20会话浏览器性能验收、5人真实用户实验或灾难恢复 SLA 验收。
- 审计本地同库提交，无独立日志服务；磁盘写入失败时不报告成功。失败业务校验用独立事务留痕，完全不可写的数据库无法保存失败证据。日志没有自动清理或编辑接口。
- 本实验程序假设数据库、备份和本机操作系统可信；持有数据库文件的人拥有本地存储访问能力。

## 大模型辅助与个人复核

`ai-log.md` 保存本次使用方式、设计决策与纠错证据；`review-checklist.md` 记录逐项复核结果。提交前已按该表完成代码走读、流程验证和文档修订。

Git 操作与远程状态见 `evidence/git.txt`。本实验只提交 `lab3/` 新增内容，不改动其他实验的未提交工作。
