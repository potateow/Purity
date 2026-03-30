#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import copy
import contextlib
import json
import math
import os
import pathlib
import re
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


TEST_URLS = {
    "github": "https://github.com/robots.txt",
    "youtube": "https://www.youtube.com/generate_204",
    "cloudflare": "https://www.cloudflare.com/cdn-cgi/trace",
}

IPPURE_URL = "https://my.ippure.com/v1/info"
FRAUD_BUCKETS = [0, 15, 25, 40, 50, 60, 70, 100]
DEFAULT_TIMEOUT = 15.0
DEFAULT_IPPURE_TIMEOUT = 25.0
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_IPPURE_RETRY_ATTEMPTS = 2
DEFAULT_LATENCY_RETRY_ATTEMPTS = 1
DEFAULT_RETRY_DELAY_SECONDS = 1.0
DEFAULT_SELECTION_SETTLE_SECONDS = 0.25

DEFAULT_SMALL_GROUP_THRESHOLD = 5
RUNTIME_PROXY_GROUP_NAME = "__purity_auto__"
SMALL_REGION_GROUP_KEY = "__small_regions__"
SMALL_REGION_GROUP_LABEL = "其他小分组"
METADATA_NAME_PATTERNS = (
    r"^\d+(?:\.\d+)?\s*(?:gb|tb)\s*\|",
    r"剩余流量",
    r"traffic reset",
    r"距离下次重置",
    r"expire date",
    r"套餐到期",
    r"官网",
    r"不可用请软件内更新订阅或官网看问题排查",
)
COUNTRY_CODE_REGION_LABELS = {
    "AR": "阿根廷",
    "AU": "澳大利亚",
    "BR": "巴西",
    "CA": "加拿大",
    "CH": "瑞士",
    "CN": "中国",
    "DE": "德国",
    "ES": "西班牙",
    "FR": "法国",
    "GB": "英国",
    "HK": "香港",
    "ID": "印度尼西亚",
    "IN": "印度",
    "IT": "意大利",
    "JP": "日本",
    "KR": "韩国",
    "MO": "澳门",
    "MY": "马来西亚",
    "NL": "荷兰",
    "PH": "菲律宾",
    "RU": "俄罗斯",
    "SG": "新加坡",
    "TH": "泰国",
    "TR": "土耳其",
    "TW": "台湾",
    "US": "美国",
    "VN": "越南",
}
REGION_KEYWORDS = (
    ("香港", ("香港", "hong kong")),
    ("台湾", ("台湾", "台灣", "taiwan")),
    ("澳门", ("澳门", "macau", "macao")),
    ("日本", ("日本", "japan", "tokyo", "osaka")),
    ("韩国", ("韩国", "south korea", "korea", "seoul")),
    ("新加坡", ("新加坡", "singapore")),
    ("美国", ("美国", "united states", "usa")),
    ("加拿大", ("加拿大", "canada")),
    ("德国", ("德国", "germany")),
    ("英国", ("英国", "united kingdom", "england", "london")),
    ("法国", ("法国", "france")),
    ("荷兰", ("荷兰", "netherlands")),
    ("土耳其", ("土耳其", "turkey", "turkiye")),
    ("印度", ("印度", "india")),
    ("马来西亚", ("马来西亚", "malaysia")),
    ("菲律宾", ("菲律宾", "philippines")),
    ("泰国", ("泰国", "thailand")),
    ("越南", ("越南", "vietnam")),
    ("印度尼西亚", ("印尼", "印度尼西亚", "indonesia")),
    ("澳大利亚", ("澳大利亚", "australia", "sydney", "melbourne")),
    ("俄罗斯", ("俄罗斯", "russia")),
    ("阿根廷", ("阿根廷", "argentina")),
    ("巴西", ("巴西", "brazil")),
    ("西班牙", ("西班牙", "spain")),
    ("意大利", ("意大利", "italy")),
    ("瑞士", ("瑞士", "switzerland")),
    ("中国", ("中国", "china", "大陆", "内地")),
)
SERVER_TOKEN_REGION_LABELS = {
    "ar": "阿根廷",
    "au": "澳大利亚",
    "br": "巴西",
    "ca": "加拿大",
    "ch": "瑞士",
    "cn": "中国",
    "de": "德国",
    "es": "西班牙",
    "fr": "法国",
    "gb": "英国",
    "hk": "香港",
    "id": "印度尼西亚",
    "in": "印度",
    "it": "意大利",
    "jp": "日本",
    "kr": "韩国",
    "mo": "澳门",
    "my": "马来西亚",
    "nl": "荷兰",
    "ph": "菲律宾",
    "ru": "俄罗斯",
    "sg": "新加坡",
    "th": "泰国",
    "tr": "土耳其",
    "tw": "台湾",
    "uk": "英国",
    "us": "美国",
    "vn": "越南",
}


class PurityError(Exception):
    pass


@dataclass
class NodeResult:
    name: str
    type: str
    server: str | None
    port: int | None
    status: str
    error: str | None
    fraud_score: int | None
    fraud_bucket: int | None
    ip_info: dict[str, Any] | None
    ip_error: str | None
    latencies_ms: dict[str, float | None]
    latency_errors: dict[str, str | None]
    average_latency_ms: float | None

    def ranking_key(self) -> tuple[float, float, float, str]:
        bucket = self.fraud_bucket if self.fraud_bucket is not None else math.inf
        latency = self.average_latency_ms if self.average_latency_ms is not None else math.inf
        raw_score = self.fraud_score if self.fraud_score is not None else math.inf
        return (bucket, latency, raw_score, self.name)


