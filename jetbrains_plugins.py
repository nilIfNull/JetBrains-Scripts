#!/usr/bin/env python3
"""
Mirror the latest JetBrains Marketplace plugin versions compatible with a given
IDE build and generate an updatePlugins.xml suitable for a custom plugin
repository served by nginx.

The script intentionally uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable

import requests

MARKETPLACE = "https://plugins.jetbrains.com"
JETBRAINS_RELEASES = "https://data.services.jetbrains.com/products/releases"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
HEADERS = {"User-Agent": USER_AGENT}

@dataclass(frozen=True)
class DownloadedPlugin:
    plugin_id: str
    name: str
    version: str
    file_name: str
    original_url: str
    element: ET.Element


def log(message: str) -> None:
    print(message, file=sys.stderr)


def read_url(url: str, *, timeout: int, accept: str = "*/*") -> bytes:
    response = requests.get(url, headers={**HEADERS, "Accept": accept}, timeout=timeout)
    response.raise_for_status()
    return response.content


def download_file(url: str, destination: Path, *, timeout: int, retries: int) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")

    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers=HEADERS, timeout=timeout, stream=True) as response:
                response.raise_for_status()
                final_url = response.url
                with tmp.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
            tmp.replace(destination)
            return final_url
        except Exception:
            if tmp.exists():
                tmp.unlink()
            if attempt >= retries:
                raise
            time.sleep(min(2 * attempt, 10))
    raise RuntimeError(f"Failed to download {url}")


def download_plugin_file(url: str, plugin_dir: Path, fallback_name: str, *, timeout: int, retries: int) -> tuple[
    Path, str]:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    tmp = plugin_dir / f"{fallback_name}.part"

    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, headers=HEADERS, timeout=timeout, stream=True) as response:
                response.raise_for_status()
                final_url = response.url
                file_name = url_file_name(final_url, fallback_name)
                if not file_name.lower().endswith(".zip"):
                    file_name = f"{file_name}.zip"
                destination = plugin_dir / file_name
                if destination.exists() and destination.stat().st_size > 0:
                    log(f"Already exists: {destination}")
                    return destination, final_url
                with tmp.open("wb") as out:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            out.write(chunk)
                tmp.replace(destination)
                return destination, final_url
        except Exception:
            if tmp.exists():
                tmp.unlink()
            if attempt >= retries:
                raise
            time.sleep(min(2 * attempt, 10))
    raise RuntimeError(f"Failed to download {url}")


def parse_xml(data: bytes, source: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid XML from {source}: {exc}") from exc


def text_of(element: ET.Element, child_name: str, default: str = "") -> str:
    child = element.find(child_name)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def sanitize_file_part(value: str) -> str:
    value = value.strip() or "plugin"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "plugin"


def url_file_name(url: str, fallback: str) -> str:
    parsed = urllib.parse.urlparse(url)
    name = posixpath.basename(parsed.path)
    if name and "." in name:
        return sanitize_file_part(name)
    return sanitize_file_part(fallback)


def absolute_url(url: str) -> str:
    return urllib.parse.urljoin(MARKETPLACE, url)


def plugin_download_url(plugin_id: str, marketplace_build: str, channel: str | None) -> str:
    query = {
        "action": "download",
        "id": plugin_id,
        "build": marketplace_build,
    }
    if channel:
        query["channel"] = channel
    return f"{MARKETPLACE}/pluginManager/?{urllib.parse.urlencode(query)}"


def marketplace_build(product_code: str, build: str) -> str:
    return f"{product_code}-{build}" if product_code else build


def looks_like_build(value: str) -> bool:
    return bool(re.fullmatch(r"\d{3}(?:\.\d+){1,3}", value.strip()))


def looks_like_ide_version(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}\.\d+(?:\.\d+)?", value.strip()))


def resolve_build(value: str, *, product_code: str, timeout: int) -> str:
    value = value.strip()
    if looks_like_build(value):
        return value
    if not looks_like_ide_version(value):
        raise RuntimeError(
            f"Invalid --build {value!r}; pass a build like 261.25134.95 or an IDE version like 2026.1.3."
        )

    query = urllib.parse.urlencode(
        {
            "code": product_code,
            "latest": "false",
            "type": "release",
        }
    )
    url = f"{JETBRAINS_RELEASES}?{query}"
    payload = json.loads(read_url(url, timeout=timeout, accept="application/json").decode("utf-8"))
    releases = payload.get(product_code)
    if not isinstance(releases, list):
        raise RuntimeError(f"No releases found for product code {product_code}")

    for release in releases:
        if not isinstance(release, dict):
            continue
        if release.get("version") == value:
            build = release.get("build")
            if isinstance(build, str) and build.strip():
                log(f"Resolved IDE {product_code} {value} -> build {build}")
                return build.strip()

    known = [
        release.get("version")
        for release in releases[:10]
        if isinstance(release, dict) and isinstance(release.get("version"), str)
    ]
    hint = f" Known recent versions: {', '.join(known)}." if known else ""
    raise RuntimeError(f"No release build found for {product_code} {value}.{hint}")


def normalize_plugin_id(raw: str) -> str:
    raw = raw.strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme and parsed.netloc:
        match = re.search(r"/plugin/(\d+)", parsed.path)
        if match:
            return match.group(1)
    return raw


def resolve_marketplace_id(plugin_id: str, *, timeout: int) -> str:
    if not plugin_id.isdigit():
        return plugin_id
    url = f"{MARKETPLACE}/api/plugins/{plugin_id}"
    try:
        payload = json.loads(read_url(url, timeout=timeout, accept="application/json").decode("utf-8"))
    except Exception as exc:
        log(f"Could not resolve numeric plugin id {plugin_id}; using it as-is ({exc})")
        return plugin_id
    xml_id = payload.get("xmlId") or payload.get("pluginXmlId")
    if isinstance(xml_id, str) and xml_id.strip():
        log(f"Resolved Marketplace id {plugin_id} -> {xml_id}")
        return xml_id.strip()
    return plugin_id


def clone_element(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="utf-8"))


def plugin_xml_element_from_zip(path: Path, expected_id: str) -> ET.Element:
    candidates: list[tuple[str, ET.Element]] = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.endswith("META-INF/plugin.xml") and not name.startswith("__MACOSX/"):
                candidates.append((name, parse_plugin_descriptor(archive.read(name), f"{path}:{name}")))

        for jar_name in archive.namelist():
            if not jar_name.endswith(".jar") or jar_name.startswith("__MACOSX/"):
                continue
            try:
                with zipfile.ZipFile(BytesIO(archive.read(jar_name))) as jar:
                    for name in jar.namelist():
                        if name.endswith("META-INF/plugin.xml") and not name.startswith("__MACOSX/"):
                            source = f"{path}:{jar_name}!/{name}"
                            candidates.append((source, parse_plugin_descriptor(jar.read(name), source)))
            except zipfile.BadZipFile:
                continue

    if not candidates:
        raise RuntimeError(f"No META-INF/plugin.xml found in {path} or nested jar files")

    for source, descriptor in candidates:
        if text_of(descriptor, "id", "") == expected_id:
            log(f"Read plugin descriptor: {source}")
            return descriptor

    candidates.sort(key=lambda item: (descriptor_score(item[0], item[1]), item[0]))
    source, descriptor = candidates[0]
    log(f"Read plugin descriptor: {source}")
    return descriptor


def parse_plugin_descriptor(data: bytes, source: str) -> ET.Element:
    root = parse_xml(data, source)
    if root.tag != "idea-plugin":
        raise RuntimeError(f"Unexpected plugin descriptor root {root.tag!r} in {source}")
    return root


def descriptor_score(source: str, descriptor: ET.Element) -> tuple[int, int]:
    plugin_id = text_of(descriptor, "id", "")
    module_count = len(descriptor.findall("module"))
    if plugin_id:
        return (0, 0)
    if module_count:
        return (1, 0)
    if "lib/" in source:
        return (2, 0)
    return (3, 0)


def ensure_child_text(element: ET.Element, child_name: str, value: str) -> None:
    child = element.find(child_name)
    if child is None:
        child = ET.SubElement(element, child_name)
    child.text = value


def mirror_plugin(
        plugin_id: str,
        *,
        marketplace_build_value: str,
        output_dir: Path,
        repo_url: str | None,
        channel: str | None,
        timeout: int,
        retries: int,
) -> DownloadedPlugin | None:
    original_url = plugin_download_url(plugin_id, marketplace_build_value, channel)
    plugin_dir = output_dir / "plugins"
    fallback_name = sanitize_file_part(f"{plugin_id}-{marketplace_build_value}.zip")
    log(f"Download latest compatible plugin: {plugin_id}")
    destination, final_url = download_plugin_file(
        original_url,
        plugin_dir,
        fallback_name,
        timeout=timeout,
        retries=retries,
    )
    file_name = destination.name

    plugin = plugin_xml_element_from_zip(destination, plugin_id)
    actual_id = text_of(plugin, "id", plugin_id)
    name = text_of(plugin, "name", actual_id)
    version = text_of(plugin, "version", "unknown")

    mirrored = clone_element(plugin)
    mirrored.attrib.clear()
    ensure_child_text(mirrored, "id", actual_id)
    ensure_child_text(mirrored, "name", name)
    ensure_child_text(mirrored, "version", version)
    relative_url = f"plugins/{file_name}"
    mirrored.set("url", urllib.parse.urljoin(repo_url.rstrip("/") + "/", relative_url) if repo_url else relative_url)
    return DownloadedPlugin(actual_id, name, version, file_name, final_url, mirrored)


def indent_xml(element: ET.Element, level: int = 0) -> None:
    indent = "\n" + level * "  "
    if len(element):
        if not element.text or not element.text.strip():
            element.text = indent + "  "
        for child in element:
            indent_xml(child, level + 1)
        if not child.tail or not child.tail.strip():
            child.tail = indent
    if level and (not element.tail or not element.tail.strip()):
        element.tail = indent


def write_update_plugins_xml(plugins: Iterable[DownloadedPlugin], output_dir: Path) -> Path:
    path = output_dir / "updatePlugins.xml"
    if path.exists() and path.stat().st_size > 0:
        root = parse_xml(path.read_bytes(), str(path))
        if root.tag != "plugin-repository":
            raise RuntimeError(f"Unexpected root tag in {path}: {root.tag}")
    else:
        root = ET.Element("plugin-repository")

    category = find_or_create_category(root, "Mirrored")
    for plugin in plugins:
        upsert_plugin_element(root, category, plugin.element)

    indent_xml(root)
    tree = ET.ElementTree(root)
    tree.write(path, encoding="utf-8", xml_declaration=True)
    return path


def find_or_create_category(root: ET.Element, name: str) -> ET.Element:
    for category in root.findall("category"):
        if category.get("name") == name:
            return category
    return ET.SubElement(root, "category", {"name": name})


def upsert_plugin_element(root: ET.Element, default_category: ET.Element, new_plugin: ET.Element) -> None:
    new_id = text_of(new_plugin, "id", "")
    if not new_id:
        default_category.append(new_plugin)
        return

    for parent in root.findall("category"):
        for index, existing in enumerate(list(parent)):
            if existing.tag == "idea-plugin" and text_of(existing, "id", "") == new_id:
                parent[index] = new_plugin
                return

    for index, existing in enumerate(list(root)):
        if existing.tag == "idea-plugin" and text_of(existing, "id", "") == new_id:
            root[index] = new_plugin
            return

    default_category.append(new_plugin)


def load_plugin_ids(args: argparse.Namespace) -> list[str]:
    plugin_ids: list[str] = []
    if args.plugins:
        plugin_ids.extend(args.plugins)
    if args.plugins_file:
        for line in Path(args.plugins_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                plugin_ids.append(line)
    seen: set[str] = set()
    result: list[str] = []
    for plugin_id in plugin_ids:
        normalized = normalize_plugin_id(plugin_id)
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror compatible JetBrains Marketplace plugins and updatePlugins.xml."
    )
    parser.add_argument(
        "--build",
        required=True,
        help="IDE build number or IDE version, for example 261.25134.95 or 2026.1.3.",
    )
    parser.add_argument(
        "--product-code",
        default="IU",
        help="JetBrains product code used when --build is an IDE version. Default: IU.",
    )
    parser.add_argument("--plugin", dest="plugins", action="append", help="Marketplace plugin id. Repeatable.")
    parser.add_argument("--plugins-file", help="Text file with one plugin id per line.")
    parser.add_argument("--output", default="dist", help="Output directory served by nginx. Default: dist.")
    parser.add_argument("--repo-url", help="Optional public base URL used in updatePlugins.xml.")
    parser.add_argument("--channel", help="Optional Marketplace channel.")
    parser.add_argument("--timeout", type=int, default=60, help="Network timeout seconds. Default: 60.")
    parser.add_argument("--retries", type=int, default=3, help="Download retries. Default: 3.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    plugin_ids = load_plugin_ids(args)
    if not plugin_ids:
        raise SystemExit("Nothing to do: pass --plugin or --plugins-file.")

    output_dir = Path(args.output).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    build = resolve_build(args.build, product_code=args.product_code, timeout=args.timeout)
    mp_build = marketplace_build(args.product_code, build)
    log(f"Marketplace build: {mp_build}")

    downloaded_plugins: list[DownloadedPlugin] = []
    downloaded_by_id: dict[str, DownloadedPlugin] = {}
    for plugin_id in plugin_ids:
        try:
            plugin_id = resolve_marketplace_id(plugin_id, timeout=args.timeout)
            if plugin_id in downloaded_by_id:
                continue
            plugin = mirror_plugin(
                plugin_id,
                marketplace_build_value=mp_build,
                output_dir=output_dir,
                repo_url=args.repo_url,
                channel=args.channel,
                timeout=args.timeout,
                retries=args.retries,
            )
            if plugin is not None:
                if plugin.plugin_id not in downloaded_by_id:
                    downloaded_by_id[plugin.plugin_id] = plugin
                    downloaded_plugins.append(plugin)
        except requests.HTTPError as exc:
            detail = exc.response.text[:1000].strip() if exc.response is not None else ""
            suffix = f": {detail}" if detail else ""
            status = exc.response.status_code if exc.response is not None else "?"
            reason = exc.response.reason if exc.response is not None else "HTTP error"
            log(f"Failed {plugin_id}: HTTP {status} {reason}{suffix}")
        except Exception as exc:
            log(f"Failed {plugin_id}: {exc}")

    if downloaded_plugins:
        xml_path = write_update_plugins_xml(downloaded_plugins, output_dir)
        log(f"Wrote {xml_path}")
    elif plugin_ids:
        log("No plugin entries were mirrored; updatePlugins.xml was not changed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
