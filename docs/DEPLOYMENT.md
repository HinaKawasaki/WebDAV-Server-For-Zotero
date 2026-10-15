# 上线 Runbook（WebDAV-Server-For-Zotero v2）

面向现有生产实例的**原地升级**操作手册：把重构后的版本 + 每用户隔离部署到
`172.16.1.1`，并保留干净的回滚点。

> 本文里的命令都需要 `sudo`（systemd）或 `ubuntu` 用户身份执行。
> 涉及生产数据的步骤一律**先备份、再执行、后验证**。

---

## 0. 现状与目标

### 已确认的现状

| 项目 | 值 |
| --- | --- |
| 生产主机 | `172.16.1.1`（`VM-0-9-ubuntu`，腾讯云；WireGuard 地址 `172.16.1.1`，公网 `101.34.62.188`） |
| systemd 单元 | `webdav.service`（`User=ubuntu`，`After/Wants=mariadb.service`） |
| 旧部署目录 | `/home/ubuntu/WebDAV_Server`（`WorkingDirectory`） |
| 旧启动命令 | `/home/ubuntu/WebDAV_Server/.webdav/bin/python /home/ubuntu/WebDAV_Server/start_server.py` |
| 数据库 | 本机 MariaDB `11.8.6`，库 `webdav`，表 `dav_users` |
| 表结构 | `username varchar(50)` PRIMARY KEY、`password varchar(64)` |
| 现有账号 | `hello`、`hello_ubuntu`（口令字段 16 / 11 字符，**应为明文**） |
| 旧版 realm | `WebDAV Storage` |
| 对外端口 | `0.0.0.0:8000`（安全组已挡住公网直连；仅 WireGuard/VPC 可达） |
| 客户端入口 | 笔记本 `172.16.3.1` 的 `127.0.0.1:8000` 经 SSH 隧道转发到该服务 |

### 升级后的目标状态

| 项目 | 值 |
| --- | --- |
| 新部署目录 | `/home/ubuntu/webdav-server`（假设，可自行替换） |
| 新启动命令 | `/home/ubuntu/webdav-server/.venv/bin/python -m server.start_server` |
| 存储布局 | `WEBDAV_SHARE_DIR/hello/…`、`WEBDAV_SHARE_DIR/hello_ubuntu/…`，对外为 `/hello/`、`/hello_ubuntu/` |
| 认证 | 只监听 `127.0.0.1`；scrypt + 随机 salt；登录节流；旧口令首次登录自动升级 |
| Zotero URL | `http://127.0.0.1:8000/<用户名>/`（**注意结尾斜杠**） |

### 本次升级**不需要**做的事

* **不需要 ALTER 表**：新哈希编码固定 56 字符，装得进现有 `password varchar(64)`。
* **不需要给应用账号 CREATE 权限**：`CREATE TABLE IF NOT EXISTS` 被拒时会退化为"检查现有表"。
* **不需要改 MariaDB 的 `bind-address`**：应用与数据库同机，走 `127.0.0.1`。

---

## 1. 上线前必须在服务器上确认的事项

我（在笔记本侧）读不到 `/home/ubuntu`，以下四项必须在服务器上先确认，再决定具体路径：

```bash
# 1) 旧版真实的存储目录（决定要迁移哪个目录）
sudo -u ubuntu cat /home/ubuntu/WebDAV_Server/conf.py | grep -iE "SHARE_DIR|DB_"
sudo systemctl show webdav.service -p WorkingDirectory

# 2) 旧版使用的数据库账号/密码（新 .env 要么复用它，要么换成新的最小权限账号）
sudo -u ubuntu grep -iE "DB_USER|DB_PASSWORD|DB_NAME|DB_TABLE" /home/ubuntu/WebDAV_Server/conf.py

# 3) 数据规模与磁盘余量（决定用 --copy 还是 --move）
sudo -u ubuntu du -sh <SHARE_DIR>
df -h /home

# 4) Python 版本（需 >= 3.9）
python3 --version
```

> 后文用 `<SHARE_DIR>` 表示旧存储目录，用 `<NEW_DIR>` 表示新部署目录。
> 下面示例统一用 `SHARE_DIR=/home/ubuntu/WebDAV_Server/zotero`、`NEW_DIR=/home/ubuntu/webdav-server`、
> `NEW_DATA=/home/ubuntu/webdav-data`。

---

## 2. 备份（不可跳过）

