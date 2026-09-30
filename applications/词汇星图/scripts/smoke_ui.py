"""Browser smoke for the dictionary, using a disposable data root."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import os
import socket
from urllib.error import HTTPError
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

import service
from smoke import fixture
from utils.scripts.dictionary_store import DictionaryStore
from utils.scripts.timestamp import iso_timestamp

APP = Path(__file__).resolve().parents[1]
PREVIEW = Path(__file__).resolve().parents[3] / "outputs" / "词汇星图" / "preview-word-page.png"
GRAPH_PREVIEW = PREVIEW.with_name("preview-graph.png")


def wait_for(url: str, seconds: int = 20) -> None:
    for _ in range(seconds * 5):
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(.2)
    raise TimeoutError(url)


def main() -> None:
    node = shutil.which("node.exe") or shutil.which("node")
    if not node:
        raise RuntimeError("需要 Node.js")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        fixture(root)
        store = DictionaryStore(root)
        store.migrate_entries()
        stage_path = root / "outputs" / "词汇星图" / "stages" / "sample.json"
        stage_path.parent.mkdir(parents=True)
        stage_path.write_text(json.dumps({"schemaVersion": "1.0", "revision": 1,
            "updatedAt": iso_timestamp(), "stageId": "sample", "label": "测试阶段", "words": {}},
            ensure_ascii=False), encoding="utf-8")
        service.fetch_audio = lambda lemma, variety: (b"RIFF" + b"\0" * 20, "audio/wav")
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.make_handler(store, service.RunManager(root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            front_port = probe.getsockname()[1]
        front_url = f"http://127.0.0.1:{front_port}"
        frontend = subprocess.Popen([node, str(APP / "node_modules" / "vite" / "bin" / "vite.js"),
                                     "--host", "127.0.0.1", "--port", str(front_port), "--strictPort"],
                                    cwd=APP, env={**os.environ, "VITE_API_PORT": str(server.server_address[1])},
                                    stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        try:
            wait_for(front_url + "/")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1920, "height": 1080})
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(front_url + "/")
                try:
                    page.locator(".graph-node").first.wait_for(timeout=10000)
                except Exception:
                    print("browser errors:", errors, "page:", page.locator("body").inner_text()[:500])
                    for endpoint in ("/api/bootstrap", "/api/graph?stage=all"):
                        try:
                            with urlopen(front_url + endpoint) as response:
                                print(endpoint, response.status)
                        except HTTPError as error:
                            print(endpoint, error.code, error.read().decode("utf-8")[:500])
                    raise
                assert page.locator(".graph-node:not(.is-placeholder)").count() == 2
                assert page.locator(".graph-node.is-placeholder").count() == 0
                page.get_by_role("button", name="项目页").click()
                assert page.locator(".project-card").count() == 1
                page.get_by_role("button", name="字典页").click()
                page.locator(".dictionary-search input").fill("pass")
                page.locator(".dictionary-results button").first.wait_for(timeout=10000)
                page.get_by_role("button", name="日历页").click()
                assert page.locator(".calendar-month").count() == 25
                page.get_by_role("button", name="词汇星图").click()
                def relations_outside_families() -> bool:
                    return page.locator(".graph-canvas").evaluate("""svg => {
                      const circles = [...svg.querySelectorAll('.family-bubble circle')].map(el => ({
                        x: Number(el.getAttribute('cx')), y: Number(el.getAttribute('cy')), r: Number(el.getAttribute('r'))
                      }));
                      return [...svg.querySelectorAll('.graph-node[data-node-kind="relation_candidate"]')].every(el => {
                        const match = el.getAttribute('transform').match(/translate\\(([-.\\d]+) ([-.\\d]+)\\)/);
                        if (!match) return false;
                        const x = Number(match[1]), y = Number(match[2]);
                        return circles.every(circle => Math.hypot(x - circle.x, y - circle.y) > circle.r + 32);
                      });
                    }""")
                assert relations_outside_families()
                GRAPH_PREVIEW.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(GRAPH_PREVIEW))
                page.locator(".graph-node:not(.is-placeholder)").first.click()
                page.locator(".word-page").wait_for(timeout=10000)
                assert relations_outside_families()
                assert page.locator(".graph-node path").count() == 4
                assert page.locator(".sense-card").count() > 0
                assert page.locator(".sense-card .pending-label").first.inner_text() == "待核验"
                page.locator(".ipa + .audio-button").first.wait_for(timeout=10000)
                assert page.locator(".sense-card").first.locator(".definition-source .sources").count() == 1
                placeholder = page.locator(".graph-node.is-placeholder").first
                before_drag = page.locator(".graph-node").evaluate_all("nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                dragged_id = placeholder.get_attribute("data-node-id")
                bounds = placeholder.bounding_box()
                assert bounds
                page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
                page.mouse.down()
                page.mouse.move(bounds["x"] + bounds["width"] / 2 + 24, bounds["y"] + bounds["height"] / 2 + 18, steps=4)
                page.mouse.up()
                page.locator(".save-state.saved").wait_for(timeout=10000)
                assert any(key.startswith("candidate:") for key in store.state("ui")["graph"]["positions"])
                assert len(store.state("ui")["graph"]["positions"]) > 1
                after_drag = page.locator(".graph-node").evaluate_all("nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                assert any(after_drag.get(key) != value for key, value in before_drag.items() if key != dragged_id)
                page.locator(".content-scroll").evaluate("element => { element.scrollTop = 360; }")
                page.get_by_role("button", name="★ 收藏夹").click()
                page.locator(".tab").filter(has_text="happy").locator("span").nth(1).click()
                page.locator(".word-page").wait_for(timeout=10000)
                assert page.locator(".content-scroll").evaluate("element => element.scrollTop") >= 300
                PREVIEW.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(PREVIEW))
                mobile = browser.new_page(viewport={"width": 390, "height": 844})
                mobile.goto(front_url + "/")
                mobile.locator(".word-page").wait_for(timeout=10000)
                assert mobile.locator(".graph-stage").bounding_box()["height"] >= 250
                literary = mobile.locator(".sense-card").nth(3).locator(".tag").filter(has_text="literary")
                literary.scroll_into_view_if_needed()
                tag_box = literary.bounding_box()
                badge_box = mobile.locator(".sense-card").nth(3).locator(".definition-source").bounding_box()
                assert tag_box and badge_box
                assert tag_box["y"] + tag_box["height"] <= badge_box["y"] or badge_box["y"] + badge_box["height"] <= tag_box["y"] or tag_box["x"] + tag_box["width"] <= badge_box["x"] or badge_box["x"] + badge_box["width"] <= tag_box["x"]
                mobile.screenshot(path=str(PREVIEW.with_name("preview-mobile.png")))
                mobile.close()
                page.locator(".tab").filter(has_text="happy").get_by_role("button", name="固定标签").click()
                page.locator(".tab").filter(has_text="happy").get_by_role("button", name="取消固定标签").click()
                page.get_by_role("button", name="独立显示").click()
                assert page.locator(".workspace-right.independent").count() == 1
                page.get_by_role("button", name="返回分屏").click()
                page.get_by_role("slider", name="图谱缩放比例").fill("150")
                page.get_by_role("button", name="加入收藏夹").click()
                page.get_by_role("button", name="见过").click()
                page.get_by_role("button", name="★ 收藏夹").click()
                page.locator(".favorite-row").first.wait_for()
                page.get_by_role("button", name="⚙ 设置").click()
                assert page.locator("#stage-select").input_value() == "all"
                page.locator(".tab").filter(has_text="happy").locator("span").nth(1).click()
                page.locator(".word-page").wait_for(timeout=10000)
                page.locator(".relation-pill").first.click()
                page.locator(".candidate-page").wait_for(timeout=10000)
                page.locator(".candidate-line").first.wait_for(timeout=10000)
                page.locator(".filter-chip").filter(has_text="同义词").click()
                page.locator(".save-state.saved").wait_for(timeout=10000)
                page.reload()
                page.locator(".candidate-page").wait_for(timeout=10000)
                assert page.locator(".tab").filter(has_text="收藏夹").count() == 1
                assert store.state("favorites")["words"]
                assert store.state("learning")["words"]
                assert store.state("ui")["graph"]["zoomPercent"] == 150
                assert store.state("ui")["graph"]["visibleRelations"]["synonym"] is False
                page.get_by_role("button", name="⚙ 设置").click()
                page.locator("#stage-select").select_option("sample")
                page.locator(".tab").filter(has_text="happy").locator("span").nth(1).click()
                page.get_by_role("button", name="加入当前学龄段").click()
                page.get_by_role("button", name="加入当前学龄段").wait_for(state="detached")
                assert store.stage("sample")["words"]
                page.get_by_role("button", name="项目页").click()
                page.get_by_label("项目名称").fill("20 词界面测试")
                page.locator(".project-create input[type=file]").set_input_files(str(APP.parents[1] / "tmp" / "dicts" / "test_20_words.jsonl"))
                page.get_by_role("button", name="创建项目").click()
                page.wait_for_function("document.querySelectorAll('.graph-node').length === 20", timeout=10000)
                assert page.locator(".graph-node").count() == 20
                assert page.locator(".graph-node.is-placeholder").count() == 20
                unbuilt_id = None
                unbuilt_box = None
                for candidate in page.locator(".graph-node.is-placeholder").all():
                    box = candidate.bounding_box()
                    if not box:
                        continue
                    candidate_id = candidate.get_attribute("data-node-id")
                    hit_id = page.evaluate("([x,y]) => document.elementFromPoint(x,y)?.closest('.graph-node')?.dataset.nodeId", [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
                    if hit_id == candidate_id:
                        unbuilt_id, unbuilt_box = candidate_id, box
                        break
                assert unbuilt_id and unbuilt_box
                page.mouse.move(unbuilt_box["x"] + unbuilt_box["width"] / 2, unbuilt_box["y"] + unbuilt_box["height"] / 2)
                page.mouse.down()
                page.mouse.move(unbuilt_box["x"] + unbuilt_box["width"] / 2 + 35, unbuilt_box["y"] + unbuilt_box["height"] / 2 + 15, steps=4)
                page.mouse.up()
                page.wait_for_function("""async id => {
                  const response = await fetch('/api/bootstrap');
                  return !!(await response.json()).ui.graph.positions[id];
                }""", arg=unbuilt_id, timeout=10000)
                for _ in range(20):
                    if unbuilt_id in store.state("ui")["graph"]["positions"]:
                        break
                    page.wait_for_timeout(100)
                assert unbuilt_id in store.state("ui")["graph"]["positions"]
                project_name = page.locator("#current-project-name")
                project_name.fill("20 词已重命名")
                project_name.press("Tab")
                page.wait_for_timeout(300)
                assert store.project(store.state("ui")["activeProjectId"])["name"] == "20 词已重命名"
                page.get_by_role("button", name="字典页").click()
                page.locator(".dictionary-search input").fill("handily")
                page.locator(".dictionary-results button").first.click()
                page.locator(".candidate-page").wait_for(timeout=10000)
                page.get_by_role("button", name="收藏夹", exact=True).click()
                assert page.locator(".favorite-row").count() == 1
                assert not errors, errors
                browser.close()
        finally:
            frontend.terminate()
            try:
                frontend.wait(timeout=5)
            except subprocess.TimeoutExpired:
                frontend.kill()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("dictionary browser smoke: passed")


if __name__ == "__main__":
    main()