@dataclass
class ProxyRegionGroup:
    key: str
    label: str
    proxies: list[dict[str, Any]]
    regions: list[tuple[str, int]]
    merged: bool = False


class MihomoRuntime:
    def __init__(
        self,
        core_binary: str,
        runtime_config: dict[str, Any],
        startup_timeout: float,
        keep_temp: bool,
    ) -> None:
        self.core_binary = core_binary
        self.runtime_config = runtime_config
        self.startup_timeout = startup_timeout
        self.keep_temp = keep_temp
        self.temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="purity-runtime-"))
        self.config_path = self.temp_dir / "config.yaml"
        self.log_path = self.temp_dir / "core.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.log_fp = None
        self.mixed_port = int(runtime_config["mixed-port"])
        self.controller_address = str(runtime_config["external-controller"])
        self.controller_url = f"http://{self.controller_address}"

    def __enter__(self) -> "MihomoRuntime":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def start(self) -> None:
        self.config_path.write_text(
            yaml.safe_dump(self.runtime_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        self.log_fp = self.log_path.open("wb")
        self.process = subprocess.Popen(
            [self.core_binary, "-f", str(self.config_path)],
            stdout=self.log_fp,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if os.name != "nt" else None,
        )
        wait_for_port(self.mixed_port, self.startup_timeout)
        wait_for_port(int(self.controller_address.rsplit(":", 1)[1]), self.startup_timeout)
        deadline = time.time() + self.startup_timeout
        while time.time() < deadline:
            try:
                self.controller_request("/proxies", timeout=2.0)
                return
            except Exception:
                time.sleep(0.2)
        raise PurityError("mihomo controller 在启动超时内未就绪。")

    def stop(self) -> None:
        if self.process is not None:
            terminate_process(self.process)
        if self.log_fp is not None:
            self.log_fp.close()
        if not self.keep_temp:
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def controller_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 5.0,
    ) -> Any:
        request = urllib.request.Request(
            f"{self.controller_url}{path}",
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def select_proxy(self, group_name: str, proxy_name: str) -> None:
        path = f"/proxies/{urllib.parse.quote(group_name, safe='')}"
        self.controller_request(
            path,
            method="PUT",
            payload={"name": proxy_name},
            timeout=5.0,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="分析 Clash/Shadowrocket 订阅中的节点纯净度与延迟。"
    )
    parser.add_argument(
        "subscription_url",
        nargs="?",
        help="订阅链接，留空则进入交互输入；命令行传 URL 时建议加引号",
    )
    parser.add_argument(
        "--config",
        help="本地 YAML 配置文件路径。指定后将跳过订阅下载。",
    )
    parser.add_argument(
        "--output",
        default="purity_results.json",
        help="结果 JSON 输出路径，默认: purity_results.json",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(2, min(6, (os.cpu_count() or 4) // 2)),
        help="并发测试数，默认按 CPU 自动计算",
    )
    parser.add_argument(
        "--core-binary",
        help="mihomo/clash 可执行文件路径；不传则自动从 PATH 中查找",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=8.0,
        help="代理内核启动等待时间（秒），默认: 8",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="单次请求超时时间（秒），默认: 15",
    )
    parser.add_argument(
        "--ippure-timeout",
        type=float,
        default=DEFAULT_IPPURE_TIMEOUT,
        help="IPPure 请求超时时间（秒），默认: 25",
    )
    parser.add_argument(
        "--groups",
        help="按地区分组筛选要测试的节点；支持分组序号或名称，多个用逗号分隔，如 1,3 或 香港,日本",
    )
    parser.add_argument(
        "--merge-small-groups-threshold",
        type=int,
        default=DEFAULT_SMALL_GROUP_THRESHOLD,
        help="地区节点数小于等于该值时合并为一个小分组，默认: 5；设为 0 可关闭合并",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="保留调试用的临时配置与日志文件",
    )
    return parser.parse_args()


def ensure_yaml_dependency() -> None:
    if yaml is None:
        raise PurityError("缺少 PyYAML，请先在 conda 环境中安装 pyyaml。")


def resolve_subscription_url(args: argparse.Namespace) -> str | None:
    if args.config:
        return None
    if args.subscription_url:
        return args.subscription_url.strip()
    print("未检测到订阅链接参数，请直接粘贴完整订阅链接后回车。")
    user_input = input("订阅链接: ").strip()
    if not user_input:
        raise PurityError("未提供订阅链接。")
    return user_input


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "purity-checker/1.0",
            "Accept": "application/yaml,text/yaml,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()

    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        with contextlib.suppress(UnicodeDecodeError):
            return raw.decode(encoding)
    return raw.decode("utf-8", errors="replace")


def decode_base64_text(value: str) -> str:
    payload = "".join(value.strip().split())
    padding = (-len(payload)) % 4
    payload += "=" * padding
    decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        with contextlib.suppress(UnicodeDecodeError):
            return decoded.decode(encoding)
    return decoded.decode("utf-8", errors="replace")


def should_try_uri_subscription(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    schemes = ("vmess://", "vless://", "trojan://", "ss://")
    if any(scheme in stripped for scheme in schemes):
        return True
    compact = "".join(stripped.split())
    if not compact:
        return False
    with contextlib.suppress(Exception):
        decoded = decode_base64_text(compact)
        return any(scheme in decoded for scheme in schemes)
    return False


def parse_vmess_uri(uri: str) -> dict[str, Any]:
    payload = uri.removeprefix("vmess://").strip()
    config = json.loads(decode_base64_text(payload))
    host = config.get("host") or ""
    path = config.get("path") or ""
    tls_enabled = str(config.get("tls", "")).lower() == "tls"
    network = config.get("net") or "tcp"
    proxy: dict[str, Any] = {
        "name": config.get("ps") or config.get("add") or "vmess-node",
        "type": "vmess",
        "server": config["add"],
        "port": int(config["port"]),
        "uuid": config["id"],
        "alterId": int(config.get("aid", 0) or 0),
        "cipher": "auto",
        "udp": True,
    }
    if tls_enabled:
        proxy["tls"] = True
        if config.get("sni"):
            proxy["servername"] = config["sni"]
        elif host:
            proxy["servername"] = host
    if network and network != "tcp":
        proxy["network"] = network
    if network == "ws":
        ws_opts: dict[str, Any] = {}
        if path:
            ws_opts["path"] = path
        if host:
            ws_opts["headers"] = {"Host": host}
        if ws_opts:
            proxy["ws-opts"] = ws_opts
    if network == "grpc" and config.get("path"):
        proxy["grpc-opts"] = {"grpc-service-name": config["path"]}
    if host and network != "ws" and "servername" not in proxy and tls_enabled:
        proxy["servername"] = host
    return proxy


def parse_trojan_uri(uri: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(uri)
    query = urllib.parse.parse_qs(parsed.query)
    name = urllib.parse.unquote(parsed.fragment) or parsed.hostname or "trojan-node"
    proxy: dict[str, Any] = {
        "name": name,
        "type": "trojan",
        "server": parsed.hostname,
        "port": int(parsed.port or 443),
        "password": urllib.parse.unquote(parsed.username or ""),
        "udp": True,
    }
    sni = query.get("sni", [None])[0]
    if sni:
        proxy["sni"] = sni
    if query.get("allowInsecure", ["0"])[0] in {"1", "true"}:
        proxy["skip-cert-verify"] = True
    network = query.get("type", ["tcp"])[0]
    if network != "tcp":
        proxy["network"] = network
    if network == "ws":
        ws_opts: dict[str, Any] = {}
        path = query.get("path", [None])[0]
        host = query.get("host", [None])[0]
        if path:
            ws_opts["path"] = path
        if host:
            ws_opts["headers"] = {"Host": host}
        if ws_opts:
            proxy["ws-opts"] = ws_opts
    return proxy


def parse_ss_uri(uri: str) -> dict[str, Any]:
    body = uri.removeprefix("ss://")
    if "#" in body:
        body, fragment = body.split("#", 1)
        name = urllib.parse.unquote(fragment)
    else:
        name = "ss-node"
    if "@" not in body:
        decoded = decode_base64_text(body.split("?", 1)[0])
        rest = ""
        if "?" in body:
            rest = "?" + body.split("?", 1)[1]
        body = decoded + rest
    main, _, query_string = body.partition("?")
    credentials, _, server_part = main.rpartition("@")
    method, _, password = credentials.partition(":")
    host, _, port = server_part.rpartition(":")
    query = urllib.parse.parse_qs(query_string)
    proxy: dict[str, Any] = {
        "name": name or host or "ss-node",
        "type": "ss",
        "server": host,
        "port": int(port),
        "cipher": method,
        "password": password,
        "udp": True,
    }
    plugin = query.get("plugin", [None])[0]
    if plugin:
        proxy["plugin"] = plugin
    return proxy


def parse_vless_uri(uri: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(uri)
    query = urllib.parse.parse_qs(parsed.query)
    name = urllib.parse.unquote(parsed.fragment) or parsed.hostname or "vless-node"
    proxy: dict[str, Any] = {
        "name": name,
        "type": "vless",
        "server": parsed.hostname,
        "port": int(parsed.port or 443),
        "uuid": urllib.parse.unquote(parsed.username or ""),
        "udp": True,
    }
    security = query.get("security", ["none"])[0]
    if security == "tls":
        proxy["tls"] = True
        sni = query.get("sni", [None])[0] or query.get("host", [None])[0]
        if sni:
            proxy["servername"] = sni
    if query.get("allowInsecure", ["0"])[0] in {"1", "true"}:
        proxy["skip-cert-verify"] = True
    network = query.get("type", ["tcp"])[0]
    if network != "tcp":
        proxy["network"] = network
    if network == "ws":
        ws_opts: dict[str, Any] = {}
        path = query.get("path", [None])[0]
        host = query.get("host", [None])[0]
        if path:
            ws_opts["path"] = path
        if host:
            ws_opts["headers"] = {"Host": host}
        if ws_opts:
            proxy["ws-opts"] = ws_opts
    if network == "grpc":
        service_name = query.get("serviceName", [None])[0]
        if service_name:
            proxy["grpc-opts"] = {"grpc-service-name": service_name}
    return proxy


def parse_uri_subscription(text: str) -> dict[str, Any]:
    candidate = text.strip()
    if not any(scheme in candidate for scheme in ("vmess://", "vless://", "trojan://", "ss://")):
        candidate = decode_base64_text(candidate)

    proxies: list[dict[str, Any]] = []
    for raw_line in candidate.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("vmess://"):
            proxies.append(parse_vmess_uri(line))
        elif line.startswith("vless://"):
            proxies.append(parse_vless_uri(line))
        elif line.startswith("trojan://"):
            proxies.append(parse_trojan_uri(line))
        elif line.startswith("ss://"):
            proxies.append(parse_ss_uri(line))

    if not proxies:
        raise PurityError("订阅内容不是有效的 Clash YAML，也不是可识别的 URI 订阅。")
    return {"proxies": proxies}


def load_subscription_data(args: argparse.Namespace) -> tuple[dict[str, Any], str]:
    if args.config:
        config_path = pathlib.Path(args.config).expanduser().resolve()
        if not config_path.is_file():
            raise PurityError(f"本地配置文件不存在: {config_path}")
        raw = config_path.read_bytes()
        for encoding in ("utf-8", "utf-8-sig", "gb18030"):
            with contextlib.suppress(UnicodeDecodeError):
                text = raw.decode(encoding)
                break
        else:
            text = raw.decode("utf-8", errors="replace")
        source = str(config_path)
    else:
        url = resolve_subscription_url(args)
        assert url is not None
        text = fetch_text(url, args.request_timeout)
        source = url

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        if should_try_uri_subscription(text):
            return parse_uri_subscription(text), source
        raise PurityError(f"YAML 解析失败: {exc}") from exc

    if not isinstance(data, dict):
        if should_try_uri_subscription(text):
            return parse_uri_subscription(text), source
        raise PurityError("订阅内容不是有效的 Clash YAML 配置。")
    return data, source


def extract_proxies(data: dict[str, Any]) -> list[dict[str, Any]]:
    proxies = data.get("proxies")
    if not isinstance(proxies, list):
        raise PurityError("配置中未找到 proxies 列表。")

    normalized: list[dict[str, Any]] = []
    for item in proxies:
        if not isinstance(item, dict):
            continue
        if "name" not in item or "type" not in item:
            continue
        normalized.append(item)

    if not normalized:
        raise PurityError("没有找到可测试的节点。")
    return normalized


def looks_like_metadata_proxy(name: str) -> bool:
    candidate = name.strip().lower()
    return any(re.search(pattern, candidate, re.IGNORECASE) for pattern in METADATA_NAME_PATTERNS)


def filter_testable_proxies(proxies: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    filtered: list[dict[str, Any]] = []
    skipped: list[str] = []
    for proxy in proxies:
        name = str(proxy.get("name", ""))
        if looks_like_metadata_proxy(name):
            skipped.append(name)
            continue
        filtered.append(proxy)
    return filtered, skipped


def extract_flag_country_code(text: str) -> str | None:
    for index in range(len(text) - 1):
        first = ord(text[index])
        second = ord(text[index + 1])
        if 0x1F1E6 <= first <= 0x1F1FF and 0x1F1E6 <= second <= 0x1F1FF:
            left = chr(ord("A") + first - 0x1F1E6)
            right = chr(ord("A") + second - 0x1F1E6)
            return f"{left}{right}"
    return None


def infer_region_from_server(server: str) -> str | None:
    for token in re.split(r"[^a-z0-9]+", server.lower()):
        if not token:
            continue
        matched = re.match(r"^([a-z]{2,3})\d*$", token)
        probe = matched.group(1) if matched else token
        if probe in SERVER_TOKEN_REGION_LABELS:
            return SERVER_TOKEN_REGION_LABELS[probe]
    return None


def infer_region_label(proxy: dict[str, Any]) -> str:
    name = str(proxy.get("name", ""))
    server = str(proxy.get("server") or "")
    haystacks = [name.lower(), server.lower()]

    for label, keywords in REGION_KEYWORDS:
        if any(keyword in haystack for haystack in haystacks for keyword in keywords):
            return label

    country_code = extract_flag_country_code(name)
    if country_code:
        return COUNTRY_CODE_REGION_LABELS.get(country_code, country_code)

    server_region = infer_region_from_server(server)
    if server_region:
        return server_region
    return "其他地区"


def build_region_groups(
    proxies: list[dict[str, Any]],
    merge_small_threshold: int,
) -> list[ProxyRegionGroup]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for proxy in proxies:
        region = infer_region_label(proxy)
        buckets.setdefault(region, []).append(proxy)

    groups: list[ProxyRegionGroup] = []
    small_regions: list[tuple[str, list[dict[str, Any]]]] = []
    for label, items in sorted(buckets.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        if len(items) <= merge_small_threshold:
            small_regions.append((label, items))
            continue
        groups.append(
            ProxyRegionGroup(
                key=label,
                label=label,
                proxies=items,
                regions=[(label, len(items))],
            )
        )

    if small_regions:
        merged_proxies: list[dict[str, Any]] = []
        merged_regions: list[tuple[str, int]] = []
        for label, items in small_regions:
            merged_proxies.extend(items)
            merged_regions.append((label, len(items)))
        groups.append(
            ProxyRegionGroup(
                key=SMALL_REGION_GROUP_KEY,
                label=SMALL_REGION_GROUP_LABEL,
                proxies=merged_proxies,
                regions=merged_regions,
                merged=True,
            )
        )
    return groups


def format_group_region_summary(group: ProxyRegionGroup) -> str:
    parts = [f"{label}({count})" for label, count in group.regions[:5]]
    if len(group.regions) > 5:
        parts.append(f"等 {len(group.regions)} 个地区")
    return "、".join(parts)


def format_group_display_label(group: ProxyRegionGroup) -> str:
    if not group.merged:
        return group.label
    return f"{group.label}（{format_group_region_summary(group)}）"


def print_region_groups(groups: list[ProxyRegionGroup]) -> None:
    total = sum(len(group.proxies) for group in groups)
    print("\n按地区识别到以下测试分组：")
    print(f"  0. 全部节点 ({total} 个)")
    for index, group in enumerate(groups, start=1):
        line = f" {index:>2}. {group.label} ({len(group.proxies)} 个)"
        if group.merged:
            line += f" | 包含: {format_group_region_summary(group)}"
        print(line)


def parse_group_selection_input(
    raw_value: str,
    groups: list[ProxyRegionGroup],
) -> list[ProxyRegionGroup]:
    tokens = [token.strip() for token in re.split(r"[,\s，]+", raw_value) if token.strip()]
    if not tokens:
        return groups

    normalized_map = {
        group.label.casefold(): group for group in groups
    }
    display_map = {
        format_group_display_label(group).casefold(): group for group in groups
    }
    selected: list[ProxyRegionGroup] = []
    seen: set[str] = set()
    for token in tokens:
        normalized = token.casefold()
        if normalized in {"0", "all", "全部"}:
            return groups
        if token.isdigit():
            index = int(token)
            if index < 1 or index > len(groups):
                raise PurityError(f"分组序号超出范围: {token}")
            group = groups[index - 1]
        else:
            group = (
                normalized_map.get(normalized)
                or display_map.get(normalized)
            )
            if group is None:
                raise PurityError(f"未识别的分组: {token}")
        if group.key in seen:
            continue
        seen.add(group.key)
        selected.append(group)
    return selected


def choose_region_groups(
    groups: list[ProxyRegionGroup],
    explicit_selection: str | None,
) -> list[ProxyRegionGroup]:
    if explicit_selection:
        return parse_group_selection_input(explicit_selection, groups)

    print_region_groups(groups)
    if any(group.merged for group in groups):
        print("当前启用了小地区合并；如果想看到更细的地区分组，可使用 --merge-small-groups-threshold 0。")
    print("输入序号可单选或多选，多个用逗号分隔；直接回车默认全部。")
    while True:
        try:
            raw_value = input("选择分组: ").strip()
        except EOFError:
            return groups
        if not raw_value:
            return groups
        try:
            return parse_group_selection_input(raw_value, groups)
        except PurityError as exc:
            print(f"输入有误：{exc}")


def locate_core_binary(explicit: str | None) -> str | None:
    if explicit:
        path = shutil.which(explicit) or explicit
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
        raise PurityError(f"指定的代理内核不可执行: {explicit}")

    local_bin_dir = pathlib.Path(__file__).resolve().parent / "bin"
    for candidate in ("mihomo", "clash-meta", "clash"):
        candidate_path = local_bin_dir / candidate
        if candidate_path.is_file() and os.access(candidate_path, os.X_OK):
            return str(candidate_path)
        if os.name == "nt":
            exe_path = local_bin_dir / f"{candidate}.exe"
            if exe_path.is_file() and os.access(exe_path, os.X_OK):
                return str(exe_path)

    for candidate in ("mihomo", "clash-meta", "clash"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def prompt_download_core() -> bool:
    if not sys.stdin.isatty():
        return False
    downloader = pathlib.Path(__file__).resolve().parent / "download_mihomo.py"
    if not downloader.is_file():
        return False

    print("未找到 mihomo/clash 可执行文件。")
    print("可以现在自动下载到当前项目的 bin 目录。")
    answer = input("是否立即下载？[Y/n]: ").strip().lower()
    if answer not in {"", "y", "yes"}:
        return False

    print("开始自动下载 mihomo...")
    try:
        subprocess.run(
            [sys.executable, str(downloader)],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PurityError(
            "自动下载 mihomo 失败。你可以手动运行 `python download_mihomo.py` 后重试。"
        ) from exc
    return True


def detect_core_binary(explicit: str | None) -> str:
    path = locate_core_binary(explicit)
    if path:
        return path

    if prompt_download_core():
        downloaded_path = locate_core_binary(None)
        if downloaded_path:
            return downloaded_path
        raise PurityError(
            "已完成自动下载，但仍未找到可执行的 mihomo/clash。"
            " 请检查 bin 目录内容，或通过 --core-binary 手动指定路径。"
        )

    raise PurityError(
        "未找到 mihomo/clash 可执行文件。"
        " 你可以先运行 `python download_mihomo.py` 自动下载到项目的 bin 目录，"
        " 或通过 --core-binary 指定路径。"
    )


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def wait_for_port(port: int, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise PurityError(f"代理端口 {port} 在 {timeout:.1f}s 内未就绪。")


def build_single_proxy_config(proxy: dict[str, Any], mixed_port: int) -> dict[str, Any]:
    proxy_name = str(proxy["name"])
    return {
        "mixed-port": mixed_port,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "silent",
        "ipv6": False,
        "dns": {
            "enable": False,
            "ipv6": False,
        },
        "proxies": [proxy],
        "proxy-groups": [
            {
                "name": "AUTO",
                "type": "select",
                "proxies": [proxy_name],
            }
        ],
        "rules": ["MATCH,AUTO"],
    }


def build_runtime_config(
    base_data: dict[str, Any],
    proxies: list[dict[str, Any]],
    mixed_port: int,
    controller_port: int,
) -> dict[str, Any]:
    runtime = copy.deepcopy(base_data)
    for key in (
        "proxies",
        "proxy-groups",
        "rules",
        "rule-providers",
        "mixed-port",
        "port",
        "socks-port",
        "redir-port",
        "tproxy-port",
        "external-controller",
        "external-controller-tls",
        "external-ui",
        "external-ui-url",
        "external-ui-name",
        "secret",
        "allow-lan",
        "bind-address",
    ):
        runtime.pop(key, None)

    runtime["mixed-port"] = mixed_port
    runtime["allow-lan"] = False
    runtime["bind-address"] = "127.0.0.1"
    runtime["mode"] = "rule"
    runtime["log-level"] = "warning"
    runtime["external-controller"] = f"127.0.0.1:{controller_port}"
    runtime["secret"] = ""

    if isinstance(runtime.get("dns"), dict):
        runtime["dns"] = copy.deepcopy(runtime["dns"])
        runtime["dns"].pop("fallback", None)
        runtime["dns"].pop("fallback-filter", None)
        if runtime["dns"].get("enable"):
            runtime["dns"]["listen"] = f"127.0.0.1:{reserve_port()}"

    runtime["proxies"] = proxies
    runtime["proxy-groups"] = [
        {
            "name": RUNTIME_PROXY_GROUP_NAME,
            "type": "select",
            "proxies": [str(proxy["name"]) for proxy in proxies],
        }
    ]
    runtime["rules"] = [f"MATCH,{RUNTIME_PROXY_GROUP_NAME}"]
    return runtime


def request_via_proxy(
    url: str,
    proxy_port: int,
    timeout: float,
    read_limit: int | None = None,
) -> tuple[bytes, float]:
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(
            {
                "http": proxy_url,
                "https": proxy_url,
            }
        )
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "purity-checker/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )

    start = time.perf_counter()
    with opener.open(request, timeout=timeout) as response:
        body = response.read() if read_limit is None else response.read(read_limit)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return body, elapsed_ms


def request_via_proxy_with_retry(
    url: str,
    proxy_port: int,
    timeout: float,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    read_limit: int | None = None,
) -> tuple[bytes, float]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return request_via_proxy(url, proxy_port, timeout, read_limit=read_limit)
        except Exception as exc:
            last_error = exc
            if attempt >= max(1, attempts):
                break
            time.sleep(delay_seconds)
    assert last_error is not None
    raise last_error


def measure_latency_target(
    label: str,
    url: str,
    proxy_port: int,
    timeout: float,
) -> tuple[str, float | None, str | None]:
    try:
        _, elapsed_ms = request_via_proxy_with_retry(
            url,
            proxy_port,
            timeout,
            attempts=DEFAULT_LATENCY_RETRY_ATTEMPTS,
            read_limit=1,
        )
        return label, round(elapsed_ms, 2), None
    except Exception as exc:
        return label, None, str(exc)


def fraud_bucket(score: int | None) -> int | None:
    if score is None:
        return None
    for bucket in FRAUD_BUCKETS:
        if score <= bucket:
            return bucket
    return FRAUD_BUCKETS[-1]


def average_latency(latencies_ms: dict[str, float | None]) -> float | None:
    values = [value for value in latencies_ms.values() if value is not None]
    if not values:
        return None
    return round(statistics.fmean(values), 2)


def terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_single_proxy(
    proxy: dict[str, Any],
    runtime: MihomoRuntime,
    request_timeout: float,
    ippure_timeout: float,
    selection_settle_seconds: float = DEFAULT_SELECTION_SETTLE_SECONDS,
) -> NodeResult:
    name = str(proxy.get("name", "unknown"))
    proxy_type = str(proxy.get("type", "unknown"))
    server = proxy.get("server")
    port = proxy.get("port")

    try:
        return run_single_proxy_test(
            proxy,
            runtime,
            request_timeout,
            ippure_timeout,
            selection_settle_seconds,
        )
    except Exception as exc:
        return NodeResult(
            name=name,
            type=proxy_type,
            server=str(server) if server is not None else None,
            port=int(port) if isinstance(port, int) else None,
            status="error",
            error=str(exc),
            fraud_score=None,
            fraud_bucket=None,
            ip_info=None,
            ip_error=str(exc),
            latencies_ms={label: None for label in TEST_URLS},
            latency_errors={label: None for label in TEST_URLS},
            average_latency_ms=None,
        )


def run_single_proxy_test(
    proxy: dict[str, Any],
    runtime: MihomoRuntime,
    request_timeout: float,
    ippure_timeout: float,
    selection_settle_seconds: float,
) -> NodeResult:
    name = str(proxy.get("name", "unknown"))
    proxy_type = str(proxy.get("type", "unknown"))
    server = proxy.get("server")
    port = proxy.get("port")
    ip_info: dict[str, Any] | None = None
    ip_error: str | None = None
    latencies_ms: dict[str, float | None] = {label: None for label in TEST_URLS}
    latency_errors: dict[str, str | None] = {label: None for label in TEST_URLS}

    try:
        runtime.select_proxy(RUNTIME_PROXY_GROUP_NAME, name)
        if selection_settle_seconds > 0:
            time.sleep(selection_settle_seconds)

        def _fetch_ippure() -> tuple[dict[str, Any] | None, str | None]:
            try:
                body, _ = request_via_proxy_with_retry(
                    IPPURE_URL,
                    runtime.mixed_port,
                    ippure_timeout,
                    attempts=DEFAULT_IPPURE_RETRY_ATTEMPTS,
                )
                return json.loads(body.decode("utf-8")), None
            except Exception as exc:
                return None, str(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1 + len(TEST_URLS)) as pool:
            ippure_future = pool.submit(_fetch_ippure)
            latency_futures = {
                pool.submit(
                    measure_latency_target,
                    label,
                    url,
                    runtime.mixed_port,
                    request_timeout,
                ): label
                for label, url in TEST_URLS.items()
            }
            ip_info, ip_error = ippure_future.result()
            for future in concurrent.futures.as_completed(latency_futures):
                label, latency, error = future.result()
                latencies_ms[label] = latency
                latency_errors[label] = error

        score = None
        if ip_info is not None:
            score = ip_info.get("fraudScore")
            score = int(score) if score is not None else None

        success_count = sum(1 for value in latencies_ms.values() if value is not None)
        status = "ok" if ip_info is not None and success_count > 0 else "partial" if ip_info is not None or success_count > 0 else "error"
        error_parts: list[str] = []
        if ip_error:
            error_parts.append(f"IPPure失败: {ip_error}")
        failed_latency_targets = [label for label, value in latencies_ms.items() if value is None]
        if failed_latency_targets:
            error_parts.append(f"延迟失败: {', '.join(failed_latency_targets)}")

        return NodeResult(
            name=name,
            type=proxy_type,
            server=str(server) if server is not None else None,
            port=int(port) if isinstance(port, int) else None,
            status=status,
            error=" | ".join(error_parts) if error_parts else None,
            fraud_score=score,
            fraud_bucket=fraud_bucket(score),
            ip_info=ip_info,
            ip_error=ip_error,
            latencies_ms=latencies_ms,
            latency_errors=latency_errors,
            average_latency_ms=average_latency(latencies_ms),
        )
    except Exception as exc:
        error_message = str(exc)
        if runtime.log_path.exists():
            with contextlib.suppress(Exception):
                log_excerpt = runtime.log_path.read_text(encoding="utf-8", errors="replace")[-800:]
                if log_excerpt.strip():
                    error_message = f"{error_message} | core日志: {log_excerpt.strip()}"
        return NodeResult(
            name=name,
            type=proxy_type,
            server=str(server) if server is not None else None,
            port=int(port) if isinstance(port, int) else None,
            status="error",
            error=error_message,
            fraud_score=None,
            fraud_bucket=None,
            ip_info=None,
            ip_error=error_message,
            latencies_ms={label: None for label in TEST_URLS},
            latency_errors={label: None for label in TEST_URLS},
            average_latency_ms=None,
        )


def _fmt_ms(val: float | None) -> str:
    return f"{val:.0f}ms" if val is not None else "-"


def _fmt_score(val: int | None) -> str:
    return str(val) if val is not None else "-"


def _char_display_width(ch: str) -> int:
    codepoint = ord(ch)
    if ch in {"\u200d", "\ufe0f"}:
        return 0
    if 0x1F1E6 <= codepoint <= 0x1F1FF:
        return 1
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in {"Cf", "Mn", "Me"}:
        return 0
    return 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    return sum(_char_display_width(ch) for ch in text)


def _truncate_display(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if _display_width(text) <= max_width:
        return text
    if max_width <= 3:
        return "." * max_width

    truncated: list[str] = []
    used_width = 0
    remaining_width = max_width - 3
    for ch in text:
        ch_width = _char_display_width(ch)
        if used_width + ch_width > remaining_width:
            break
        truncated.append(ch)
        used_width += ch_width
    return "".join(truncated) + "..."


def _display_justify(text: str, width: int, *, align: str = "left") -> str:
    rendered = _truncate_display(text, width)
    padding = max(0, width - _display_width(rendered))
    if align == "right":
        return (" " * padding) + rendered
    return rendered + (" " * padding)


def _rank_comprehensive(results: list[NodeResult]) -> list[NodeResult]:
    return sorted(results, key=lambda r: r.ranking_key())


def _rank_by_latency(results: list[NodeResult]) -> list[NodeResult]:
    return sorted(
        results,
        key=lambda r: (
            r.average_latency_ms if r.average_latency_ms is not None else math.inf,
            r.name,
        ),
    )


def print_ranked_results(results: list[NodeResult]) -> None:
    ok_count = sum(1 for r in results if r.status == "ok")
    partial_count = sum(1 for r in results if r.status == "partial")
    fail_count = sum(1 for r in results if r.status == "error")
    print(f"\n共 {len(results)} 个节点  |  成功 {ok_count}  部分 {partial_count}  失败 {fail_count}")

    name_col_width = 30
    if results:
        name_col_width = min(40, max(30, max(_display_width(r.name) for r in results)))

    comprehensive = _rank_comprehensive(results)
    comprehensive_header = (
        f" {_display_justify('#', 3, align='right')}  "
        f"{_display_justify('节点', name_col_width)} "
        f"{_display_justify('状态', 6)} "
        f"{_display_justify('纯净', 4, align='right')} "
        f"{_display_justify('延迟', 8, align='right')}"
    )
    comprehensive_width = _display_width(comprehensive_header)
    print("\n" + "=" * comprehensive_width)
    print(" 综合排名（纯净度优先，延迟次之）")
    print("=" * comprehensive_width)
    print(comprehensive_header)
    print("-" * comprehensive_width)
    for i, r in enumerate(comprehensive, 1):
        status = "OK" if r.status == "ok" else "部分" if r.status == "partial" else "失败"
        print(
            f" {_display_justify(str(i), 3, align='right')}  "
            f"{_display_justify(r.name, name_col_width)} "
            f"{_display_justify(status, 6)} "
            f"{_display_justify(_fmt_score(r.fraud_score), 4, align='right')} "
            f"{_display_justify(_fmt_ms(r.average_latency_ms), 8, align='right')}"
        )

    by_latency = _rank_by_latency(results)
    latency_header = (
        f" {_display_justify('#', 3, align='right')}  "
        f"{_display_justify('节点', name_col_width)} "
        f"{_display_justify('状态', 6)} "
        f"{_display_justify('延迟', 8, align='right')} "
        f"{_display_justify('纯净', 4, align='right')}"
    )
    latency_width = _display_width(latency_header)
    print("\n" + "=" * latency_width)
    print(" 延迟排名（速度优先）")
    print("=" * latency_width)
    print(latency_header)
    print("-" * latency_width)
    for i, r in enumerate(by_latency, 1):
        status = "OK" if r.status == "ok" else "部分" if r.status == "partial" else "失败"
        print(
            f" {_display_justify(str(i), 3, align='right')}  "
            f"{_display_justify(r.name, name_col_width)} "
            f"{_display_justify(status, 6)} "
            f"{_display_justify(_fmt_ms(r.average_latency_ms), 8, align='right')} "
            f"{_display_justify(_fmt_score(r.fraud_score), 4, align='right')}"
        )


def _serialize_node(item: NodeResult, rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "name": item.name,
        "status": item.status,
        "fraudScore": item.fraud_score,
        "avgLatencyMs": item.average_latency_ms,
        "github": item.latencies_ms.get("github"),
        "youtube": item.latencies_ms.get("youtube"),
        "cloudflare": item.latencies_ms.get("cloudflare"),
        "ip": (item.ip_info or {}).get("ip"),
        "country": (item.ip_info or {}).get("country"),
        "error": item.error,
    }


def serialize_results(
    source: str,
    results: list[NodeResult],
    total_targets: int,
) -> dict[str, Any]:
    ok = sum(1 for r in results if r.status == "ok")
    partial = sum(1 for r in results if r.status == "partial")
    failed = sum(1 for r in results if r.status == "error")
    comprehensive = _rank_comprehensive(results)
    by_latency = _rank_by_latency(results)
    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "source": source,
        "total": total_targets,
        "tested": len(results),
        "ok": ok,
        "partial": partial,
        "failed": failed,
        "comprehensiveRanking": [
            _serialize_node(r, i) for i, r in enumerate(comprehensive, 1)
        ],
        "latencyRanking": [
            _serialize_node(r, i) for i, r in enumerate(by_latency, 1)
        ],
    }


def _split_into_chunks(items: list, n: int) -> list[list]:
    k, m = divmod(len(items), n)
    return [items[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n)]


def main() -> int:
    args = parse_args()
    ensure_yaml_dependency()
    if args.merge_small_groups_threshold < 0:
        raise PurityError("--merge-small-groups-threshold 不能小于 0。")
    data, source = load_subscription_data(args)
    proxies, skipped = filter_testable_proxies(extract_proxies(data))
    if not proxies:
        raise PurityError("过滤提示类节点后，没有剩余可测试的真实节点。")
    region_groups = build_region_groups(proxies, args.merge_small_groups_threshold)
    selected_region_groups = choose_region_groups(region_groups, args.groups)
    selected_group_labels = [
        format_group_display_label(group) for group in selected_region_groups
    ]
    selected_proxies = [
        proxy for group in selected_region_groups for proxy in group.proxies
    ]
    if not selected_proxies:
        raise PurityError("所选分组中没有可测试节点。")
    core_binary = detect_core_binary(args.core_binary)
    output_path = pathlib.Path(args.output).expanduser().resolve()
    num_workers = min(args.workers, len(selected_proxies))

    print("⚠️  提示：请确保已关闭系统 TUN 模式代理，否则测试流量会被劫持，导致延迟偏高或连接失败。")
    print(f"加载到 {len(proxies)} 个真实节点。")
    print(f"使用代理内核: {core_binary}")
    if skipped:
        print(f"已自动跳过 {len(skipped)} 个提示类节点。")
    print(f"已选择 {len(selected_region_groups)} 个分组，共 {len(selected_proxies)} 个节点。")
    print(f"当前测试分组: {'、'.join(selected_group_labels)}")
    print(f"开始测试（{num_workers} 个并行 worker）...")

    results: list[NodeResult] = []
    progress_lock = threading.Lock()
    completed_count = [0]

    def _on_result(result: NodeResult) -> None:
        with progress_lock:
            results.append(result)
            completed_count[0] += 1
            marker = "OK" if result.status == "ok" else "WARN" if result.status == "partial" else "ERR"
            print(f"[{completed_count[0]}/{len(selected_proxies)}] {marker} {result.name}")
            snapshot = serialize_results(source, results, len(selected_proxies))
            output_path.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _worker(chunk: list[dict[str, Any]]) -> None:
        config = build_runtime_config(
            data, chunk,
            mixed_port=reserve_port(),
            controller_port=reserve_port(),
        )
        with MihomoRuntime(
            core_binary=core_binary,
            runtime_config=config,
            startup_timeout=args.startup_timeout,
            keep_temp=args.keep_temp,
        ) as runtime:
            for proxy in chunk:
                result = test_single_proxy(
                    proxy, runtime,
                    args.request_timeout,
                    args.ippure_timeout,
                )
                _on_result(result)

    chunks = _split_into_chunks(selected_proxies, num_workers)
    if num_workers == 1:
        _worker(chunks[0])
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = [executor.submit(_worker, chunk) for chunk in chunks]
            for future in concurrent.futures.as_completed(futures):
                future.result()

    print_ranked_results(results)
    print(f"\n结果已保存至: {output_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except PurityError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
    except urllib.error.URLError as exc:
        print(f"网络错误: {exc}", file=sys.stderr)
        raise SystemExit(3)
