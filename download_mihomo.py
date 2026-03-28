#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import pathlib
import platform
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass


GITHUB_REPO = "MetaCubeX/mihomo"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
TAGGED_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{{tag}}"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "purity-mihomo-downloader/1.0",
}


class DownloadError(Exception):
    pass


@dataclass(frozen=True)
class TargetPlatform:
    os_name: str
    arch: str
    archive_ext: str
    binary_name: str
    system_label: str


def parse_args() -> argparse.Namespace:
    default_output = pathlib.Path(__file__).resolve().parent / "bin" / (
        "mihomo.exe" if os.name == "nt" else "mihomo"
    )
    parser = argparse.ArgumentParser(
        description="自动检测当前系统并下载合适的 mihomo 到本项目 bin 目录。"
    )
    parser.add_argument(
        "--tag",
        help="指定要下载的 release tag，例如 v1.19.21；不传则下载最新版本",
    )
    parser.add_argument(
        "--output",
        default=str(default_output),
        help=f"mihomo 输出路径，默认: {default_output}",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="即使本地已是相同版本，也强制重新下载",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="网络请求超时时间（秒），默认: 30",
    )
    return parser.parse_args()


def http_get_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_release(tag: str | None, timeout: float) -> dict:
    url = LATEST_RELEASE_API if not tag else TAGGED_RELEASE_API.format(tag=tag)
    try:
        return http_get_json(url, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and tag:
            raise DownloadError(f"未找到指定的 mihomo 版本: {tag}") from exc
        raise DownloadError(f"获取 mihomo release 信息失败: {exc}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(f"访问 GitHub 失败: {exc}") from exc


def normalize_arch(machine: str) -> str:
    normalized = machine.lower()
    mapping = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86": "386",
        "i386": "386",
        "i686": "386",
        "armv7l": "armv7",
        "armv7": "armv7",
        "armv6l": "armv6",
        "armv6": "armv6",
        "armv5l": "armv5",
        "armv5": "armv5",
        "riscv64": "riscv64",
        "ppc64le": "ppc64le",
        "s390x": "s390x",
        "loongarch64": "loong64",
    }
    if normalized in mapping:
        return mapping[normalized]
    raise DownloadError(f"暂不支持自动匹配的 CPU 架构: {machine}")


def detect_target_platform() -> TargetPlatform:
    system = platform.system().lower()
    arch = normalize_arch(platform.machine())

    if system == "darwin":
        return TargetPlatform("darwin", arch, ".gz", "mihomo", f"macOS/{arch}")
    if system == "linux":
        return TargetPlatform("linux", arch, ".gz", "mihomo", f"Linux/{arch}")
    if system == "windows":
        return TargetPlatform("windows", arch, ".zip", "mihomo.exe", f"Windows/{arch}")
    raise DownloadError(f"暂不支持自动下载的操作系统: {platform.system()}")


def extract_installed_tag(binary_path: pathlib.Path) -> str | None:
    if not binary_path.is_file():
        return None
    try:
        result = subprocess.run(
            [str(binary_path), "-v"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    combined = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\bv\d+\.\d+\.\d+\b", combined)
    return match.group(0) if match else None


def classify_asset_suffix(asset_name: str, tag: str, ext: str) -> int | None:
    suffix = asset_name[: -len(ext)]
    if suffix == tag:
        return 0
    if suffix == f"compatible-{tag}":
        return 10
    if re.fullmatch(rf"v\d+-{re.escape(tag)}", suffix):
        return 20
    if re.fullmatch(rf"abi\d+-{re.escape(tag)}", suffix):
        return 30
    if re.fullmatch(rf"go\d+-{re.escape(tag)}", suffix):
        return 40
    if re.fullmatch(rf"v\d+-go\d+-{re.escape(tag)}", suffix):
        return 50
    return None


def pick_asset(release: dict, target: TargetPlatform) -> dict:
    tag = str(release["tag_name"])
    prefix = f"mihomo-{target.os_name}-{target.arch}-"
    candidates: list[tuple[int, str, dict]] = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if not name.startswith(prefix) or not name.endswith(target.archive_ext):
            continue
        suffix = name[len(prefix):]
        rank = classify_asset_suffix(suffix, tag, target.archive_ext)
        if rank is None:
            continue
        candidates.append((rank, name, asset))

    if not candidates:
        available = [
            str(asset.get("name", ""))
            for asset in release.get("assets", [])
            if str(asset.get("name", "")).startswith(f"mihomo-{target.os_name}-")
        ]
        raise DownloadError(
            "没有找到适合当前系统的 mihomo 下载资产。"
            f" 当前平台: {target.system_label}；可用资产示例: {', '.join(available[:8]) or '无'}"
        )

    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2]


def download_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise DownloadError(f"下载 mihomo 失败: {exc}") from exc


def extract_binary_from_zip(raw: bytes, binary_name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        file_names = [name for name in archive.namelist() if not name.endswith("/")]
        preferred = [
            name for name in file_names if pathlib.PurePosixPath(name).name == binary_name
        ]
        candidates = preferred or [
            name for name in file_names
            if pathlib.PurePosixPath(name).name.lower().startswith("mihomo")
        ]
        if not candidates:
            raise DownloadError("ZIP 压缩包中未找到 mihomo 可执行文件。")
        with archive.open(candidates[0]) as member:
            return member.read()


def extract_binary_from_gzip(raw: bytes) -> bytes:
    try:
        return gzip.decompress(raw)
    except OSError as exc:
        raise DownloadError("解压 mihomo gzip 文件失败。") from exc


def write_binary(binary_path: pathlib.Path, content: bytes) -> None:
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=binary_path.parent, delete=False) as temp_file:
        temp_file.write(content)
        temp_path = pathlib.Path(temp_file.name)
    if os.name != "nt":
        current_mode = temp_path.stat().st_mode
        temp_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    os.replace(temp_path, binary_path)


def main() -> int:
    args = parse_args()
    output_path = pathlib.Path(args.output).expanduser().resolve()
    target = detect_target_platform()
    release = fetch_release(args.tag, args.timeout)
    tag = str(release["tag_name"])

    current_tag = extract_installed_tag(output_path)
    if current_tag == tag and not args.force:
        print(f"本地已安装最新 mihomo: {current_tag}")
        print(f"路径: {output_path}")
        return 0

    asset = pick_asset(release, target)
    asset_name = str(asset["name"])
    asset_url = str(asset["browser_download_url"])
    print(f"检测到平台: {target.system_label}")
    print(f"目标版本: {tag}")
    print(f"下载资产: {asset_name}")

    raw = download_bytes(asset_url, args.timeout)
    if target.archive_ext == ".zip":
        binary_bytes = extract_binary_from_zip(raw, target.binary_name)
    else:
        binary_bytes = extract_binary_from_gzip(raw)

    write_binary(output_path, binary_bytes)
    installed_tag = extract_installed_tag(output_path)
    print(f"已写入: {output_path}")
    if installed_tag:
        print(f"当前版本: {installed_tag}")
    else:
        print("已完成下载，但未能自动识别版本号。")
    print(f"官方发布页: {RELEASES_PAGE}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        raise SystemExit(130)
    except DownloadError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
