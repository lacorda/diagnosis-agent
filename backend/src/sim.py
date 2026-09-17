# 模拟探测库存。工具只查这里，不访问真实网络。
# 场景一：www.example.com 能解析，但 IP 连通超时；默认网卡正常；timeout 日志可命中。
# 场景二：eth1 接口关闭，重启接口属于高风险操作。

import re

DNS_RECORDS = {
    "www.example.com": "93.184.216.34",
    "internal.service.local": "192.168.1.100",
}

TIMEOUT_IPS = {"93.184.216.34", "192.168.1.254"}

LOCAL_INTERFACES = {
    "eth0": {"name": "eth0", "status": "up", "result": "接口正常", "ip": "192.168.1.50"},
    "ethernet": {"name": "Ethernet", "status": "up", "result": "接口正常", "ip": "192.168.1.50"},
    "wi-fi": {"name": "Wi-Fi", "status": "up", "result": "接口正常", "ip": "192.168.1.50"},
    "eth1": {"name": "eth1", "status": "down", "result": "接口关闭 (Administratively down)", "ip": None},
}

LOGS_BY_KEYWORD = {
    "timeout": [
        "kernel: neighbour: 93.184.216.34 probe failed, no reply",
        "routing: default via isp, last next-hop unreachable",
        "conntrack: tcp SYN to 93.184.216.34 timed out after 3s",
    ],
    "connection refused": [
        "app: 连接到 192.168.1.200:5432 失败：Connection refused",
    ],
    "dns": [
        "resolver: DNS 服务器 8.8.8.8 响应慢",
        "resolver: 无法解析主机名 failed.internal.service",
    ],
}

IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


def resolve_dns(domain):
    """按域名查模拟 DNS 记录。

    参数:
        domain: 用户给出的主机名，例如 www.example.com
    返回:
        命中时含 ok=True 和 ip；库存没有该域名时 ok=False
    """
    name = str(domain or "").strip().lower()
    if not name:
        raise ValueError("hostname 不能为空")
    if IPV4.match(name):
        return {"ok": True, "domain": name, "ip": name, "note": "输入已经是 IP，无需解析"}
    ip = DNS_RECORDS.get(name)
    if not ip:
        return {"ok": False, "domain": name, "ip": None, "error": "NXDOMAIN"}
    return {"ok": True, "domain": name, "ip": ip}


def ping_target(target):
    """对目标做连通性检查。example.com 对应 IP 固定为 timeout。

    参数:
        target: ping 目标，优先填 DNS 返回的 ip
    返回:
        含 reachable 与 status；timeout 时把 status 交给日志工具
    """
    addr = str(target or "").strip()
    if not addr:
        raise ValueError("target 不能为空")
    ip = addr if IPV4.match(addr) else DNS_RECORDS.get(addr.lower(), addr)
    if addr in ("localhost", "127.0.0.1"):
        return {"ok": True, "target": addr, "ip": "127.0.0.1", "reachable": True, "status": "ok", "latencyMs": 1}
    if ip in TIMEOUT_IPS:
        return {"ok": True, "target": addr, "ip": ip, "reachable": False, "status": "timeout", "latencyMs": None}
    return {"ok": True, "target": addr, "ip": ip, "reachable": True, "status": "ok", "latencyMs": 24}


def check_interface(interface_name):
    """检查本机网卡状态。未指定时查默认 Ethernet。

    参数:
        interface_name: 可选接口名，eth1 固定为关闭
    返回:
        含接口名、status、result
    """
    key = str(interface_name or "ethernet").strip().lower()
    found = LOCAL_INTERFACES.get(key) or LOCAL_INTERFACES["ethernet"]
    return {
        "ok": found["status"] == "up",
        "interface": found["name"],
        "status": found["status"],
        "ip": found["ip"],
        "result": found["result"],
    }


def analyze_logs(keywords, time_range="过去1小时"):
    """按关键字取模拟网络日志。

    参数:
        keywords: 检索词，连通失败时应填 ping 返回的 status
        time_range: 时间范围描述
    返回:
        命中时含 logs；没有匹配则 logs 为空
    """
    key = str(keywords or "").strip().lower()
    if not key:
        raise ValueError("keywords 不能为空")
    matched_key = next((item for item in LOGS_BY_KEYWORD if key in item or item in key), None)
    if not matched_key:
        return {"ok": False, "keywords": key, "timeRange": time_range, "logs": [], "error": "无匹配日志"}
    return {"ok": True, "keywords": key, "timeRange": time_range, "logs": list(LOGS_BY_KEYWORD[matched_key])}


def restart_interface(interface_name):
    """重启指定网络接口。仅在人工确认后由高风险工具调用。

    参数:
        interface_name: 接口名称，如 eth1
    返回:
        重启后的接口状态
    """
    name = str(interface_name or "").strip()
    if not name:
        raise ValueError("interface_name 不能为空")
    return {
        "ok": True,
        "interface": name,
        "status": "up",
        "result": f"接口 {name} 已重启并启用",
    }
