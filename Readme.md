# Secure-WebDAV-Tunnel

1. 本项目是一个轻量级、安全且易于部署的 WebDAV 私有云方案。
2. 本项目旨在为需要在复杂网络环境下（如内网、校园网或受限局域网）安全访问私有文件的用户提供支持。通过结合 ***WsgiDAV 服务端***与
   *** SSH 隧道 转发技术 ***，实现了即便在不安全的 HTTP 协议下也能保证数据的绝对加密传输。

## 为什么选择本项目？

1. 架构解耦：服务端与客户端配置彻底分离，支持独立部署与维护。
2. 数据库：内置 SQLite 自动托管逻辑，无需配置复杂的数据库服务。
3. 端到端加密：利用标准的 SSH 协议保护 WebDAV 流量，杜绝中间人攻击。
4. 快速初始化：服务端主程序 start_server.py 具备自检功能，首次运行自动创建数据库与默认凭据。

## 如何使用本项目?

1. 服务端运行:
   Linux:

```bash
python3 -m venv .webdav_server
source .webdav_server/Script/Activate
pip install requirements.txt
python3 start_server.py
```

Windows:

```pwsh
python -m venv .webdav_server
#激活虚拟环境, 注意 Windows 下路径与 Linux 不同
.\.webdav_server\Scripts\Activate.ps1
#安装依赖
pip install -r requirements.txt
#运行服务端
python start_server.py
```

2. 客户端:
   填充client_conf.py中需要填充的部分: SSH_HOST填充服务端的IP地址,SSH_PASSWORD填写服务端的密码.
3. 启动zotero,依次点击: 编辑/设置/同步,将 "文件同步" 中的 "我的文库附件同步方式" 修改为 "WebDAV" :
选择http,在://后的文本框填入:
```localhost:8000```
或者client_conf.py中自行修改的LOCAL_PORT.  
4. 用户名和密码应当自行在server中设置,方便起见,本项目已提供演示用户名和密码,均填入 admin 即可开始同步Zotero数据.  
