"""Restart safety and launch failure regressions; all process fixtures are synthetic."""
import importlib.util
import base64
import json
import os
import socket
import subprocess
import sys
import threading
from contextlib import nullcontext
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from utils.scripts import local_app_process as processes
from utils.scripts.workflow_checkpoint import create_run_directory

APP = Path(__file__).resolve().parent
SCRIPTS = (APP / 'scripts/start.py', APP / 'scripts/workspace_service.py',
           APP / 'node_modules/vite/bin/vite.js', APP / 'run.ps1')


def process(pid, script, parent=0, name='python.exe'):
    return {'ProcessId': pid, 'ParentProcessId': parent, 'Name': name,
            'CommandLine': f'"{name}" -B -u "{script}"', 'CreationDate': 'synthetic'}


@pytest.mark.skipif(os.name != 'nt', reason='Windows process command lines')
def test_old_launcher_and_venv_wrapper_are_stopped_once():
    snapshot = {'processes': [process(10, SCRIPTS[0]), process(11, SCRIPTS[1], 10),
                              process(12, SCRIPTS[1], 11)],
                'listeners': [{'LocalPort': 5178, 'OwningProcess': 12}]}
    assert [item['ProcessId'] for item in processes.restart_plan(snapshot, SCRIPTS, 99)] == [10]


@pytest.mark.skipif(os.name != 'nt', reason='Windows process command lines')
def test_foreign_listener_blocks_the_entire_restart():
    snapshot = {'processes': [process(10, SCRIPTS[1]), process(20, APP / 'other.py')],
                'listeners': [{'LocalPort': 5178, 'OwningProcess': 10},
                              {'LocalPort': 5177, 'OwningProcess': 20}]}
    with pytest.raises(RuntimeError, match='5177.*其他程序'):
        processes.restart_plan(snapshot, SCRIPTS, 99)


@pytest.mark.skipif(os.name != 'nt', reason='Windows process command lines')
def test_protect_current_launcher_and_its_ancestors():
    snapshot = {'processes': [process(10, SCRIPTS[0]), process(99, SCRIPTS[0], 10)], 'listeners': []}
    assert processes.restart_plan(snapshot, SCRIPTS, 99) == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows process command lines')
def test_match_script_not_arbitrary_argument_or_substring():
    assert not processes.owns_process(process(10, APP / 'other.py'), SCRIPTS)
    item = process(11, SCRIPTS[0])
    item['CommandLine'] = f'python.exe -c "print(1)" "{SCRIPTS[0]}"'
    assert not processes.owns_process(item, SCRIPTS)
    item['Name'] = 'other.exe'
    assert not processes.owns_process(item, SCRIPTS)
    item = process(12, SCRIPTS[2], name='node.exe')
    item['CommandLine'] = f'node.exe "{APP / "node_modules/.bin/../vite/bin/vite.js"}" --port 5177'
    assert processes.owns_process(item, SCRIPTS)


@pytest.mark.skipif(os.name != 'nt', reason='Windows process command lines')
def test_utf8_launcher_and_dedicated_powershell_window():
    item = process(10, SCRIPTS[0])
    item['CommandLine'] = f'python.exe -B -X utf8 "{SCRIPTS[0]}" --port 5177'
    assert processes.owns_process(item, SCRIPTS)
    item['Name'] = 'powershell.exe'
    item['CommandLine'] = f'powershell.exe -NoProfile -NoExit -File "{SCRIPTS[3]}"'
    assert processes.owns_process(item, SCRIPTS)
    item['CommandLine'] = f'powershell.exe -Command Write-Host "{SCRIPTS[3]}"'
    assert not processes.owns_process(item, SCRIPTS)


@pytest.mark.skipif(os.name != 'nt', reason='Windows process command lines')
def test_recycled_pid_is_not_stopped(monkeypatch):
    old = process(10, SCRIPTS[1])
    new = {**old, 'CreationDate': 'different-synthetic'}
    snapshots = iter([{'processes': [old], 'listeners': []}, {'processes': [new], 'listeners': []}])
    monkeypatch.setattr(processes, 'windows_snapshot', lambda ports: next(snapshots))
    stopped = []
    monkeypatch.setattr(processes, 'stop_tree', stopped.append)
    with pytest.raises(RuntimeError, match='身份发生变化'):
        processes.restart_application((5177, 5178), SCRIPTS)
    assert not stopped


