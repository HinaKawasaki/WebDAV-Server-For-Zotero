# Changelog

## v2.4 — 2026-10-24

一次集中发布：可用性、性能、健壮性与运维护栏。

### 可用性

- 登录节流不再"先拦后验"：**口令正确时永远放行并清零失败记录**。隧道/VPN 后面所有
  客户端共用 `127.0.0.1`，节流 key 退化成"按账号"，旧行为下任何人喂几次错误口令就能
  把别人的账号长期按在 401。猜错仍然照常校验并返回 401 + `Retry-After`；防爆破依赖
  每次尝试的 scrypt 成本、强口令和隧道/VPN 的访问控制。
- 节流状态有界：`WEBDAV_LOGIN_MAX_KEYS`（默认 4096）限制 key 数，每 key 最多 64 个
  失败时间戳；超限优先丢弃失败次数最少的 key，被攻击的账号保留历史。
- 数据库不可用时返回 `503 Service Unavailable` + `Retry-After`，不再伪装成 401。
- 路径里的 NUL 字节返回 400（以前会 500 并回显 `ValueError('embedded null byte')`）。
- 流式响应中途的 `OSError`/`ValueError`：首块已发出时记录日志并安全截断响应体，
  不再把异常抛成 traceback；首块之前仍转换成正常的 4xx/5xx DAV 响应。

### 性能

- `server/db.py`：数据库连接**按线程复用**（cheroot 固定线程池），空闲超过 10 秒才
  `ping`；连接级错误（2006/2013/2055）丢弃重连，业务级错误（1062 等）保留连接；
  新增 `db.close_pooled()`。连接数约为工作线程数（默认 20）。
- `server/davstate.py`：`PROPFIND`（`Depth: 1` / `infinity`）的死属性改为一次
  `LIKE '<集合前缀>%' ESCAPE '!'` 前缀查询 + 请求内缓存，`Depth: 1` 用
  `LOCATE/SUBSTRING` 只保留直接子项；前缀里的 `%`、`_`、`!` 都会被转义。
- 锁持久化改为**增量写**：与上次落库快照 diff，只 upsert/delete 变化的行，整批一个事务。
  过期锁与悬空 `URL2TOKEN` 清理也只写涉及的行；两个实例同时写不再互相覆盖对方的行
  （锁的互斥语义仍按单实例设计）。
- 实测（RTT≈28 ms 的转发库）：

  | 场景 | 改动前 | 改动后 |
  | --- | --- | --- |
  | 单次属性查询（新建连接 → 复用连接） | 165 ms | 28.6 ms |
  | 21 个资源的 `Depth:1 allprop` | 3.46 s | 0.029 s |
  | 一次 LOCK（表里已有 102 把锁） | 2.99 s（103 条语句） | 0.117 s（2 条语句） |

### 健壮性与安全

- Basic 头解析：支持无 padding、ISO-8859-1、制表符/连续空格/首尾空白、折行（obs-fold）
  的 token，解码前去掉空白并重新生成规范 base64；无法解析时按匿名处理（干净 401）。
- `users.add_user` 的重复用户只认 MySQL/MariaDB 错误码 **1062**，不再匹配 "Duplicate
  entry" 文本；其他数据库错误原样抛出。
- 认证缓存的失效世代文件改为**随机 token 内容**（原子写入），每次校验读取比较，
  不再依赖 mtime：粗粒度时间戳、保留 mtime 的同步/恢复、时钟回拨都不会漏掉改密。
- 口令策略：新口令至少 **12 字符**、不得与用户名相同、不得是单一字符重复；
  `users.add_user` / `set_password`（含 `manage adduser/passwd`）执行，登录时的旧哈希
  自动升级用 `enforce_policy=False` 绕过，老口令继续可用。
- 单实例护栏：`<SHARE_DIR>/.server.lock` 上非阻塞 `flock` + 写入 PID；第二个服务进程
  以退出码 1 结束并提示锁文件与 PID。锁由内核在退出时释放；`manage` 不受影响。
- DAV 状态表建表失败时降级为内存实现并告警，不再因 1059/1071 之类的 DDL 错误挡住启动。
- `tools/backup.sh` 把 `dav_properties`/`dav_locks` 与用户表一起 dump（存在才 dump），
  并容忍 `tar` 在活文件变化时返回 1。

### 文档

- README 更新节流语义、503、`WEBDAV_LOGIN_MAX_KEYS`、连接池/批量读/锁增量、口令策略、
  单实例锁；删除"死属性/锁不跨重启保留"的过时说明；`passwords.py` 注释 63 → 56。
- `docs/DEPLOYMENT.md` 的备份/恢复命令同步为三张表。

