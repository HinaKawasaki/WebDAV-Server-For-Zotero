# Changelog

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