def test_invalid_ports_leave_failed_checkpoint(monkeypatch):
    spec = importlib.util.spec_from_file_location('learning_startup_test', APP / 'scripts/start.py')
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    created = []
    create = startup.create_run_directory
    def capture(root):
        result = create(root)
        created.append(result)
        return result
    monkeypatch.setattr(startup, 'create_run_directory', capture)
    monkeypatch.setattr(sys, 'argv', ['start.py', '--port', '5178', '--service-port', '5178'])
    assert startup.main() == 1
    saved = json.loads((created[0] / 'state.json').read_text(encoding='utf-8'))
    assert saved['stateHistory'] == ['prepared', 'checking', 'cleaning', 'failed']
    assert '不能相同' in (created[0] / 'startup.log').read_text(encoding='utf-8')


@pytest.mark.skipif(os.name != 'nt', reason='Windows listener inventory')
def test_real_foreign_port_is_preserved():
    with socket.socket() as listener, socket.socket() as free:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        free.bind(('127.0.0.1', 0))
        gui_port, service_port = listener.getsockname()[1], free.getsockname()[1]
        free.close()
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(SCRIPTS[0]),
                                 '--port', str(gui_port), '--service-port', str(service_port)],
                                capture_output=True, encoding='utf-8', timeout=30)
        assert result.returncode == 1
        assert '其他程序' in result.stderr
        assert listener.fileno() >= 0
        with socket.create_connection(('127.0.0.1', gui_port), timeout=2):
            pass


def test_cors_preflight_for_write_interfaces():
    spec = importlib.util.spec_from_file_location('learning_cors_test', APP / 'scripts/workspace_service.py')
    service = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service)
    server = service.ThreadingHTTPServer(('127.0.0.1', 0), service.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ('/api/config/interactive-tutor', '/api/projects/20261003T000000/actions'):
            request = Request(f'http://127.0.0.1:{server.server_port}{path}', method='OPTIONS',
                              headers={'Origin': 'http://127.0.0.1:5177'})
            with urlopen(request, timeout=3) as response:
                assert response.status == 204
                assert response.headers['Access-Control-Allow-Origin'] == 'http://127.0.0.1:5177'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_repeat_launch_waits_for_startup_lock():
    path = create_run_directory(ROOT / 'logs/交互式学习/runs') / 'test-startup.lock'
    started, entered = threading.Event(), threading.Event()
    errors = []
    def acquire():
        started.set()
        try:
            with processes.startup_lock(path, timeout=3):
                entered.set()
        except Exception as error:
            errors.append(error)
    with processes.startup_lock(path):
        thread = threading.Thread(target=acquire)
        thread.start()
        assert started.wait(1)
        assert not entered.wait(.1)
    thread.join(timeout=4)
    assert entered.is_set() and not errors and not thread.is_alive()
    assert not path.exists()


def test_interrupted_build_cleans_command_tree(monkeypatch):
    class Child:
        def wait(self):
            raise KeyboardInterrupt
    child, stopped = Child(), []
    monkeypatch.setattr(processes.subprocess, 'Popen', lambda *args, **kwargs: child)
    monkeypatch.setattr(processes, 'stop_process', stopped.append)
    with pytest.raises(KeyboardInterrupt):
        processes.run_logged_command(['synthetic-build'], cwd=APP, env={}, log=None)
    assert stopped == [child]


def test_gui_start_failure_cleans_service_and_gui(monkeypatch):
    spec = importlib.util.spec_from_file_location('learning_failure_test', APP / 'scripts/start.py')
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    run = create_run_directory(ROOT / 'logs/交互式学习/runs')
    service, gui = object(), object()
    children, stopped = iter([service, gui]), []
    def ready(url, children, **kwargs):
        if len(children) == 2:
            raise RuntimeError('synthetic frontend failed')
    monkeypatch.setattr(startup, 'create_run_directory', lambda root: run)
    monkeypatch.setattr(startup, 'startup_lock', lambda path: nullcontext())
    monkeypatch.setattr(startup, 'restart_application', lambda *args, **kwargs: None)
    monkeypatch.setattr(startup, 'run_logged_command', lambda *args, **kwargs: None)
    monkeypatch.setattr(startup, 'wait_ready', ready)
    monkeypatch.setattr(startup.subprocess, 'Popen', lambda *args, **kwargs: next(children))
    monkeypatch.setattr(startup, 'stop_process', stopped.append)
    monkeypatch.setattr(sys, 'argv', ['start.py'])
    assert startup.main() == 1
    assert stopped == [gui, service]
    saved = json.loads((run / 'state.json').read_text(encoding='utf-8'))
    assert saved['stateHistory'][-3:] == ['gui_starting', 'cleaning', 'failed']


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell 5.1 entry')
def test_entry_parses_in_windows_powershell():
    entry = APP / 'run.ps1'
    assert entry.read_bytes().startswith(bytes([239, 187, 191]))
    escaped = str(entry).replace("'", "''")
    script = ("$tokens=$null; $errors=$null; "
              f"[System.Management.Automation.Language.Parser]::ParseFile('{escaped}',[ref]$tokens,[ref]$errors) | Out-Null; "
              "if ($errors.Count) { exit 1 }; Write-Output 'ParseOK'")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                             base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                            capture_output=True, timeout=10)
    assert result.returncode == 0 and b'ParseOK' in result.stdout