### 测试

- 全套 **215** 项单元测试通过，`ruff check` 干净，Python 3.9 语法检查通过。
- 真实 MariaDB 11.8：用户表 9/9、完整集成 21/21、锁增量 6/6、批量读 9/9；
  `tools/prod_smoke.py` 48/48（真实 cheroot + 真实库）。

## v2.03.1 — 2026-10-16

日志降噪：把两条"正常状态却报 WARNING"的消息降为 INFO，让
`journalctl -p warning` 重新只表示"需要处理的事"。只影响日志级别，行为不变。

- `users.ensure_schema`：账号没有 `CREATE` 权限但表已存在时记 INFO（这正是部署
  文档 §9 推荐的刻意最小权限配置）；表确实缺失时仍然报警并给出可执行的补救
- `users.check_schema`：`password VARCHAR(64)` 这种"放得下但偏紧"的提示改放进新的
  `notes` 列表，`prepare()` 以 INFO、`manage initdb` 以 `[*]` 输出；真正需要处理的
  状态仍走 `warnings`（WARNING / `[!]`）
- 保留为 WARNING 的一条：`WEBDAV_HOST` 不是回环地址（Basic 凭据明文传输，安全相关）

## v2.03 — 2026-10-16

DAV 状态落库：死属性（`PROPPATCH`）与锁存进 MariaDB，**重启不再丢**。

### 新增

- `server/davstate.py`：只替换 WsgiDAV 的存储层，**锁与属性的策略代码仍用上游实现**
  - `MariaDBPropertyManager`（继承 `PropertyManager`）→ 属性存 `dav_properties`
  - `MariaDBLockStorage`（继承 `LockStorageDict`）→ 锁存 `dav_locks`；锁字典里的
    `owner` 是 bytes，用**显式标记的 JSON 编码**而不是 pickle（DB 里可读，也不会反序列化
    不可信数据）；启动时自动清理过期锁
  - URL 以 sha256 作为主键前缀，规避 utf8mb4 索引长度上限；属性值原样保存
- 表结构由 `ensure_schema` 在启动时按需创建，`manage initdb` 也会创建并打印状态
- `WEBDAV_PERSIST_PROPERTIES` / `WEBDAV_PERSIST_LOCKS` / `WEBDAV_PROPERTIES_TABLE` /
  `WEBDAV_LOCKS_TABLE` 可调

### 设计取舍

- **可选特性，绝不拖垮服务**：应用账号没有 `CREATE` 权限或表尚未创建时，服务照常启动，
  日志里大声告警并回退到内存实现（行为与 v2.02 完全一致），不会进入重启循环
- **不做级联删除**：WsgiDAV 在 MOVE/DELETE 时逐个处理子项，父目录若级联删除会让它遍历到
  已消失的行（实测踩到的 bug）；孤儿行可借助 `ref_url` 列排查
- 只要求 `SELECT/INSERT/UPDATE/DELETE`，维持最小权限

### 工程

- 新增 `tests/test_davstate.py`（33 项）：重启后属性/锁仍在、copy/move/remove 语义、
  过期锁不被复活、SQL 语句形状、无 `CREATE` 权限时的降级，以及**穿过完整 WSGI 栈的
  端到端用例**（PUT → PROPPATCH → 重建 app → PROPFIND 仍能读回属性）
- 全套 160 项测试通过

## v2.02 — 2026-10-16

性能与可用性：认证结果缓存、登录节流改为指数退避、运维脚本与 CI 收口。

### 性能

- 新增认证结果短 TTL 缓存（`WEBDAV_AUTH_CACHE_SECONDS`，默认 45 秒）：命中即跳过
  数据库往返与 scrypt 校验。本地基准单请求 **97 ms → 0.7 ms（131×）**；生产实测
  认证开销约 46 ms/请求，Zotero 的同步突发里绝大多数请求都能命中。
  只缓存成功、键是口令的 HMAC（进程内随机密钥）、容量有上限（`WEBDAV_AUTH_CACHE_MAX_ENTRIES`）。
- 世代文件（`WEBDAV_AUTH_CACHE_EPOCH_FILE`）：`manage adduser/passwd/deluser` 会 touch 它，
  运行中的服务器随即丢弃全部缓存——**改密立即生效**，不必等 TTL 到期。

### 可用性（登录节流）

- 从"锁定整个观察窗口（默认 5 分钟）"改为**指数退避**：超过阈值后每次失败的等待以
  `WEBDAV_LOGIN_BASE_PENALTY`（默认 2 秒）起步翻倍，封顶 `WEBDAV_LOGIN_MAX_PENALTY`
  （默认 300 秒）。手抖敲错几次口令只付出几秒代价，持续爆破仍会被拖慢。
