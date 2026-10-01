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
from smoke import fixture, overview_relation_fixture
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


def assert_graph_stable(page, samples: int = 4) -> None:
    snapshot = """() => ({
      zoom: document.querySelector('[aria-label="图谱缩放比例"]')?.value,
      canvas: document.querySelector('.graph-canvas > g')?.getAttribute('transform'),
      nodes: [...document.querySelectorAll('.graph-node')].map(node =>
        [node.dataset.nodeId, node.getAttribute('transform')]),
    })"""
    expected = page.evaluate(snapshot)
    for _ in range(samples):
        page.wait_for_timeout(300)
        assert page.evaluate(snapshot) == expected, "图谱在无输入时改变了缩放或节点位置"


def assert_family_contacts(page, gap: float = 8) -> None:
    page.wait_for_function("""gap => {
      const nodes = new Map([...document.querySelectorAll('.graph-node')].map(el => {
        const xy = el.getAttribute('transform').match(/[-\\d.]+/g).map(Number);
        return [el.dataset.nodeId, {x: xy[0], y: xy[1], r: Number(el.querySelector('circle').getAttribute('r'))}];
      }));
      const families = [...document.querySelectorAll('.family-bubble')];
      return families.length > 0 && families.every(group => {
        const members = group.dataset.memberIds.split(',').map(id => nodes.get(id));
        if (members.length < 2) return true;
        return members.every((a, i) => members.some((b, j) => i !== j && Math.abs(Math.hypot(a.x-b.x, a.y-b.y)-a.r-b.r-gap) < .02)
          && members.every((b, j) => i === j || Math.hypot(a.x-b.x, a.y-b.y) >= a.r+b.r+gap-.02));
      });
    }""", arg=gap, timeout=10000)


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
        server.daemon_threads = False
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
                assert page.locator('.graph-node[data-node-kind="inflection"]').count() > 0
                assert_family_contacts(page)
                assert page.get_by_role("slider", name="图谱缩放比例").input_value() == "100"
                # Check live application, boundary values, and persisted geometry on reload.
                for configured_gap in (0, 64, 12, 8):
                    page.locator(".page-rail .rail-settings").click()
                    page.get_by_label("同族节点间距", exact=False).fill(str(configured_gap))
                    page.get_by_role("button", name="保存配置").click()
                    page.get_by_role("status").get_by_text("配置已保存并应用").wait_for()
                    assert store.config["graph"]["physics"]["familyNodeGap"] == configured_gap
                    page.get_by_role("button", name="词汇星图", exact=True).click()
                    assert_family_contacts(page, configured_gap)
                    if configured_gap == 12:
                        page.reload()
                        page.locator(".graph-node").first.wait_for()
                        assert_family_contacts(page, configured_gap)
                summaries = store.word_summaries("all")
                for summary in summaries:
                    if not summary["core"]:
                        continue
                    node = page.locator(f'.graph-node[data-node-id="{summary["wordId"]}"]')
                    label = node.locator('text').nth(1).evaluate("el => el.firstChild.textContent")
                    assert summary["core"][0]["partOfSpeech"] in node.get_attribute('aria-label')
                    assert summary["core"][0]["text"] in node.get_attribute('aria-label')
                    assert summary["core"][0]["text"][:2] in label
                    width = node.locator('text').nth(1).evaluate("el => el.getComputedTextLength()")
                    assert width <= 64, '词性释义不能遮盖相邻节点的文字'
                assert '全部已开启' in page.locator('.graph-density-note').text_content()
                page.get_by_role("slider", name="图谱缩放比例").fill("95")
                assert page.locator('.graph-node:not(.is-placeholder)').first.locator('text').count() == 1
                page.get_by_role("slider", name="图谱缩放比例").fill("100")
                # Exercise a visible family subset whose saved positions do not touch.
                distant_pair = page.evaluate("""() => {
                  const nodes = new Map([...document.querySelectorAll('.graph-node')].map(el =>
                    [el.dataset.nodeId, el.getAttribute('transform').match(/[-\\d.]+/g).map(Number)]));
                  for (const group of document.querySelectorAll('.family-bubble')) {
                    const ids = group.dataset.memberIds.split(',');
                    for (let i=0; i<ids.length; i++) for (let j=i+1; j<ids.length; j++) {
                      const a=nodes.get(ids[i]), b=nodes.get(ids[j]);
                      if (Math.hypot(a[0]-b[0], a[1]-b[1]) > 72.1) return [ids[i], ids[j]];
                    }
                  }
                  return null;
                }""")
                assert distant_pair, '测试词族需包含未直接接触的一对节点'
                overview = store.graph()
                subset = {**overview, 'nodes': [node for node in overview['nodes'] if node['wordId'] in distant_pair],
                          'edges': [], 'families': [{'familyId': 'subset', 'nodeIds': distant_pair}], 'hiddenCount': 0}
                entry_pair = [node for node in overview['nodes'] if node['kind'] == 'entry'][:2]
                multiple = {**overview, 'nodes': entry_pair, 'families': [], 'edges': []}
                for kind in ('synonym', 'near_synonym', 'antonym'):
                    multiple['edges'].append({'source': entry_pair[0]['wordId'], 'target': entry_pair[1]['wordId'],
                        'type': kind, 'relationshipId': 'review_' + kind, 'status': 'confirmed', 'ruleClassified': False,
                        'wordLevel': False, 'relations': []})

                def graph_probe(route):
                    if 'search=review_subset' in route.request.url:
                        route.fulfill(json=subset)
                    elif 'search=review_relations' in route.request.url:
                        route.fulfill(json=multiple)
                    else:
                        route.continue_()

                page.route('**/api/graph?**', graph_probe)
                search = page.locator('.graph-search input')
                search.fill('review_subset')
                page.wait_for_function("document.querySelectorAll('.graph-node').length === 2")
                assert_family_contacts(page)
                search.fill('review_relations')
                page.wait_for_function("document.querySelectorAll('.graph-edge').length === 3")
                coordinates = page.locator('.graph-edge').evaluate_all("edges => edges.map(edge => [edge.getAttribute('x1'), edge.getAttribute('y1'), edge.getAttribute('x2'), edge.getAttribute('y2')].join(','))")
                assert len(set(coordinates)) == 3, '总览多类型边不能相互遮盖'
                page.unroute('**/api/graph?**', graph_probe)
                search.fill('')
                page.wait_for_function('count => document.querySelectorAll(".graph-node").length === count', arg=len(overview['nodes']))
                assert_family_contacts(page)
                project_name = page.get_by_label("当前项目名")
                project_name.wait_for()
                page.wait_for_function("!document.querySelector('[aria-label=当前项目名]').disabled")
                project_name.fill("界面重命名测试")
                with page.expect_response(lambda response: response.url.endswith('/api/projects/default')
                                          and response.request.method == 'PUT') as renamed:
                    project_name.press("Enter")
                assert renamed.value.status == 200
                assert store.project("default")["name"] == "界面重命名测试"
                project_name.fill("撤销项目名")
                project_name.press("Escape")
                assert project_name.input_value() == "界面重命名测试"
                assert page.locator(".app-header").count() == 0
                assert page.locator(".page-rail .rail-brand").count() == 1
                page.get_by_role("button", name="项目页").click()
                page.locator(".project-card").first.wait_for(timeout=10000)
                assert page.locator(".project-card").count() == 1
                page.get_by_role("button", name="词典页").click()
                page.locator(".dictionary-search input").fill("pass")
                page.locator(".dictionary-results button").first.wait_for(timeout=10000)
                page.get_by_role("button", name="日历页").click()
                page.locator(".calendar-month").first.wait_for(timeout=10000)
                assert page.locator(".calendar-month").count() == 25
                assert page.locator(".calendar-scroll").evaluate("grid => getComputedStyle(grid).gridTemplateColumns.split(' ').length") == 2
                with urlopen(front_url + "/api/today") as response:
                    calendar_today = json.loads(response.read())["day"]
                page.wait_for_function("""day => {
                  const box = document.querySelector('.calendar-scroll');
                  const target = box.querySelector(`[aria-label="${day}"]`).getBoundingClientRect();
                  const bounds = box.getBoundingClientRect();
                  return box.scrollTop > 0 && target.top >= bounds.top && target.bottom <= bounds.bottom;
                }""", arg=calendar_today)
                assert page.locator('.study-calendar-details h2').text_content() == calendar_today
                page.locator('.calendar-scroll').evaluate('box => { box.scrollTop = 0; }')
                page.locator('.calendar-scroll button').first.click()
                page.wait_for_timeout(300)
                assert page.locator('.calendar-scroll').evaluate('box => box.scrollTop') == 0
                page.get_by_role("button", name="词典页").click()
                page.route('**/api/plans', lambda route: route.fulfill(status=503, json={'error': 'SYNTHETIC_PLAN_FAILURE'}))
                page.get_by_role("button", name="日历页").click()
                page.wait_for_function("document.querySelector('.calendar-scroll')?.scrollTop > 0")
                page.locator('.study-calendar-details .error-note').wait_for()
                assert page.locator('.study-calendar-details h2').text_content() == calendar_today
                page.unroute('**/api/plans')
                page.get_by_role("button", name="背单词页").click()
                page.get_by_role("button", name="新建背诵计划").click()
                assert page.get_by_role("dialog", name="新建背诵计划").is_visible()
                with urlopen(front_url + "/api/today") as response:
                    today = json.loads(response.read())["day"]
                assert page.locator(".study-calendar-scroll .study-calendar-month").evaluate_all("items => items[12].querySelector('h3').textContent") == f"{int(today[:4])} 年 {int(today[5:7])} 月"
                assert page.locator(".study-calendar-scroll .range-start").get_attribute("aria-label") == today
                page.wait_for_function("document.querySelector('.study-calendar-scroll').scrollTop > 0", timeout=10000)
                page.get_by_role("dialog").get_by_label("要背完一遍的天数").fill("2")
                assert page.locator(".study-calendar-scroll .in-range").count() == 2
                assert page.locator(".study-calendar-scroll .range-end").get_attribute("aria-label") != today
                page.get_by_role("dialog").get_by_label("每天要背的单词数").fill("50")
                page.get_by_role("dialog").get_by_role("button", name="创建计划").click()
                page.locator(".study-plan-view").wait_for(timeout=10000)
                page.locator(".study-star:not([disabled])").first.click()
                page.wait_for_function("document.querySelector('.study-star')?.getAttribute('aria-pressed') === 'true'")
                page.locator(".study-star").first.click()
                page.wait_for_function("document.querySelector('.study-star')?.getAttribute('aria-pressed') === 'false'")
                with page.expect_response(lambda response: '/api/plans/' in response.url and response.request.method == 'PATCH') as shuffled:
                    page.locator(".study-plan-controls .study-switch input[type=checkbox]").check()
                assert shuffled.value.status == 200
                page.get_by_role("button", name="组内乱序").wait_for()
                with page.expect_response(lambda response: '/api/plans/' in response.url and response.request.method == 'PATCH') as shuffled:
                    page.locator(".study-plan-controls .study-switch input[type=checkbox]").uncheck()
                assert shuffled.value.status == 200
                page.get_by_role("button", name="组内乱序").wait_for(state='detached')
                assert page.locator(".study-plan-controls input[type=date]").input_value() == today
                page.locator(".study-mode-actions input[type=checkbox]").check()
                page.get_by_role("button", name="显示所有答案").click()
                assert page.locator(".study-answer.is-hidden").count() == 0
                page.get_by_role("button", name="隐藏所有答案").click()
                assert page.locator(".study-answer.is-hidden").count() == 2
                page.locator(".study-row").first.locator(".study-switch input[type=checkbox]").check()
                page.locator(".study-plan-card").filter(has_text="1 / 2").wait_for(timeout=10000)
                assert "本页已通过 50%（1/2）" in page.locator(".study-progress-summary").inner_text()
                page.get_by_role("button", name="下一组").click()
                page.locator(".study-plan-controls").get_by_text("第 1 次重试").wait_for(timeout=10000)
                assert page.locator(".study-row").count() == 1
                assert "本页已通过 0%（0/1）" in page.locator(".study-progress-summary").inner_text()
                assert "今天已通过 50%（1/2）" in page.locator(".study-progress-summary").inner_text()
                page.get_by_role("button", name="下一组").click()
                page.locator(".study-plan-controls").get_by_text("第 2 次重试").wait_for(timeout=10000)
                page.locator(".study-plan-controls input[type=checkbox]").check()
                page.get_by_role("button", name="组间乱序").click()
                assert page.get_by_role("button", name="组间乱序").get_attribute("aria-pressed") == "true"
                page.get_by_role("button", name="日历页").click()
                page.locator(".calendar-grid button.has-plan").first.wait_for(timeout=10000)
                page.get_by_role("button", name="背单词页").click()
                page.locator(".study-plan-card-actions").get_by_role("button", name="设置").click()
                assert page.get_by_role("dialog", name="设置背诵计划").is_visible()
                page.get_by_role("dialog").get_by_label("要背完一遍的天数").fill("2")
                page.get_by_role("dialog").get_by_role("button", name="保存设置").click()
                page.get_by_role("dialog", name="设置背诵计划").wait_for(state="detached", timeout=10000)
                assert page.locator(".study-plan-card").first.inner_text().find("0 / 2") >= 0
                page.locator(".study-plan-card-actions").get_by_role("button", name="设置").click()
                page.get_by_role("dialog").get_by_label("计划名（可选）").fill("")
                page.get_by_role("dialog").get_by_role("button", name="保存设置").click()
                page.get_by_role("dialog", name="设置背诵计划").wait_for(state="detached", timeout=10000)
                assert "艾宾浩斯" in page.locator(".study-plan-card").first.inner_text()
                page.locator(".study-plan-card-actions").get_by_role("button", name="删除").click()
                page.get_by_role("dialog", name="确认删除背诵计划").get_by_role("button", name="确认删除").click()
                page.locator(".study-plan-card").first.wait_for(state="detached", timeout=10000)
                assert page.locator(".study-tab").count() == 0
                page.get_by_role("button", name="新建背诵计划").click()
                page.get_by_role("dialog").get_by_label("记忆方法").select_option("hulu")
                page.get_by_role("dialog").get_by_role("button", name="创建计划").click()
                page.locator(".study-plan-view").wait_for(timeout=10000)
                assert "葫芦背书法" in page.locator(".study-plan-card").first.inner_text()
                page.locator(".page-rail .rail-settings").click()
                page.get_by_label("每页单词数").fill("1")
                page.get_by_role("button", name="保存配置").click()
                page.get_by_role("status").get_by_text("配置已保存并应用").wait_for(timeout=10000)
                page.get_by_role("button", name="背单词页").click()
                page.wait_for_function("document.querySelectorAll('.study-row').length === 1", timeout=10000)
                assert page.locator(".study-row").count() == 1
                page.locator(".study-mode-actions input[type=checkbox]").check()
                assert page.locator(".study-answer.is-hidden").count() == 1
                page.keyboard.press("Space")
                assert page.locator(".study-answer.is-hidden").count() == 0
                page.get_by_role("button", name="下一组").click()
                assert not page.locator(".study-mode-actions input[type=checkbox]").is_checked()
                assert "未达到 80%" in page.locator(".study-plan-view .error-note").inner_text()
                page.locator(".study-mode-actions input[type=checkbox]").check()
                page.locator(".study-row").first.locator(".study-switch input[type=checkbox]").check()
                page.locator(".study-plan-card").filter(has_text="1 / 2").wait_for(timeout=10000)
                page.keyboard.press("ArrowRight")
                page.locator(".study-pagination").get_by_text("2 / 2").wait_for(timeout=10000)
                assert not page.locator(".study-mode-actions input[type=checkbox]").is_checked()
                page.locator(".study-mode-actions input[type=checkbox]").check()
                page.get_by_role("button", name="下一组").click()
                assert not page.locator(".study-mode-actions input[type=checkbox]").is_checked()
                page.locator(".study-mode-actions input[type=checkbox]").check()
                page.locator(".study-row").first.locator(".study-switch input[type=checkbox]").check()
                page.locator(".study-plan-card").filter(has_text="2 / 2").wait_for(timeout=10000)
                page.get_by_role("button", name="下一组").click()
                assert page.locator(".study-plan-view .error-note").count() == 0
                page.locator(".study-plan-card-actions").get_by_role("button", name="删除").click()
                page.get_by_role("dialog", name="确认删除背诵计划").get_by_role("button", name="确认删除").click()
                page.get_by_role("button", name="词汇星图").click()
                def relations_outside_families() -> bool:
                    return page.locator(".graph-canvas").evaluate("""svg => {
                      const circles = [...svg.querySelectorAll('.family-bubble')].map(group => {
                        const el = group.querySelector('circle');
                        return { members: new Set(group.dataset.memberIds.split(',')),
                          x: Number(el.getAttribute('cx')), y: Number(el.getAttribute('cy')), r: Number(el.getAttribute('r')) };
                      });
                      return [...svg.querySelectorAll('.graph-node')].every(el => {
                        const match = el.getAttribute('transform').match(/translate\\(([-.\\d]+) ([-.\\d]+)\\)/);
                        if (!match) return false;
                        const x = Number(match[1]), y = Number(match[2]);
                        return circles.every(circle => circle.members.has(el.dataset.nodeId) || Math.hypot(x - circle.x, y - circle.y) > circle.r + 32);
                      });
                    }""")
                assert relations_outside_families()
                GRAPH_PREVIEW.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(GRAPH_PREVIEW))
                before_initialize_revision = store.state("ui")["revision"]
                page.locator(".filter-chip").filter(has_text="初始化").click()
                for _ in range(50):
                    if store.state("ui")["revision"] > before_initialize_revision:
                        break
                    page.wait_for_timeout(100)
                assert store.state("ui")["revision"] > before_initialize_revision
                assert_graph_stable(page)
                assert_family_contacts(page)
                page.locator(".graph-canvas").evaluate(
                    "canvas => canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 0 }))")
                assert_graph_stable(page, samples=1)
                page.locator(".save-state.saved").wait_for(timeout=10000)
                wheel_zoom = int(page.get_by_role("slider", name="图谱缩放比例").input_value())
                page.locator(".graph-canvas").evaluate(
                    "canvas => canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -1 }))")
                page.locator(".graph-canvas").evaluate(
                    "canvas => canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 1 }))")
                assert_graph_stable(page, samples=1)
                assert page.get_by_role("slider", name="图谱缩放比例").input_value() == str(wheel_zoom)
                page.locator(".graph-canvas").evaluate(
                    "canvas => canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, altKey: true, deltaY: -1 }))")
                page.wait_for_function("value => Number(document.querySelector('[aria-label=\"图谱缩放比例\"]')?.value) === value", arg=wheel_zoom + 10)
                page.locator(".graph-canvas").evaluate(
                    "canvas => canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, altKey: true, deltaY: 1 }))")
                page.wait_for_function("value => Number(document.querySelector('[aria-label=\"图谱缩放比例\"]')?.value) === value", arg=wheel_zoom)
                page.locator(".graph-canvas").evaluate("""canvas => {
                  for (const deltaY of [-1, 1, -1, -1, 1]) {
                    canvas.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY }));
                  }
                }""")
                assert_graph_stable(page)
                assert page.get_by_role("slider", name="图谱缩放比例").input_value() == str(wheel_zoom)
                assert store.state("ui")["graph"]["zoomPercent"] == wheel_zoom
                canvas_bounds = page.locator(".graph-canvas").bounding_box()
                assert canvas_bounds
                page.mouse.move(canvas_bounds["x"] + canvas_bounds["width"] / 2,
                                canvas_bounds["y"] + canvas_bounds["height"] / 2)
                page.keyboard.down("Alt")
                page.mouse.wheel(0, -100)
                page.wait_for_function("value => Number(document.querySelector('[aria-label=\"图谱缩放比例\"]')?.value) > value", arg=wheel_zoom)
                page.keyboard.up("Alt")
                page.locator(".save-state.saved").wait_for(timeout=10000)
                real_wheel_zoom = page.get_by_role("slider", name="图谱缩放比例").input_value()
                page.wait_for_timeout(2000)
                page.mouse.wheel(0, -100)
                assert_graph_stable(page, samples=1)
                assert page.get_by_role("slider", name="图谱缩放比例").input_value() == real_wheel_zoom
                assert store.state("ui")["graph"]["zoomPercent"] == int(real_wheel_zoom)
                page.get_by_role("slider", name="图谱缩放比例").fill(str(wheel_zoom))
                page.wait_for_function("value => Number(document.querySelector('[aria-label=\"图谱缩放比例\"]')?.value) === value", arg=wheel_zoom)
                page.locator(".save-state.saved").wait_for(timeout=10000)
                assert store.state("ui")["graph"]["zoomPercent"] == wheel_zoom
                baseline_zoom = page.get_by_role("slider", name="图谱缩放比例").input_value()
                baseline_canvas = page.locator(".graph-canvas > g").get_attribute("transform")
                baseline_nodes = page.locator(".graph-node").evaluate_all(
                    "nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                assert page.locator(".graph-node text").first.evaluate("el => getComputedStyle(el).userSelect") == "none"
                focused_id = page.locator(".graph-node:not(.is-placeholder)").first.get_attribute("data-node-id")
                page.locator(f'.graph-node[data-node-id="{focused_id}"]').click()
                page.locator(".word-page").wait_for(timeout=10000)
                page.wait_for_timeout(1200)
                assert page.locator(".word-page").count() == 1
                assert page.get_by_role("slider", name="图谱缩放比例").input_value() == "125"
                assert_family_contacts(page)
                assert_graph_stable(page)
                page.keyboard.press("Escape")
                page.wait_for_function("value => document.querySelector('[aria-label=\"图谱缩放比例\"]')?.value === value", arg=baseline_zoom)
                page.wait_for_function("count => document.querySelectorAll('.graph-node').length === count", arg=len(baseline_nodes))
                restored_nodes = page.locator(".graph-node").evaluate_all(
                    "nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                assert restored_nodes == baseline_nodes
                assert page.locator(".graph-canvas > g").get_attribute("transform") == baseline_canvas
                assert_graph_stable(page)
                assert page.locator(".word-page").count() == 1
                with page.expect_response(lambda response: "/api/graph?" in response.url and "selected=" in response.url and response.ok):
                    page.locator(f'.graph-node[data-node-id="{focused_id}"]').click()
                page.wait_for_timeout(200)
                page.locator(".save-state.saved").wait_for(timeout=10000)
                selected_initialize_revision = store.state("ui")["revision"]
                page.locator(".filter-chip").filter(has_text="初始化").click()
                for _ in range(50):
                    if store.state("ui")["revision"] > selected_initialize_revision:
                        break
                    page.wait_for_timeout(100)
                assert store.state("ui")["revision"] > selected_initialize_revision
                assert_graph_stable(page)
                selected_baseline_zoom = page.get_by_role("slider", name="图谱缩放比例").input_value()
                selected_baseline_nodes = page.locator(".graph-node").evaluate_all(
                    "nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                page.keyboard.press("Escape")
                page.wait_for_function("value => document.querySelector('[aria-label=\"图谱缩放比例\"]')?.value === value", arg=selected_baseline_zoom)
                page.wait_for_function("count => document.querySelectorAll('.graph-node').length === count", arg=len(baseline_nodes))
                restored_selected_nodes = page.locator(".graph-node").evaluate_all(
                    "nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                common_nodes = selected_baseline_nodes.keys() & restored_selected_nodes.keys()
                assert common_nodes
                # The selected disk shrinks on cancellation, so contact projection
                # may adjust positions while preserving the initialized viewport.
                assert_family_contacts(page)
                assert_graph_stable(page)
                page.locator(f'.graph-node[data-node-id="{focused_id}"]').click()
                page.locator(".word-page").wait_for(timeout=10000)
                split_before = store.state("ui")["split"]["leftWidthPercent"]
                handle = page.locator(".split-handle").bounding_box()
                assert handle
                page.mouse.move(handle["x"] + handle["width"] / 2, handle["y"] + handle["height"] / 2)
                page.mouse.down()
                page.mouse.move(handle["x"] + handle["width"] / 2 + 80,
                                handle["y"] + handle["height"] / 2, steps=5)
                page.mouse.up()
                for _ in range(30):
                    if store.state("ui")["split"]["leftWidthPercent"] > split_before:
                        break
                    page.wait_for_timeout(100)
                assert store.state("ui")["split"]["leftWidthPercent"] > split_before
                assert page.locator(".word-page").count() == 1
                word_tab_name = page.locator(".tab.active").inner_text().replace("◌", "").strip().splitlines()[0]
                assert relations_outside_families()
                assert page.locator(".graph-node path").count() == 4
                assert page.locator(".sense-card").count() > 0
                assert page.locator(".sense-card .pending-label").first.inner_text() == "待核验"
                page.locator(".ipa + .audio-button").first.wait_for(timeout=10000)
                assert page.locator(".sense-card").first.locator(".definition-source .sources").count() == 1
                before_drag = page.locator(".graph-node").evaluate_all("nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                dragged_id, bounds = None, None
                for placeholder in page.locator(".graph-node.is-placeholder").all():
                    box = placeholder.bounding_box()
                    if not box:
                        continue
                    candidate_id = placeholder.get_attribute("data-node-id")
                    hit_id = page.evaluate("([x,y]) => document.elementFromPoint(x,y)?.closest('.graph-node')?.dataset.nodeId",
                                           [box["x"] + box["width"] / 2, box["y"] + box["height"] / 2])
                    if hit_id == candidate_id:
                        dragged_id, bounds = candidate_id, box
                        break
                assert dragged_id and bounds
                page.mouse.move(bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
                page.mouse.down()
                page.mouse.move(bounds["x"] + bounds["width"] / 2 + 24, bounds["y"] + bounds["height"] / 2 + 18, steps=4)
                page.mouse.up()
                page.locator(".save-state.saved").wait_for(timeout=10000)
                assert any(key.startswith("w_") for key in store.state("ui")["graph"]["positions"])
                assert len(store.state("ui")["graph"]["positions"]) > 1
                page.wait_for_function("""({before, dragged}) => [...document.querySelectorAll('.graph-node')].some(
                    node => node.dataset.nodeId !== dragged && before[node.dataset.nodeId] !== node.getAttribute('transform'))""",
                    arg={"before": before_drag, "dragged": dragged_id}, timeout=5000)
                after_drag = page.locator(".graph-node").evaluate_all("nodes => Object.fromEntries(nodes.map(node => [node.dataset.nodeId, node.getAttribute('transform')]))")
                assert any(after_drag.get(key) != value for key, value in before_drag.items() if key != dragged_id)
                assert_family_contacts(page)
                page.locator(".content-scroll").evaluate("element => { element.scrollTop = 360; }")
                page.get_by_role("button", name="收藏夹", exact=True).click()
                page.locator(".tab").filter(has_text=word_tab_name).locator("span").nth(1).click()
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
                page.locator(".tab").filter(has_text=word_tab_name).get_by_role("button", name="固定标签").click()
                page.locator(".tab").filter(has_text=word_tab_name).get_by_role("button", name="取消固定标签").click()
                page.locator(".tab").filter(has_text=word_tab_name).get_by_role("button", name="固定标签").click()
                page.get_by_role("button", name="独立显示").click()
                page.locator(".workspace-right.independent").wait_for(timeout=10000)
                page.get_by_role("button", name="返回分屏").click()
                page.get_by_role("slider", name="图谱缩放比例").fill("150")
                page.get_by_role("button", name="加入收藏夹").click()
                learning_before_conflict = store.state("learning", "default")
                store.patch_learning(focused_id, "unfamiliar", learning_before_conflict["revision"], "default")
                page.get_by_role("button", name="见过").click()
                page.get_by_role("button", name="熟悉", exact=True).click()
                for _ in range(50):
                    if store.state("learning", "default")["words"].get(focused_id) == "familiar":
                        break
                    page.wait_for_timeout(100)
                assert store.state("learning", "default")["words"].get(focused_id) == "familiar"
                assert page.locator(".toast").filter(has_text="熟悉度未保存").count() == 0
                page.get_by_role("button", name="收藏夹", exact=True).click()
                page.locator(".favorite-row").first.wait_for()
                page.get_by_role("button", name="设置", exact=True).click()
                assert page.locator("#stage-select").input_value() == "all"
                page.get_by_label("每页单词数").fill("7")
                page.locator(".settings-grid label").filter(has_text="背景").first.locator("input[type=text]").fill("#123456")
                page.get_by_role("button", name="保存配置").click()
                page.get_by_role("status").get_by_text("配置已保存并应用").wait_for(timeout=10000)
                assert store.config["study"]["wordsPerPage"] == 7, (store.config["study"],
                    page.get_by_label("每页单词数").input_value())
                assert page.locator(".app-shell").evaluate("el => el.style.getPropertyValue('--bg')") == "#123456"
                page.locator(".tab").filter(has_text=word_tab_name).locator("span").nth(1).click()
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
                page.get_by_role("button", name="设置", exact=True).click()
                page.locator("#stage-select").select_option("sample")
                page.locator(".tab").filter(has_text=word_tab_name).locator("span").nth(1).click()
                page.get_by_role("button", name="加入当前学龄段").click()
                page.get_by_role("button", name="加入当前学龄段").wait_for(state="detached")
                assert store.stage("sample")["words"]
                page.get_by_role("button", name="项目页").click()
                page.locator(".project-card .project-preview").first.click()
                assert page.locator(".project-preview-pane span").count() == 2
                page.get_by_role("button", name="新建项目").click()
                page.get_by_label("项目名").fill("20 词界面测试")
                page.locator(".project-modal input[type=file]").set_input_files(str(APP.parents[1] / "tmp" / "dicts" / "test_20_words.jsonl"))
                page.get_by_role("button", name="创建项目").click()
                page.wait_for_function("document.querySelectorAll('.graph-node').length === 20", timeout=10000)
                assert page.locator(".graph-node").count() == 20
                assert page.locator(".graph-node.is-placeholder").count() == 20
                assert relations_outside_families()
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
                assert relations_outside_families()
                page.get_by_role("button", name="项目页").click()
                page.locator(".project-card").filter(has_text="20 词界面测试").get_by_role("button", name="设置20 词界面测试").click()
                page.get_by_label("项目名").fill("20 词已重命名")
                page.locator(".project-modal").get_by_role("button", name="保存").click()
                page.locator(".project-modal").wait_for(state="detached")
                assert store.project(store.state("ui")["activeProjectId"])["name"] == "20 词已重命名"
                page.get_by_role("button", name="词典页").click()
                page.locator(".dictionary-search input").fill("handily")
                page.locator(".dictionary-results button").first.click()
                page.locator(".candidate-page").wait_for(timeout=10000)
                page.get_by_role("button", name="收藏夹", exact=True).click()
                page.locator(".favorite-row").first.wait_for(timeout=10000)
                assert page.locator(".favorite-row").count() == 1
                page.get_by_role("button", name="设置", exact=True).click()
                page.locator("#stage-select").select_option("all")
                # Check complete spelling edges independently of the 500-node test.
                page.get_by_role("button", name="项目页").click()
                page.get_by_role("button", name="新建项目").click()
                page.get_by_label("项目名").fill("全部拼写边测试")
                page.locator(".project-modal textarea").fill("planet\nplaneta\nplanetb\nplanetc\nplanetd\nplanete")
                page.get_by_role("button", name="创建项目").click()
                page.wait_for_function("document.querySelectorAll('.graph-node').length === 6 && document.querySelectorAll('.graph-edge').length === 15", timeout=10000)
                spelling_button = page.locator('.filter-chip').filter(has_text='拼写相似词')
                spelling_button.click()
                page.wait_for_function("document.querySelectorAll('.graph-edge').length === 0")
                spelling_button.click()
                page.wait_for_function("document.querySelectorAll('.graph-edge').length === 15")
                spelling_button.click()
                page.wait_for_function("document.querySelectorAll('.graph-edge').length === 0")
                page.get_by_role("button", name="项目页").click()
                page.get_by_role("button", name="新建项目").click()
                page.get_by_label("项目名").fill("大词表图谱测试")
                page.locator(".project-modal textarea").fill("pass\n" + "\n".join(
                    "sampleword" + "".join(chr(97 + (index // divisor) % 26) for divisor in (676, 26, 1))
                    for index in range(600)))
                # Large imports maintain a dense global index before returning.
                with page.expect_response(lambda response: response.url.endswith('/api/projects')
                                          and response.request.method == 'POST', timeout=60000) as imported:
                    page.get_by_role("button", name="创建项目").click()
                assert imported.value.status == 200
                page.wait_for_function("document.querySelectorAll('.graph-node').length === 500", timeout=20000)
                assert page.locator(".graph-node:not(.is-placeholder)").count() == 1
                page.locator(".graph-node:not(.is-placeholder)").first.evaluate(
                    "node => node.dispatchEvent(new MouseEvent('click', { bubbles: true }))")
                page.wait_for_function("document.querySelectorAll('.graph-node').length < 100", timeout=10000)
                page.locator(".word-page").wait_for(timeout=10000)
                # A new candidate replaces the single unpinned word preview.
                page.locator(".relation-pill").first.click()
                page.locator(".candidate-page").wait_for()
                page.locator(".save-state.saved").wait_for()
                saved_tabs = store.state("ui")["tabs"]
                previews = [tab for tab in saved_tabs if tab["kind"] in ("word", "candidate") and not tab["pinned"]]
                assert len(previews) == 1 and previews[0]["kind"] == "candidate"
                assert not any(tab["kind"] == "word" and tab["wordId"] == focused_id and not tab["pinned"] for tab in saved_tabs)
                assert any(tab["kind"] == "word" and tab["pinned"] for tab in saved_tabs)
                candidate_tab_id = previews[0]["tabId"]
                page.locator(".tab.active").dblclick()
                page.locator(".tab.active").get_by_role("button", name="取消固定标签").wait_for()
                page.get_by_role("button", name="词典页").click()
                page.locator(".dictionary-search input").fill("pass")
                page.locator(".dictionary-results button").first.click()
                page.locator(".word-page").wait_for()
                page.locator(".save-state.saved").wait_for()
                assert any(tab["tabId"] == candidate_tab_id and tab["pinned"] for tab in store.state("ui")["tabs"])

                page.get_by_role("button", name="项目页").click()
                page.locator(".project-card").filter(has_text="大词表图谱测试").get_by_role("button", name="预览", exact=True).click()
                pane = page.locator(".project-preview-pane")
                geometry = pane.evaluate("""element => {
                  const header = element.querySelector('header'), count = element.querySelector(':scope > p');
                  const listing = element.querySelector(':scope > div');
                  const before = [header.getBoundingClientRect().y, count.getBoundingClientRect().y];
                  listing.scrollTop = listing.scrollHeight;
                  return {before, after: [header.getBoundingClientRect().y, count.getBoundingClientRect().y],
                          listScroll: listing.scrollTop, paneScroll: element.scrollTop};
                }""")
                assert geometry["listScroll"] > 0
                assert geometry["before"] == geometry["after"]
                assert geometry["paneScroll"] == 0
                pair_project = overview_relation_fixture(store)
                page.reload()
                page.get_by_role("button", name="项目页").click()
                page.locator(".project-card").filter(has_text=pair_project["name"]).locator(".project-open").click()
                page.wait_for_function("document.querySelectorAll('.graph-node').length === 2 && document.querySelectorAll('.graph-edge').length === 1")
                edge = page.locator('.graph-edge')
                assert edge.get_attribute('stroke') == store.config['graph']['relations']['near_synonym']
                assert edge.get_attribute('stroke-dasharray') == '5 5'
                makeshift = next(row for row in pair_project['words'] if row['lemma'] == 'makeshift')
                node = page.locator(f'.graph-node[data-node-id="{makeshift["wordId"]}"]')
                assert node.locator('text').first.text_content() == 'makeshift'
                page.get_by_role('button', name='近义词', exact=True).click()
                page.wait_for_function("document.querySelectorAll('.graph-edge').length === 0")
                page.get_by_role('button', name='近义词', exact=True).click()
                page.wait_for_function("document.querySelectorAll('.graph-edge').length === 1")
                node.click()
                page.locator('.word-page h1').filter(has_text='makeshift').wait_for()
                mark = page.locator('.relation-mark.relation-near_synonym')
                mark.wait_for()
                assert mark.evaluate('el => getComputedStyle(el).backgroundColor') == 'rgb(14, 165, 233)'
                assert page.locator('.source-badge.ai').count() >= 5
                assert page.locator('.source-badge.ai').filter(has_text='置信度 0.95').count() >= 5
                assert page.locator('.confidence-note').count() == 0
                assert '多项 AI 内容使用相同置信度' not in page.locator('.word-page').inner_text()
                # Relation blocks and canvas edges follow a saved custom color together.
                page.locator('.page-rail .rail-settings').click()
                page.locator('.settings-grid label').filter(has_text='关系颜色 / 近义词').locator('input[type=text]').fill('#67C5E8')
                page.get_by_role('button', name='保存配置').click()
                page.get_by_role('status').get_by_text('配置已保存并应用').wait_for()
                page.get_by_role('button', name='词汇星图', exact=True).click()
                page.keyboard.press('Escape')
                node.click()
                page.locator('.word-page h1').filter(has_text='makeshift').wait_for()
                assert page.locator('.relation-mark.relation-near_synonym').evaluate('el => getComputedStyle(el).backgroundColor') == 'rgb(103, 197, 232)'
                assert page.locator('.graph-edge').get_attribute('stroke') == '#67C5E8'
                page.keyboard.press('Escape')
                page.locator('.graph-density-note').wait_for()
                page.wait_for_function("document.querySelectorAll('.graph-edge').length === 1")
                # Reload restores focus when a word tab is active; select the graph tab
                # to verify a persisted overview rather than accidentally testing focus.
                page.locator('.tab').first.click()
                page.locator('.save-state.saved').wait_for()
                page.reload()
                page.locator('.graph-density-note').wait_for()
                page.wait_for_function("document.querySelectorAll('.graph-node').length === 2 && document.querySelectorAll('.graph-edge').length === 1")
                assert page.locator('.graph-edge').get_attribute('stroke') == '#67C5E8'
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
