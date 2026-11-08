# Docker 部署

使用 Docker Engine + Compose v2（支持 `up --wait`）和 Linux 容器。容器运行一个
Python Web 进程，内部监听 `0.0.0.0:8765`。不需要单独部署数据库；当前版本仍是
SQLite、共享工作区、单实例服务，不支持将多个副本连接到同一运行数据卷。

## 首次启动

在仓库根目录执行：

```sh
cp .env.example .env
mkdir -p data
```

Windows PowerShell 对应命令为 `Copy-Item .env.example .env` 和
`New-Item -ItemType Directory -Force data`。

将现有项目的 **整个 `data/` 目录内容**复制到服务器的 `data/` 下，保留中文名称和层级。
已有外部素材目录时，也可以在 `.env` 中将 `RFQ_DATA_DIR` 设置为该目录的绝对路径。
例如 Windows 本地验证可用 `F:/Codex/MatrixNEXT/rfq-agent/data`。
素材不在 Git 和镜像里，单独复制源码不会带上价格表、模板或案例。

```text
data/
  询价信息素材/
    价格资源表_演示数据.xlsx
    内部物料成本表_演示数据.xlsx
    报价单样例.docx
    1/ ...原始邮件及附件...
    2/ ...原始邮件及附件...
```

Linux 上这些目录需允许容器 UID/GID `10001:10001` 遍历和读取，素材以只读方式挂载。
目录必须存在；路径错误时 Compose 会报错，不会静默创建空目录。
可先用空目录启动检查页面，但缺少价格表时不能完成正常报价，不能把页面健康视为业务就绪。

按需填写 `.env` 中的 `GENESIS_MODEL`、`GENESIS_ACCESS_TOKEN`、`GENESIS_BASE_URL`。
不填模型配置仍可使用确定性解析和按钮流程。`.env` 不进入镜像或 Git。
`RFQ_DEMO_APPROVAL` 默认 `0`，演示批准必须显式开启，不能视作正式审批权限。

```sh
docker compose config --quiet
docker compose up -d --build --wait
docker compose ps
docker compose logs --tail=100 rfq-agent
```

服务器本机访问 `http://127.0.0.1:8765/`。通过 SSH 临时查看可使用：

```sh
ssh -L 8765:127.0.0.1:8765 user@server
```

然后在自己电脑打开同一地址。不要输出或分享完整的 `docker compose config`，其内容会展开密钥。

## 接入线上入口

服务当前没有用户登录和权限隔离，所有调用者共享任务与上传文件。默认只发布到宿主机
`127.0.0.1`；在同一宿主机的反向代理上配置 **HTTPS 和登录认证/访问控制** 后再提供线上入口。
如果代理本身在容器中，应将其接入同一个 Docker 网络，转发到 `rfq-agent:8765`。
不要直接把 `RFQ_BIND_HOST` 改成 `0.0.0.0` 暴露给公网。

代理需覆盖所有页面、API 和文件下载路径，并设置上传大小限制与请求超时
（模型处理可能超过一分钟）。域名、证书和身份验证由实际部署平台配置。
本 Compose 不自动创建这些外部资源。当前标准库 HTTP 服务适用于受控的单实例使用，
多租户或高并发部署需要另行改造。

## 持久化与验证

| 内容 | 容器路径 | 保存方式 |
|---|---|---|
| 原始参考素材和报价模板 | `/app/data` | 宿主机目录，只读挂载 |
| SQLite、任务、上传邮件、导出文件 | `/app/runtime` | Compose 命名卷 `rfq-agent_runtime` |
| 模型配置 | 进程环境变量 | 由宿主机 `.env` 注入 |

容器以非 root 用户运行，根文件系统只读；`runtime` 卷和 `/tmp` 可写。
健康检查访问首页，仅检查 HTTP 存活，不调用模型、不创建任务。
部署后在页面检查素材状态，处理一封邮件并导出草案，再执行
`docker compose up -d --force-recreate --wait`，确认任务及导出仍然存在。
聊天会话保存在内存中，重建容器后需开启新对话。

`docker compose down` 保留数据卷；**`docker compose down -v` 会删除运行数据**。
项目名或 `-p` 参数变化会使用另一套卷，请在升级时保持不变。
Docker 不会仅因 `unhealthy` 自动重启容器；`restart: unless-stopped` 处理进程退出，
健康异常应通过平台监控处理。

## 升级、备份和恢复

升级代码后，在同一部署目录执行：

```sh
docker compose build --pull
docker compose up -d --wait
```

更新依赖时重新生成锁定文件并验证构建：

```sh
uv pip compile pyproject.toml --extra documents --python-version 3.12 --generate-hashes --output-file requirements-docker.txt
```

依赖版本和下载哈希已锁定；Python 基础镜像标签仍可接收补丁更新。
如需跨服务器部署相同构建，可将已验证镜像推送到自己的镜像仓库，设置 `.env` 中的
`RFQ_IMAGE` 为版本标签或 digest，再执行 `docker compose pull` 和
`docker compose up -d --no-build --wait`。

为取得一致的 SQLite 和任务文件备份，先停止写入，再复制整个运行目录：

```sh
docker compose stop rfq-agent
mkdir -p backups/runtime
docker compose cp rfq-agent:/app/runtime/. ./backups/runtime/
docker compose start rfq-agent
```

每次备份应使用新的空目录，避免旧文件混入。另行备份 `data/` 和 `.env`，限制备份访问权限。
Windows 可用 `New-Item -ItemType Directory -Force backups/runtime` 创建目录。

恢复到同版本应用的**新空卷**（或经确认可覆盖的卷）：先 `docker compose create`，
在服务停止时复制内容并修正属主，随后启动：

```sh
docker compose cp ./backups/runtime/. rfq-agent:/app/runtime/
docker compose run --rm --no-deps --user 0 --cap-add CHOWN --cap-add DAC_OVERRIDE rfq-agent chown -R 10001:10001 /app/runtime
docker compose up -d --wait
```

这里的备份/恢复针对本 Docker 部署，容器内路径始终为 `/app`。
原 Windows 本地 `runtime/` 含有绝对路径记录，不应直接当成跨系统迁移备份；
本方案默认创建新运行卷并复用原始素材，历史任务迁移需单独处理路径映射。

配置参考：[Docker 构建指南](https://docs.docker.com/build/building/best-practices/)及
[Compose 服务配置](https://docs.docker.com/reference/compose-file/services/)。