```bash
STAMP=$(date +%Y%m%d-%H%M%S)
sudo mkdir -p /home/ubuntu/webdav-backup-$STAMP

# 2.1 数据库（含现有明文口令，注意保管权限）
mysqldump --single-transaction webdav dav_users | gzip \
  | sudo tee /home/ubuntu/webdav-backup-$STAMP/dav_users.sql.gz >/dev/null
sudo chmod 600 /home/ubuntu/webdav-backup-$STAMP/dav_users.sql.gz

# 2.2 旧部署目录（代码 + 数据；数据大时先看 §1 第 3 步）
sudo cp -a /home/ubuntu/WebDAV_Server /home/ubuntu/webdav-backup-$STAMP/WebDAV_Server

# 2.3 systemd 单元
sudo cp /etc/systemd/system/webdav.service /home/ubuntu/webdav-backup-$STAMP/

echo "backup dir: /home/ubuntu/webdav-backup-$STAMP"
```

记录到工单/笔记里：`$STAMP`、`<SHARE_DIR>`、旧 `DB_USER`、旧 systemd 单元内容。

---

## 3. 部署新代码

### 3.1 取代码

新版本此刻只在笔记本仓库里（本地 commit）。二选一：

```bash
# A) 先 push 到 GitHub，再在服务器上 clone
cd /home/ubuntu
git clone https://github.com/HareKawasaki/WebDAV-Server-For-Zotero.git webdav-server

# B) 或者从笔记本直接同步整个目录（不要同步 .git 之外的临时文件）
#    在笔记本上执行：
#    rsync -av --exclude .git --exclude __pycache__ --exclude .env \
#      /mnt/df/code_documents/webdav_serve_for_zotero/ \
#      ubuntu@172.16.1.1:/home/ubuntu/webdav-server/
```

### 3.2 建虚拟环境并装依赖

```bash
cd /home/ubuntu/webdav-server
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r server/requirements.txt      # cheroot / PyMySQL / WsgiDAV（已固定版本）
.venv/bin/python -c "import pymysql, wsgidav, cheroot; print('deps ok')"
```

### 3.3 写 `.env`

```bash
cd /home/ubuntu/webdav-server
cp .env.example .env
chmod 600 .env
sudo -u ubuntu nano .env
```

`.env` 关键项：

```ini
# 只监听回环；隧道是唯一入口
WEBDAV_HOST=127.0.0.1
WEBDAV_PORT=8000

# 新的存储根目录（每用户一个子目录）
WEBDAV_SHARE_DIR=/home/ubuntu/webdav-data
WEBDAV_PER_USER_STORAGE=true

# 想保持客户端不重新输密码，就把 realm 设成旧值
WEBDAV_REALM=WebDAV Storage

WEBDAV_DB_HOST=127.0.0.1
WEBDAV_DB_PORT=3306
WEBDAV_DB_USER=<应用账号>
WEBDAV_DB_PASSWORD=<应用账号密码>
WEBDAV_DB_NAME=webdav
WEBDAV_DB_TABLE=dav_users
```

**数据库账号建议**（最小权限；若复用旧账号，确认它至少有下面四项）：

```sql
CREATE USER IF NOT EXISTS 'webdav_app'@'localhost' IDENTIFIED BY '<强密码>';
GRANT SELECT, INSERT, UPDATE, DELETE ON webdav.dav_users TO 'webdav_app'@'localhost';
-- 仅当需要由应用建表时才加；本次升级不需要：
-- GRANT CREATE ON webdav.* TO 'webdav_app'@'localhost';
FLUSH PRIVILEGES;
```

> MariaDB 默认把来自 `127.0.0.1` 的连接解析为 `localhost`，所以建在 `@'localhost'` 上最稳。

### 3.4 初始化自检（此时服务仍未切换）

```bash
cd /home/ubuntu/webdav-server
mkdir -p /home/ubuntu/webdav-data
.venv/bin/python -m server.manage initdb
```

预期输出要点：

* `user table ready in webdav.dav_users (password column: 64, hash length: 56)`
* 可能有一条"VARCHAR(64) 余量不大"的**告警**（不阻断）
* 若报 `does not exist and this account may not create it` → 表名/库名或账号权限不对，先解决
* 若报列宽不足并打印 `ALTER TABLE …` → 按提示加宽后再继续

然后确认账号可读：

```bash
.venv/bin/python -m server.manage listusers
# 期望：hello → /hello/；hello_ubuntu → /hello_ubuntu/
```

---

## 4. 数据迁移（每用户隔离）

旧数据是所有账号共用的一个根目录，需要**指定一个归属账号**。请先决定：
现有这份数据属于 `hello` 还是 `hello_ubuntu`？

> 默认操作：**余量足够时用 `--copy`**（源目录保持不动 → 回滚最省事）。
> 数据很大且磁盘紧张时才用默认的移动语义。

