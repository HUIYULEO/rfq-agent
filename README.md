# MatrixNEXT RFQ Agent

状态：可离线运行的框架骨架。Python 3.11+，已在本机Python 3.12验证。

## 运行

在本目录执行（无需安装依赖或配置密钥）：

```powershell
python -m rfq_agent.cli demo --input examples/scaffold.json
python -m unittest discover -s tests -v
```

输出位于 `runtime/<task_id>/`，包含任务状态、事件、字段修订和JSON草案。示例为明确标注的合成数据，不是官方案例验收结果。默认价格适配器返回待复核和null价格，不生成虚构报价。

## 分层

```text
cli.py                  本地入口
agent.py                Deep Agents + Genesis适配
service.py              五个业务动作、顺序检查、revision管理
models.py               RFQ、行项目、任务结构
ports.py                文档解析/报价适配器契约
storage.py              单进程任务持久化
examples/               离线框架样例
tests/                  状态、修订、无价和持久化测试
```

## Genesis模式（尚未实测）

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[agent]"
$env:GENESIS_BASE_URL = "https://token.moi.matrixorigin.cn/v1"
$env:GENESIS_MODEL = "控制台实际模型ID"
# GENESIS_ACCESS_TOKEN通过本地环境变量设置，不提交到仓库或聊天
.\.venv\Scripts\python -m rfq_agent.cli agent --input examples/scaffold.json
```

`.env.example`用于说明配置，本版不会自动读取.env。Agent适配采用官方 `create_deep_agent(model=..., tools=...)` 接口；依赖范围是待联调约束，未安装或生成锁文件。联网依赖安装和真实工具调用通过后锁定实际版本。设置了模型请求超时、有限重试和图步数上限。

当前子Agent禁用仅由提示词约束；正式接入时须按实际安装版本配置工具白名单/关闭委派，不能将提示词当权限控制。未提供shell或本地文件系统backend。

## 下一步

1. 实现DocumentParser：官方EML/PDF/XLSX到统一RFQ，Buyer C优先。
2. 实现QuoteProvider：读取官方成本/供应商表，返回有证据的过滤及计算结果。
3. 以DOCX模板替换框架JSON导出，支持动态行数。
4. 实测Genesis与Deep Agents，锁定依赖，再接MOI或Web入口。

当前不包含Web界面、HTTP API、MOI资源配置、正式DOCX生成或真实定价。存储只支持单进程顺序调用，不适用于并发部署。旧revision产物保留为审计文件，当前任务不再引用；将来下载接口必须校验revision。

方案与进度见父目录的 RFQ_SOLUTION_DESIGN.md、RFQ_REQUIREMENTS_BACKLOG.md、RFQ_DEVELOPMENT_PLAN.md。

API参考：https://docs.langchain.com/oss/python/deepagents/quickstart