@pytest.mark.skipif(os.name != 'nt', reason='Windows process command lines')
def test_old_launch_cannot_stop_newer_queued_launch():
    old = {**process(10, SCRIPTS[0]), 'CreationDate': '/Date(100)/'}
    current = {**process(20, SCRIPTS[0]), 'CreationDate': '/Date(200)/'}
    queued = {**process(30, SCRIPTS[0]), 'CreationDate': '/Date(300)/'}
    queued_child = {**process(31, SCRIPTS[1], 30), 'CreationDate': '/Date(301)/'}
    snapshot = {'processes': [old, current, queued, queued_child], 'listeners': []}
    assert [p['ProcessId'] for p in processes.restart_plan(snapshot, SCRIPTS, 20, (SCRIPTS[0],))] == [10]
    # When that queued launch takes its turn, the prior launch's service may
    # have been created after it. Its parent entry still proves it is old.
    old_child = {**process(21, SCRIPTS[1], 20), 'CreationDate': '/Date(400)/'}
    snapshot['processes'] = [current, old_child, queued]
    snapshot['listeners'] = [{'LocalPort': 5178, 'OwningProcess': 21}]
    assert [p['ProcessId'] for p in processes.restart_plan(snapshot, SCRIPTS, 30, (SCRIPTS[0],))] == [20]


def test_argument_errors_are_logged(monkeypatch):
    spec = importlib.util.spec_from_file_location('learning_argument_test', APP / 'scripts/start.py')
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    run = create_run_directory(ROOT / 'logs/交互式学习/runs')
    monkeypatch.setattr(startup, 'create_run_directory', lambda root: run)
    monkeypatch.setattr(sys, 'argv', ['start.py', '--unknown-launch-flag'])
    assert startup.main() == 1
    assert '--unknown-launch-flag' in (run / 'startup.log').read_text(encoding='utf-8')
    assert json.loads((run / 'state.json').read_text(encoding='utf-8'))['state'] == 'failed'


def test_health_status_alone_is_not_ready(monkeypatch):
    spec = importlib.util.spec_from_file_location('learning_readiness_test', APP / 'scripts/start.py')
    startup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(startup)
    class Response:
        status = 200
        def read(self, size):
            return b'{"status":"ok","protocolVersion":999}'
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    class Child:
        def poll(self):
            return None
    monkeypatch.setattr(startup, 'urlopen', lambda *args, **kwargs: Response())
    monkeypatch.setattr(startup.time, 'sleep', lambda seconds: None)
    with pytest.raises(RuntimeError, match='就绪检查超时'):
        startup.wait_ready('http://127.0.0.1/health', (Child(),), timeout=.01,
                           expected_json={'status': 'ok', 'protocolVersion': 3})


@pytest.mark.skipif(not (APP / 'node_modules/typescript').is_dir(), reason='Run npm ci for frontend checks')
def test_graph_paths_and_rendering():
    """Check actual TS logic against root-to-target path enumeration and React output."""
    import shutil
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for frontend checks')
    script = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const localRequire = require('node:module').createRequire(process.argv[2]);
