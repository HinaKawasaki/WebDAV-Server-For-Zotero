# Secure WebDAV Tunnel for Zotero

为 Zotero「文件同步」准备的私有 WebDAV 服务：服务端是 WsgiDAV + cheroot，用户存放于
MariaDB/MySQL；客户端通过 SSH 隧道把远端 WebDAV 映射到本机回环地址，Zotero 只与本机通信。

**每个账号拥有独立目录和独立的 URL 前缀**（Nextcloud 风格），互不可见：

```
<WEBDAV_SHARE_DIR>/alice/...   <--  http://host:8000/alice/
<WEBDAV_SHARE_DIR>/bob/...     <--  http://host:8000/bob/
```

## 架构

```
Zotero ──HTTP──▶ 127.0.0.1:<LOCAL_PORT>/<user>/ ──SSH 隧道──▶ <服务器>:<REMOTE_PORT>/<user>/ ──▶ WsgiDAV
                                                                                                  │
                                                                                            MariaDB/MySQL
```

服务端默认只监听 `127.0.0.1`。HTTP Basic 凭据只是 Base64，**安全性完全依赖 SSH 隧道**，
因此不要把 WebDAV 端口直接暴露到网络上；确实需要时请在前面加 TLS 反向代理。

## 目录结构

```
server/           WebDAV 服务端
  conf.py         .env / 环境变量配置
  db.py           MariaDB/MySQL 访问层（PyMySQL）
  users.py        用户表读写与认证
  passwords.py    scrypt 哈希与旧格式兼容
  throttle.py     登录失败节流
  storage.py      每用户目录命名规则
  provider.py     把每个用户限制在 /<username>/ 的 DAV provider
  migration.py    旧共享根目录 -> 单用户目录的迁移
  auth.py         WsgiDAV DomainController
  app.py          WsgiDAV 应用工厂
  start_server.py 启动入口
  manage.py       用户管理与迁移 CLI
client/           隧道客户端
  client_conf_example.py  配置模板（复制为 client_conf.py）
  sshtunnel_client.py     自动重连的端口转发
tools/            开发辅助脚本
tests/            unittest 测试
docs/             上线 Runbook（docs/DEPLOYMENT.md）
```

## 环境要求

* Python 3.9+（实测 3.14）
* MariaDB 10.x / MySQL 8.x
* 客户端所在机器能通过 SSH 登录服务器

## 服务端部署

### 1. 准备数据库

```sql
CREATE DATABASE IF NOT EXISTS webdav CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
CREATE USER IF NOT EXISTS 'webdav'@'127.0.0.1' IDENTIFIED BY '换成强密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE ON webdav.dav_users TO 'webdav'@'127.0.0.1';
FLUSH PRIVILEGES;
```

只需要 `CREATE`（首次建表）与增删改查，不需要 `DROP` / `ALTER` / `GRANT`。

### 2. 安装依赖

```bash
git clone <repo> && cd WebDAV-Server-For-Zotero
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r server/requirements.txt
```

### 3. 配置

```bash
cp .env.example .env
$EDITOR .env      # 至少填写 WEBDAV_DB_PASSWORD
```

### 4. 初始化数据库与账号

```bash
python -m server.manage initdb --admin-user admin   # 交互式设置密码
python -m server.manage adduser alice               # 同时创建 alice 的存储目录
python -m server.manage listusers                   # 列出账号及其 URL 前缀
python -m server.manage passwd alice
python -m server.manage deluser alice --purge       # --purge 连数据目录一起删
```

`adduser` 会打印该账号应填入 Zotero 的完整 URL。**不再创建任何内置默认口令。**

### 5. 启动

```bash
python -m server.start_server
# 也可以： python server/start_server.py
```

启动时会先校验配置、创建存储根目录、探测数据库并确保表存在；任一步失败都会打印原因并以
退出码 1 结束（便于 systemd 的 `Restart=on-failure` 生效）。

### systemd 单元示例