```bash
cd /home/ubuntu/webdav-server

# 4.1 预演：列出将要迁移的条目，不做任何修改
.venv/bin/python -m server.manage adopt-shared hello --from <SHARE_DIR>

# 4.2 确认无误后执行（复制，保留源目录）
.venv/bin/python -m server.manage adopt-shared hello --from <SHARE_DIR> --copy --apply

# 4.3 校验
find /home/ubuntu/webdav-data/hello -maxdepth 2 | head
# 若目标已有同名条目、需要覆盖，追加 --force
```

迁移完成后，`/home/ubuntu/webdav-data/<owner>/` 里就是该账号的 WebDAV 内容；
另一个账号从空目录开始。

---

## 5. 切换 systemd 单元

```bash
sudo systemctl stop webdav.service

sudo tee /etc/systemd/system/webdav.service >/dev/null <<'EOF'
[Unit]
Description=WebDAV Server for Zotero
After=network.target mariadb.service
Wants=mariadb.service

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/webdav-server
EnvironmentFile=/home/ubuntu/webdav-server/.env
ExecStart=/home/ubuntu/webdav-server/.venv/bin/python -m server.start_server
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl start webdav.service
sleep 3
systemctl status webdav.service --no-pager
```

> 新版对 `SIGTERM` 是**真正的优雅退出**（实测约 1.5s 内停止并释放端口），
> 所以 `systemctl restart/stop` 不会再留下占用 8000 端口的僵尸进程。

---

## 6. 验证清单

### 6.1 服务器本机

```bash
ss -ltn | grep 8000                        # 期望 127.0.0.1:8000，且只有一行
journalctl -u webdav.service -n 30 --no-pager
```

```bash
code() { curl -s -o /dev/null -w "%{http_code}\n" --max-time 6 "$@"; }

code -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8000/hello/                     # 401
code -u hello:'<口令>' -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8000/hello/   # 207
code -u hello:'<口令>' -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8000/         # 403
code -u hello:'<口令>' -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8000/hello_ubuntu/  # 403
code -u hello:'<口令>' -X PROPFIND -H 'Depth: 0' 'http://127.0.0.1:8000/hello/../hello_ubuntu/'  # 403
code -u hello:'<错口令>' -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8000/hello/  # 401

# 上传/回读
echo hello > /tmp/t.txt
code -u hello:'<口令>' -T /tmp/t.txt http://127.0.0.1:8000/hello/t.txt          # 201
curl -s -u hello:'<口令>' http://127.0.0.1:8000/hello/t.txt                     # hello
rm -f /tmp/t.txt
sudo -u ubuntu rm -f /home/ubuntu/webdav-data/hello/t.txt
```

### 6.2 口令升级确认（首次登录后）

```bash
mysql -N -e "SELECT username, CHAR_LENGTH(password) FROM webdav.dav_users;"
# 已登录过的账号长度应变成 56（scrypt$16384…），未登录的仍是旧长度
```

### 6.3 笔记本侧（客户端真实路径）

笔记本上的 `127.0.0.1:8000` 隧道是现成的，直接验证即可：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/hello/          # 401
curl -s -o /dev/null -w "%{http_code}\n" -u hello:'<口令>' \
     -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8000/hello/                    # 207
```

---

## 7. 客户端切换（Zotero）

1. 启动隧道：`python client/sshtunnel_client.py`（会打印应填的 URL）。
2. Zotero → `编辑 / 设置 / 同步` → 文件同步 `WebDAV`：
   * `hello` 所在机器填 `http://127.0.0.1:8000/hello/`
   * `hello_ubuntu` 所在机器填 `http://127.0.0.1:8000/hello_ubuntu/`
   * 用户名/密码不变
3. 首次同步后确认附件能正常上传/下载。

> URL 变了，Zotero 会把它当成"另一个 WebDAV 位置"。因为数据已经迁到对应账号目录下，
> 正常情况会直接匹配；如果 Zotero 提示冲突或要重新上传，让它重新校验一次即可（数据已在服务端）。
> 保留 `WEBDAV_REALM=WebDAV Storage` 可以避免客户端重新弹出密码框。

---

## 8. 回滚

出现问题时按下面顺序回退（旧数据目录在 §4 用 `--copy` 时未被改动）：

```bash
# 1) 停新服务，恢复旧单元
sudo systemctl stop webdav.service
sudo cp /home/ubuntu/webdav-backup-<STAMP>/webdav.service /etc/systemd/system/webdav.service
sudo systemctl daemon-reload
sudo systemctl start webdav.service
systemctl status webdav.service --no-pager

# 2) 若数据库被改坏（例如误删账号），从备份恢复
gunzip -c /home/ubuntu/webdav-backup-<STAMP>/dav_users.sql.gz | mysql webdav

# 3) 若旧数据目录被动过（用了 --move 而非 --copy），从备份整体还原
sudo rm -rf /home/ubuntu/WebDAV_Server
sudo cp -a /home/ubuntu/webdav-backup-<STAMP>/WebDAV_Server /home/ubuntu/

# 4) 客户端把 Zotero URL 改回 http://127.0.0.1:8000/
```