const ts = localRequire('typescript');
const React = localRequire('react');
const { renderToStaticMarkup } = localRequire('react-dom/server');
const source = fs.readFileSync(process.argv[2], 'utf8').replace('function Graph(', 'export function Graph(').replace('function ProjectDetail(', 'export function ProjectDetail(');
const js = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX
}}).outputText;
const context = { exports: {}, require: name => name === './api' ? {} : localRequire(name) };
vm.runInNewContext(js, context);
const { Graph, upstreamEdges } = context.exports;
const ids = ['A', 'B', 'C', 'D', 'E'];
const nodes = ids.map(id => ({ id }));
const possible = [];
for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++) {
  possible.push({ predecessor_id: ids[i], successor_id: ids[j] });
}
// Independently enumerate all complete paths on every DAG with this topological order.
for (let mask = 0; mask < (1 << possible.length); mask++) {
  const edges = possible.filter((_, index) => mask & (1 << index));
  const roots = ids.filter(id => !edges.some(e => e.successor_id === id));
  for (const target of ids) {
    const expected = new Set();
    const visit = (id, path) => {
      if (id === target) { path.forEach(index => expected.add(index)); return; }
      edges.forEach((e, index) => {
        if (e.predecessor_id === id) visit(e.successor_id, [...path, index]);
      });
    };
    roots.forEach(root => visit(root, []));
    assert.deepEqual([...upstreamEdges(nodes, edges, target)].sort(), [...expected].sort());
  }
}
const data = ['A','B','C','D','E','F','G','isolated','unknown'].map((id, i) => ({
  id, title: id, number: String(i), group: 'topic', track: id === 'D' ? 'branch' : id === 'unknown' ? undefined : 'main',
  status: id === 'C' ? 'awaiting_answer' : 'pending', skip: false,
  teaches: id === 'E' ? ['E'] : [], prerequisites: id === 'E' ? ['A'] : []
}));
const edges = [['A','C'],['B','C'],['A','D'],['C','E'],['D','E'],['A','F'],['E','G'],['missing','E']]
  .map(([predecessor_id, successor_id]) => ({ predecessor_id, successor_id }));
const project = { id: 'fixture', lessons: data, units: data, lessonEdges: edges, unitEdges: edges, pending: {}, canEdit: true };
const config = { graph: { min_zoom:25, max_zoom:400, prerequisite_color:'#8B5CF6', teaching_color:'#0891B2' } };
const render = (kind, selection, snapshot = project) => renderToStaticMarkup(React.createElement(Graph, {
  kind, project: snapshot, selection, select: () => {}, config, onAction: async () => {}
}));
for (const kind of ['lesson','unit']) {
  const selected = render(kind, { kind, id:'E' });
  assert.equal((selected.match(/class="upstream-edge-flow"/g) || []).length, 5);
  assert.equal((selected.match(/class="track-badge branch"/g) || []).length, 1);
  assert(!selected.includes('track-badge main'));
  assert(selected.includes('unknown 未分类'));
  assert(selected.includes('stroke="#C59B23" stroke-width="3"'));
  assert(selected.includes('aria-pressed="true"'));
  for (const id of ['A','isolated','missing']) {
    assert(!render(kind, { kind, id }).includes('upstream-edge-flow'));
  }
  assert(!render(kind, null).includes('upstream-edge-flow'));
  const cross = render(kind, { kind: kind === 'lesson' ? 'unit' : 'lesson', id:'E' });
  assert(!cross.includes('upstream-edge-flow'));
  assert(cross.includes('stroke="#0891B2"'));
  const changed = edges.filter((_, index) => index !== 4);
  const updated = render(kind, { kind, id:'E' }, { ...project, lessonEdges:changed, unitEdges:changed });
  assert.equal((updated.match(/class="upstream-edge-flow"/g) || []).length, 3);
}
// Exercise the App's actual separator event handlers without a browser or API writes.
const states = ['graph', [], project, null, config, '', 50, 'idle'];
const refs = []; let stateCursor = 0, refCursor = 0;
const hookReact = { ...React,
  useState: initial => {
    const index = stateCursor++;
    if (!(index in states)) states[index] = initial;
    return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
  },
  useRef: initial => refs[refCursor++] ||= { current: initial },
  useEffect: () => {}, useCallback: callback => callback
};
const apiMock = {};
const appContext = { exports: {}, require: name => name === 'react' ? hookReact : name === './api' ? apiMock : localRequire(name) };
vm.runInNewContext(js, appContext);
const find = (element, predicate) => {
  if (!element || typeof element !== 'object') return null;
  if (predicate(element)) return element;
  for (const child of [element.props?.children].flat(Infinity)) {
    const result = find(child, predicate); if (result) return result;
  }
  return null;
};
const separator = () => {
  stateCursor = refCursor = 0;
  const tree = appContext.exports.default();
  const container = find(tree, element => element.props?.className?.startsWith('dual-graphs'));
  container.props.ref.current = { getBoundingClientRect: () => ({ left:100, width:1000 }) };
  return find(tree, element => element.props?.role === 'separator').props;
};
const capture = new Set();
const target = { getBoundingClientRect: () => ({ left:595 }),
  setPointerCapture: id => capture.add(id), releasePointerCapture: id => capture.delete(id) };