```ini
[Unit]
Description=WebDAV Server for Zotero
After=network.target mariadb.service
Wants=mariadb.service

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/WebDAV_Server
EnvironmentFile=/home/ubuntu/WebDAV_Server/.env
ExecStart=/home/ubuntu/WebDAV_Server/.venv/bin/python -m server.start_server
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

服务器收到 `SIGTERM` / `SIGINT` 时会优雅停止 cheroot 线程池。

## 存储布局与用户隔离

* 每个账号的根就是 `<WEBDAV_SHARE_DIR>/<用户名>/`，对外表现为 `/<用户名>/`。
* 请求第一段路径与当前登录账号不符时直接 `403`，包括 `/`、`/其他用户名/`。
* `..` 段被显式拒绝，再做一次 `realpath` 前缀校验，`/alice/../bob/x` 拿不到任何东西。
* 符号链接默认关闭（`fs_dav_provider.follow_symlinks=false`），避免用软链绕出目录。
* 用户目录不存在时按需创建；`adduser` 也会预先创建。
* **锁与死属性天然分用户**：WsgiDAV 用请求 URL 作为键，而 alice 的 `/alice/x` 与 bob 的
  `/bob/x` 是两个不同的键，所以一个用户的锁不会阻塞另一个用户。
* 需要临时退回旧的"所有人共用根目录"行为时，设置 `WEBDAV_PER_USER_STORAGE=false`。

用户名会直接用作目录名，因此新建账号限定为 `[A-Za-z0-9]` 开头、由 `[A-Za-z0-9._@+-]` 组成
（最多 64 字符）。历史账号若不符合该规则（例如含空格或斜杠），仍可正常登录，其目录名会被
净化为 `name~<短哈希>`，`listusers` 会显示对应的 URL 前缀。

## 从旧的共享根目录迁移

旧版本所有账号共用根目录，切换到每用户布局后需要把已有数据归属给某个账号：

```bash
# 先停服务，然后预演（默认就是 dry run，不会改任何东西）
python -m server.manage adopt-shared admin --from ./zotero

# 确认无误后执行
python -m server.manage adopt-shared admin --from ./zotero --apply

# 想保留原目录副本（不移动）就加 --copy；目标已有同名条目需加 --force
```

目标默认是 `<WEBDAV_SHARE_DIR>/<该账号目录>/`，命令会跳过目标目录自身。

## 客户端

```bash
python3 -m venv .venv-client && source .venv-client/bin/activate
pip install -r client/requirements.txt
cp client/client_conf_example.py client/client_conf.py
$EDITOR client/client_conf.py        # SSH_PKEY 推荐；WEBDAV_USER 填你的用户名
python client/sshtunnel_client.py
```

隧道断开后会自动以指数退避（1s → 2s → 4s … 上限 60s）重连，直到你按 Ctrl+C 或发送 SIGTERM。
启动横幅会打印应填入 Zotero 的完整 URL。

Zotero 中：`编辑 / 设置 / 同步` → 文件同步方式选 `WebDAV` → 地址填
`http://127.0.0.1:<LOCAL_PORT>/<用户名>/`（注意**结尾的斜杠和用户名前缀**），用户名与密码使用
`server.manage adduser` 创建的账号。

## 本地开发：本机没有 MySQL 怎么办

生产库在服务器上，且 MariaDB 只监听服务器自己的 `127.0.0.1`，所以从笔记本直连 3306 会得到
`Connection refused`（实测确认；ufw 里放开 `172.16.1.0/24` 也没用，因为 mysqld 根本没绑到外网
地址）。用 SSH 本地转发即可，服务器上不需要改任何配置：

```bash
tools/dev_db_tunnel.sh ubuntu@<服务器> 13306
```

然后在 `.env` 里把本机开发库指向转发端口：

```
WEBDAV_DB_HOST=127.0.0.1
WEBDAV_DB_PORT=13306
```

> **授权注意**：走隧道时 MySQL 看到的连接来源是**服务器的回环地址**，所以开发账号要建成
> `'webdav_dev'@'localhost'`（或 `@'127.0.0.1'`），写成 `@'172.16.1.%'` 反而不匹配。
> 另外建议使用独立的开发库（例如 `webdav_dev`），别把测试账号写进生产表。

如果希望笔记本直连，需要在服务器上把 MariaDB 的 `bind-address` 改为 `172.16.1.1`（并授权
`'webdav_dev'@'172.16.1.%'`），这属于改服务器配置，请按需自行决定。

## 密码存储与平滑迁移

| 存储格式 | 来源 | 处理方式 |
| --- | --- | --- |
| `scrypt$n$r$p$salt$hash` | 当前版本 | 每用户随机 salt |
| `sha256$<hex>` | 旧版本标记格式 | 校验通过后自动改写为 scrypt |
| 64 位十六进制 | 上一版 `server/auth.py` | 同上 |
| 明文 | 重构前直接写库的版本 | 同上 |

