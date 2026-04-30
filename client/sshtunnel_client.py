import sshtunnel as tunnel
from clinet_conf import *
import time
import sys
from sshtunnel import SSHTunnelForwarder


def start_zotero_tunnel():
    print("--- Zotero Secure Tunnel Client ---")
    
    # 核心配置解耦
    tunnel = SSHTunnelForwarder(
        (SSH_HOST, SSH_PORT),
        ssh_username=SSH_USER,
        ssh_password=SSH_PASSWORD, 
        remote_bind_address=('127.0.0.1', REMOTE_PORT), # 指向serve的 WebDAV 端口
        local_bind_address=('127.0.0.1', LOCAL_PORT) # 映射到本地端口
    )

    try:
        tunnel.start()
        print(f"[*] 隧道已成功建立！")
        print(f"[*] 请在 Zotero 设置中填写以下信息:")
        print(f"    - URL: http://127.0.0.1:{LOCAL_PORT}/")
        print(f"    - 用户名: admin")
        print(f"    - 密码: admin")
        print(f"\n[!] 状态: 正在监听本地端口 {LOCAL_PORT} -> 远程端口 {REMOTE_PORT}")
        
        while True:
            if not tunnel.is_active:
                print("[!] 隧道断开，正在尝试重连...")
                tunnel.restart()
            time.sleep(2)
            
    except Exception as e:
        print(f"[-] 隧道连接失败: {e}")
    finally:
        tunnel.stop()

if __name__ == "__main__":
    start_zotero_tunnel()