const event = { button:0, pointerId:7, clientX:598, currentTarget:target, preventDefault:() => {} };
separator().onPointerDown(event);
assert.equal(states[7], 'dragging'); assert(capture.has(7));
separator().onPointerMove(event); assert.equal(states[6], 50); // no jump at grab position
separator().onPointerMove({ ...event, clientX:697 }); assert.equal(states[6], 60);
separator().onPointerMove({ ...event, clientX:-1000 }); assert.equal(states[6], 25);
separator().onPointerMove({ ...event, clientX:2000 }); assert.equal(states[6], 75);
separator().onPointerUp(event); assert.equal(states[7], 'idle'); assert(!capture.has(7));
separator().onDoubleClick(); assert.equal(states[6], 50);
separator().onKeyDown({ key:'ArrowLeft', preventDefault:() => {} }); assert.equal(states[6], 48);
separator().onKeyDown({ key:'ArrowRight', preventDefault:() => {} }); assert.equal(states[6], 50);
for (const end of ['onPointerCancel','onLostPointerCapture']) {
  separator().onPointerDown(event); separator()[end](); assert.equal(states[7], 'idle');
  separator().onPointerMove({ ...event, clientX:2000 }); assert.equal(states[6], 50);
}

// Render the actual detail component and exercise card actions against a public API stub.
const counts = { total:5, completed:2, learning:1, pending:1, skipped:1, completionRate:.4 };
const detailProject = { ...project, id:'20261004T010203', title:'合成详情项目', status:'in_progress', state:'awaiting_answer', revision:'TEST-revision',
  details:{ kind:'tutor', directory:'outputs/synthetic-project', createdAt:'2026-10-04T01:02:03', lastLearningAt:null,
    currentLesson:{ id:'A', number:'1.1', title:'合成课程' }, materials:[{ id:'S',title:'合成材料' }], coverage:[],
    statistics:{ units:counts, lessons:counts, main:counts, branch:counts, mastery:null } } };
const detailHtml = renderToStaticMarkup(React.createElement(context.exports.ProjectDetail, {
  project:detailProject, onClose:()=>{}, onGraph:()=>{}, onDelete:()=>{}, onRename:async()=>{}, saving:false
}));
for (const label of ['项目名称','基础信息','最后一次学习时间','尚未学习','当前进度','1.1 合成课程','知识点与前置关系','课程与前置关系','材料覆盖','40%']) assert(detailHtml.includes(label));
assert(detailHtml.includes('填写课程作答区'));
const navDetailHtml = renderToStaticMarkup(React.createElement(context.exports.ProjectDetail, {
  project:{ ...detailProject, state:'navigation_ready', lessons:[], lessonEdges:[], details:{ ...detailProject.details, kind:'navigation', currentLesson:null,
    coverage:[{ point_id:'P-1', summary:'合成材料要点', track:'main', unit_ids:['A'], lessons:[] }] } },
  onClose:()=>{}, onGraph:()=>{}, onDelete:()=>{}, onRename:async()=>{}, saving:false
}));
for (const label of ['合成材料要点','尚未规划课程','建立学习项目']) assert(navDetailHtml.includes(label));
apiMock.readProject = async id => { assert.equal(id,detailProject.id); return detailProject; };
(async () => {
  states[0] = 'projects'; states[1] = [{ id:detailProject.id,title:detailProject.title,status:'in_progress',lessonCount:5,unitCount:5,createdAt:detailProject.details.createdAt,lastLearningAt:null }]; states[8] = false;
  const tree = () => { stateCursor = refCursor = 0; return appContext.exports.default(); };
  const card = find(tree(), e => e.type === 'article' && e.props.className.startsWith('project-card'));
  assert(card);
  const body = find(card,e => e.props?.className === 'card-body');
  assert.equal(body.type,'button');
  const actions = find(card,e => e.props?.className === 'card-actions');
  assert.equal(actions.props.children.map(e => e.props.children).join('|'),'详情|删除|打开项目目录|跳转到图谱页');
  let stopped = false; body.props.onClick({ stopPropagation:()=>{stopped=true;} });
  await new Promise(resolve => setImmediate(resolve));
  assert(stopped); assert.equal(states[0],'projects'); assert.equal(states[8],true);
  const withDetails = tree(); assert(find(withDetails,e => e.type?.name === 'ProjectDetail'));
  const graphButton = find(withDetails,e => e.props?.className === 'card-actions').props.children[3];
  graphButton.props.onClick(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(states[0],'graph'); assert.equal(states[8],false);
  console.log('5120 DAG comparisons, graph controls, card actions and project details passed');
})().catch(error => { console.error(error); process.exitCode = 1; });

'''
    result = subprocess.run([node, '-', str(APP / 'src/App.tsx')], input=script,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
