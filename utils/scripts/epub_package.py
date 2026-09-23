"""Deterministic EPUB package inspection and spine/resource extraction."""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET


class EpubPackageError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class SpineItem:
    id: str
    href: str
    media_type: str


class EpubPackage:
    def __init__(self, path):
        self.path = path
        self.archive = zipfile.ZipFile(path)
        self.names = set(self.archive.namelist())
        self._load()

    def close(self) -> None:
        self.archive.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _load(self) -> None:
        if "mimetype" not in self.names or self.archive.read("mimetype") != b"application/epub+zip":
            raise EpubPackageError("文件不是有效 EPUB：mimetype 缺失或不正确")
        try:
            container = ET.fromstring(self.archive.read("META-INF/container.xml"))
        except KeyError as exc:
            raise EpubPackageError("EPUB 缺少 META-INF/container.xml") from exc
        rootfile = next((item.attrib.get("full-path") for item in container.iter() if _local(item.tag) == "rootfile"), None)
        if not rootfile or rootfile not in self.names:
            raise EpubPackageError("EPUB 缺少有效 OPF package")
        self.opf_path = rootfile
        try:
            self.opf = ET.fromstring(self.archive.read(rootfile))
        except (KeyError, ET.ParseError) as exc:
            raise EpubPackageError("OPF 无法解析") from exc
        self.opf_dir = posixpath.dirname(rootfile)
        self.manifest: dict[str, dict[str, str]] = {}
        for item in self.opf.iter():
            if _local(item.tag) == "item" and item.get("id") and item.get("href"):
                self.manifest[item.get("id")] = {
                    "href": item.get("href"),
                    "media_type": item.get("media-type", ""),
                    "properties": item.get("properties", ""),
                }
        self.spine: list[SpineItem] = []
        for itemref in self.opf.iter():
            if _local(itemref.tag) != "itemref":
                continue
            ref = self.manifest.get(itemref.get("idref", ""))
            if not ref:
                raise EpubPackageError(f"spine 引用了不存在的 manifest 项：{itemref.get('idref')}")
            self.spine.append(SpineItem(itemref.get("idref", ""), self.resolve(ref["href"]), ref["media_type"]))
        if not self.spine:
            raise EpubPackageError("EPUB spine 为空")

    def resolve(self, href: str, base: str | None = None) -> str:
        base_dir = posixpath.dirname(base or self.opf_path)
        return posixpath.normpath(posixpath.join(base_dir, href.split("#", 1)[0]))

    def read(self, path: str) -> bytes:
        try:
            return self.archive.read(path)
        except KeyError as exc:
            raise EpubPackageError(f"EPUB 资源不存在：{path}") from exc

    def nav_path(self) -> str | None:
        for item in self.manifest.values():
            if "nav" in item.get("properties", "").split():
                return self.resolve(item["href"])
        for item in self.manifest.values():
            if item.get("media_type") == "application/x-dtbncx+xml":
                return self.resolve(item["href"])
        return None

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in self.opf.iter():
            if _local(item.tag) in {"title", "creator", "language", "identifier"} and item.text:
                result[_local(item.tag)] = item.text.strip()
        return result