- 被节流时在 401 响应上返回 `Retry-After`，客户端可以据此退避。
- 认证成功（含缓存命中）会清空该 key 的历史失败，避免一个客户端把账号长期拖在惩罚里。
- `WEBDAV_LOGIN_MAX_ATTEMPTS=0` 可完全关闭节流。

### 工具

- 新增 `tools/backup.sh`：备份 `dav_users` 表与存储目录，支持 `archive/rsync/skip`
  三种数据模式与保留份数；口令不进进程列表（临时 `my.cnf` 权限 600）。
- 新增 `tools/ops_check.sh`：服务 / 端口 / HTTP / 数据库 / 磁盘水位巡检，退出码
  0=正常 1=告警 2=严重，`--quiet` 只在异常时输出，可接 cron 或 systemd timer。
  它按 `.env` 的 `WEBDAV_HOST` 推导探测地址（通配地址回落到回环）。
- 新增 `tools/lib.sh`：上面两个脚本共用的 `.env` 解析（环境变量优先）。

### 工程

- 新增 `.github/workflows/ci.yml`：Python 3.9 / 3.12 / 3.14 跑全部单元测试，外加
  `ruff check`；新增 `ruff.toml` 并修掉全部 lint 问题。

### 文档

- `docs/DEPLOYMENT.md` 补充：`.env` 口令必须是纯 ASCII **且长度与数据库一致**、
  `1045`/`1129`/`start-limit-hit` 的处置、以及"让客户端直接走 WireGuard"的部署方式。

## v2.01 — 2026-10-15

首个上生产的重构版本：安全与可靠性修复 + 每用户独立存储。

### 安全

- 口令改为带随机 salt 的 `scrypt`；编码固定 56 字符，适配历史遗留的
  `dav_users.password varchar(64)`，**无需 ALTER 表**
- 兼容并自动升级旧格式：无盐 SHA-256（`sha256$…` 与 64 位裸十六进制）与明文
- 口令比较使用常量时间函数；新增按「用户名 + 来源地址」的登录失败节流
- 删除硬编码数据库凭据与内置 `admin/admin` 默认账号，配置改由 `.env` / 环境变量驱动
- 服务端默认只监听 `127.0.0.1`（Basic 凭据的机密性交给 SSH 隧道）
- 新增 `RequestGuard` 中间件：无法解析的 `Authorization` 头按匿名处理（干净 401），
  文件系统 `OSError` 转为 414 / 507 / 403 / 409，异常细节只进日志；同时关闭 WsgiDAV /
  Python 版本横幅——不再出现「500 + 异常原文 + 框架版本」的响应

### 存储布局

- 每个账号拥有独立目录 `<SHARE_DIR>/<用户名>/`，对外映射为 `/<用户名>/`
- 跨用户访问、`..` 逃逸、编码绕过、符号链接一律 403
- 锁与死属性因 URL 前缀不同而天然按用户分桶，避免共用根目录时的跨用户串扰
- 新增 `manage.py`：`initdb` / `adduser` / `passwd` / `deluser --purge` / `listusers` /
  `adopt-shared`（旧共享根目录迁移，支持 `--copy` 保留源目录、默认 dry-run）

### 可靠性

- 数据库访问改为即用即关的连接上下文，修正连接泄漏
- 区分「认证失败」与「后端故障」，后者 fail closed 并记录明确原因
- 数据库账号无需 `CREATE` 权限即可启动；启动与 `initdb` 自检密码列宽，必要时打印
  可直接执行的 `ALTER TABLE`
- 修复 `SIGTERM`/`SIGINT` 无法停止服务的问题（在信号处理函数内调用 cheroot 的
  `stop()` 会与内部锁自死锁）
- 客户端隧道改为指数退避自动重连并支持密钥认证，不再因一次网络抖动退出

### 工程化

- 固定依赖版本；新增 `.gitignore`、`.env.example`、`tools/dev_db_tunnel.sh`、
  `tools/prod_smoke.py`；`Lisence` → `LICENSE`；重写 Readme；新增上线 Runbook
- 108 项单元测试；47 项生产冒烟检查（`tools/prod_smoke.py`）

### 已知限制

- 数据库口令必须是**纯 ASCII**：PyMySQL 以 latin-1 编码口令，非 ASCII 会直接报
  `'latin-1' codec can't encode characters`
- 附件下载速度受云主机出网带宽限制，与代码无关；生产实测出网约 4 Mbps（~0.5 MB/s），
  入网约 10–20 MB/s