回滚后 `password` 列里的明文口令会随备份一起回来，登录行为与升级前一致。

---

## 9. 上线后加固（建议按序执行）

1. **轮换历史口令**：现有两行是明文，首次登录会自动升级为 scrypt；仍然建议主动换一次：
   ```bash
   cd /home/ubuntu/webdav-server
   .venv/bin/python -m server.manage passwd hello
   .venv/bin/python -m server.manage passwd hello_ubuntu
   ```
2. **收口数据库账号**：若沿用了旧账号，改为 §3.3 的最小权限账号；确保只剩
   `SELECT/INSERT/UPDATE/DELETE ON webdav.dav_users`。
3. **（可选）加宽密码列**，为将来的参数调整留余量：
   ```sql
   ALTER TABLE `webdav`.`dav_users` MODIFY `password` VARCHAR(255) NOT NULL;
   ```
4. **（可选）加 TLS**：目前 Basic 凭据的机密性完全依赖 SSH 隧道；若要暴露给更多客户端，
   在前面加反向代理并让 WebDAV 继续只监听 `127.0.0.1`。
5. **确认安全组**：`8000` 端口不应对公网开放（当前已挡住，保持即可）。
6. **日志**：需要留存时在 `.env` 里设 `WEBDAV_LOG_FILE=/home/ubuntu/webdav-server/logs/server.log`
   （内置 5MB × 5 轮转，无额外依赖）。

---

## 10. 实测数据与已知限制

上线后对生产实例做过一轮附加测试（经 SSH 隧道，与 Zotero 同路径），结论如下。

### 服务端能力（本机回环，不经过网络）

| 操作 | 10 MiB |
| --- | --- |
| PUT | ~180 MB/s |
| GET | ~140 MB/s |

16 并发 PROPFIND、8 并发 1 MiB PUT 全部成功；DAV 方法矩阵（MKCOL/PUT/GET/HEAD/PROPFIND/
PROPPATCH/LOCK/UNLOCK/COPY/MOVE/DELETE）、死属性、锁语义均符合预期；跨用户访问、
`..` 逃逸、跨用户 `Destination` 一律 403。

### ⚠️ 云主机出网带宽是下载瓶颈

| 路径 | 下载（服务器→客户端） | 上传（客户端→服务器） |
| --- | --- | --- |
| 服务端本机 | 140 MB/s | 180 MB/s |
| 经 SSH 隧道（paramiko 客户端） | **0.48 MB/s** | 6.2 MB/s |
| 经原生 `ssh -L` | **0.49 MB/s** | 19.9 MB/s |
| **裸 SSH（完全不经 WebDAV）** | **0.50 MB/s** | 10.8 MB/s |

连裸 SSH 都只有 0.50 MB/s，说明既不是 WebDAV 也不是隧道实现，而是**这台云主机
对出网流量限速在约 4 Mbps**（入网未限）。

对 Zotero 的影响：上传附件正常，**下载/同步附件会被压在 ~0.5 MB/s**（50 MB 附件约 100 秒）。
要改善只能在云控制台提升带宽上限，或把服务迁到带宽更大的机器；代码层面无法规避。

## 11. 一页速查

```bash
# —— 备份 ——
mysqldump --single-transaction webdav dav_users | gzip > dav_users.sql.gz
sudo cp -a /home/ubuntu/WebDAV_Server /home/ubuntu/webdav-backup-<STAMP>/

# —— 部署 ——
cd /home/ubuntu/webdav-server && python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
cp .env.example .env && chmod 600 .env && nano .env
.venv/bin/python -m server.manage initdb
.venv/bin/python -m server.manage adopt-shared hello --from <SHARE_DIR> --copy --apply

# —— 切换 ——
sudo systemctl stop webdav.service
sudo cp <新单元> /etc/systemd/system/webdav.service
sudo systemctl daemon-reload && sudo systemctl start webdav.service

# —— 验证 ——
curl -s -o /dev/null -w '%{http_code}\n' -u hello:'<pw>' \
     -X PROPFIND -H 'Depth: 0' http://127.0.0.1:8000/hello/    # 207

# —— 回滚 ——
sudo systemctl stop webdav.service
sudo cp /home/ubuntu/webdav-backup-<STAMP>/webdav.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl start webdav.service
```