迁移发生在用户**下一次登录成功时**，无需停机或写迁移脚本。仍建议用
`python -m server.manage passwd <name>` 主动轮换一遍历史口令。

salt 与哈希用 base64url（无填充），配合默认参数编出来是 **56 个字符**。

### 列宽与数据库权限（已在真实 MariaDB 11.8 上验证）

* 哈希编码刻意控制在 64 字符以内，因此现有的 `dav_users.password VARCHAR(64)`（真实生产库就是
  `username varchar(50)` / `password varchar(64)`）**不需要先 ALTER 就能直接上线**。
* 启动与 `manage initdb` 会读 `information_schema` 校验列宽：
  * 列比哈希还窄 → 立即失败，并打印可直接执行的
    ``ALTER TABLE `webdav`.`dav_users` MODIFY `password` VARCHAR(255) NOT NULL;``
  * 只是"够用但没余量"（如 VARCHAR(64)）→ 只告警，不阻断启动。
* 数据库账号可以**没有 CREATE 权限**：`CREATE TABLE IF NOT EXISTS` 被拒时会退化为"检查现有表是否
  可用"，只有表确实不存在才报错。真实联调用的就是只有
  `SELECT, INSERT, UPDATE, DELETE ON webdav.dav_users` 的账号。
* 服务端的密码编码超过列宽时会**在启动阶段**就报错，而不是等到 INSERT 时抛出
  `Data too long for column 'password'`。

## 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEBDAV_HOST` | `127.0.0.1` | 监听地址；非回环会打印告警 |
| `WEBDAV_PORT` | `8000` | 监听端口 |
| `WEBDAV_SHARE_DIR` | `<项目>/zotero` | 存储根目录（自动转绝对路径） |
| `WEBDAV_PER_USER_STORAGE` | `true` | 每用户独立目录 + `/<用户名>/` 前缀 |
| `WEBDAV_REALM` | `Zotero WebDAV Storage` | Basic 认证 realm |
| `WEBDAV_LOG_LEVEL` | `INFO` | 日志级别 |
| `WEBDAV_LOG_FILE` | 空 | 设置后额外写轮转文件日志 |
| `WEBDAV_NUM_THREADS` | `20` | cheroot 工作线程数 |
| `WEBDAV_REQUEST_QUEUE_SIZE` | `128` | 监听 backlog |
| `WEBDAV_SOCKET_TIMEOUT` | `60` | 连接超时（秒） |
| `WEBDAV_LOGIN_MAX_ATTEMPTS` | `10` | 同一用户+来源在窗口内的失败上限 |
| `WEBDAV_LOGIN_WINDOW_SECONDS` | `300` | 节流观察窗口（秒） |
| `WEBDAV_DB_HOST` | `127.0.0.1` | 数据库地址 |
| `WEBDAV_DB_PORT` | `3306` | 数据库端口 |
| `WEBDAV_DB_USER` | `webdav` | 数据库用户 |
| `WEBDAV_DB_PASSWORD` | 空（必填） | 数据库密码 |
| `WEBDAV_DB_NAME` | `webdav` | 数据库名 |
| `WEBDAV_DB_TABLE` | `dav_users` | 用户表名 |

## 测试

```bash
python -m unittest discover -s tests -v
```

覆盖：密码哈希与旧格式迁移、配置解析与校验、登录节流、用户表读写（使用假数据库）、
客户端退避与隧道参数、每用户目录命名、路径隔离与符号链接防护、共享目录迁移。

## 上线

面向现有生产实例的原地升级步骤、验证清单与回滚方案见
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

## 安全说明

* 服务端默认只监听回环地址；Basic 凭据的机密性由 SSH 隧道保证。
* 每个账号只能访问 `/<自己>/`，越权、`..` 逃逸和符号链接都会被拒绝。
* 密码使用带随机 salt 的 scrypt 存储，比较使用常量时间函数。
* 登录失败达到阈值后按「用户名 + 来源地址」临时锁定。
* SQL 全部使用参数化查询；表名经过严格白名单校验。
* 数据库不可用时认证**失败关闭**（fail closed）并记录明确的错误原因。
* 涉及 DAV「死属性」的内存存储不跨重启保留；锁同样只存在于进程内存中，单实例部署足够。
