"""Windows Codex 项目和聊天归属恢复工具（社区方案，非官方）。

Community recovery reference: https://github.com/openai/codex/issues/36663
This is a local recovery attempt, not an official Codex repair utility.
"""

import argparse
import copy
import csv
import hashlib
import io
import json
import ntpath
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parent
LOCAL = HERE / '.local-recovery'


class RecoveryError(Exception):
    pass


def norm(path):
    path = path.replace('/', '\\')
    if path.startswith('\\\\?\\UNC\\'):
        path = '\\\\' + path[8:]
    elif path.startswith('\\\\?\\'):
        path = path[4:]
    return ntpath.normcase(ntpath.normpath(path))


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def atomic_write(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.project-recovery-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_stopped():
    if os.name != 'nt':
        raise RecoveryError('本工具的写入和撤销功能仅支持 Windows。')
    result = subprocess.run(
        ['tasklist.exe', '/FO', 'CSV', '/NH'], capture_output=True,
        text=True, errors='replace', check=True, creationflags=0x08000000,
    )
    names = {'chatgpt.exe', 'codex.exe', 'codex-cli.exe'}
    running = [(row[0], row[1]) for row in csv.reader(io.StringIO(result.stdout))
               if len(row) >= 2 and row[0].lower() in names]
    if running:
        detail = ', '.join(f'{name} (PID {pid})' for name, pid in running)
        raise RecoveryError(
            'Codex/ChatGPT 仍在运行，尚未修改任何配置。\n'
            '请先结束其他任务，并通过托盘的退出菜单完全退出应用；\n'
            '只关闭窗口可能仍有后台进程。退出后重新运行本脚本。\n' + detail
        )


def select_scope(plan, project=None):
    selected = copy.deepcopy(plan)
    if project is not None:
        matches = [p for p in plan['projects'] if project in
                   (p['canonicalId'], p['legacyId'], p['registry']['name'])]
        if len(matches) != 1:
            raise RecoveryError('项目名称不存在或不唯一；请使用预览中的项目 ID。')
        selected['projects'] = matches
    ids = {p['canonicalId'] for p in selected['projects']}
    selected['assignments'] = [a for a in plan['assignments'] if a['canonicalId'] in ids]
    if not selected['projects']:
        raise RecoveryError('恢复范围为空，已停止。')
    return selected


def patch_state(original, plan):
    state = copy.deepcopy(original)
    for key, empty in [('local-projects', {}), ('thread-project-assignments', {}), ('project-order', []), ('app-server-project-id-by-legacy-project-id-by-host', {})]:
        if state.get(key) is None:
            state[key] = empty
    registry = state.setdefault('local-projects', {})
    assignments = state.setdefault('thread-project-assignments', {})
    order = state.setdefault('project-order', [])
    mappings = state.setdefault('app-server-project-id-by-legacy-project-id-by-host', {}).setdefault(plan['hostKey'], {})
    if not all(isinstance(v, dict) for v in [registry, assignments, mappings]) or not isinstance(order, list):
        raise RecoveryError('项目配置格式已变化，请先重新检查。')
    for project in plan['projects']:
        legacy_id = project['legacyId']
        existing = registry.get(legacy_id)
        if existing is not None and {norm(p) for p in existing.get('rootPaths', [])} != {norm(p) for p in project['registry']['rootPaths']}:
            raise RecoveryError(f'项目目录与计划冲突：{project["registry"]["name"]}')
        registry.setdefault(legacy_id, project['registry'])
        previous = mappings.get(legacy_id)
        if previous not in (None, project['canonicalId']):
            raise RecoveryError(f'项目 ID 对应关系发生变化：{legacy_id}')
        mappings[legacy_id] = project['canonicalId']
        if legacy_id not in order:
            order.append(legacy_id)
    for assignment in plan['assignments']:
        previous = assignments.get(assignment['threadId'])
        if previous is not None and (previous.get('projectKind') != 'local' or previous.get('projectId') != assignment['legacyId']):
            raise RecoveryError(f'任务已被分配到其他项目：{assignment["threadId"]}')
        value = dict(previous or {})
        value.update(projectKind='local', projectId=assignment['legacyId'], cwd=assignment['cwd'], pendingCoreUpdate=False)
        assignments[assignment['threadId']] = value
    restored = {a['threadId'] for a in plan['assignments']}
    state['projectless-thread-ids'] = [t for t in (state.get('projectless-thread-ids') or []) if t not in restored]
    migration = (state.get('app-server-projects-migration-by-host') or {}).get(plan['hostKey'])
    if isinstance(migration, dict) and isinstance(migration.get('pendingThreadAssignmentIds'), list):
        migration['pendingThreadAssignmentIds'] = [t for t in migration['pendingThreadAssignmentIds'] if t not in restored]
    return state


def validate_db(db, plan):
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise RecoveryError('数据库检查未通过，已停止。')
    changed = []
    for project in plan['projects']:
        roots = [r[0] for r in db.execute('SELECT path FROM project_roots WHERE project_id=?', (project['canonicalId'],))]
        if {norm(p) for p in roots} != {norm(p) for p in project['registry']['rootPaths']}:
            raise RecoveryError(f'数据库项目目录发生变化：{project["registry"]["name"]}')
        if not all(Path(p).is_dir() for p in roots):
            raise RecoveryError(f'项目目录当前不可用：{project["registry"]["name"]}')
    for assignment in plan['assignments']:
        row = db.execute('SELECT cwd,project_id,source FROM threads WHERE id=?', (assignment['threadId'],)).fetchone()
        if row is None or norm(row[0]) != norm(assignment['cwd']) or row[2] != assignment['source']:
            raise RecoveryError(f'任务已变化或不存在：{assignment["threadId"]}')
        if row[1] not in (None, assignment['canonicalId']):
            raise RecoveryError(f'任务已有不同项目归属：{assignment["threadId"]}')
        if row[1] != assignment['canonicalId']:
            changed.append({'id': assignment['threadId'], 'before': row[1], 'after': assignment['canonicalId']})
    return changed


def new_backup(db, state_path, root):
    root.mkdir(parents=True, exist_ok=True)
    directory = root / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    directory.mkdir()
    (directory / 'global-state-before.json').write_bytes(state_path.read_bytes())
    with closing(sqlite3.connect(directory / 'state-before.sqlite')) as destination:
        db.backup(destination)
        if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RecoveryError('数据库备份校验失败，已停止。')
    return directory


def commit_changes(db, state_path, original_bytes, state, rows):
    replaced = False
    db.execute('BEGIN IMMEDIATE')
    try:
        for row in rows:
            result = db.execute('UPDATE threads SET project_id=? WHERE id=? AND project_id IS ?', (row['after'], row['id'], row['before']))
            if result.rowcount != 1:
                raise RecoveryError('数据库在恢复期间发生变化，已停止。')
        require_stopped()
        if state_path.read_bytes() != original_bytes:
            raise RecoveryError('界面状态在恢复期间发生变化，已停止。')
        atomic_write(state_path, encoded(state))
        replaced = True
        db.commit()
    except BaseException:
        db.rollback()
        if replaced:
            atomic_write(state_path, original_bytes)
        raise


def apply(plan, backup_root):
    require_stopped()
    state_path = Path(plan['statePath'])
    original_bytes = state_path.read_bytes()
    original = json.loads(original_bytes.decode('utf-8-sig'))
    check_state(original)
    state = patch_state(original, plan)
    with closing(sqlite3.connect(Path(plan['databasePath']).as_uri() + '?mode=rw', uri=True, timeout=10)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        rows = validate_db(db, plan)
        if state == original and not rows:
            print('这些项目和任务的恢复配置已经存在，无需重复写入。')
            return None
        directory = new_backup(db, state_path, Path(backup_root))
        keys = sorted(k for k in set(original) | set(state) if original.get(k) != state.get(k) or (k in original) != (k in state))
        receipt = {
            'status': 'prepared', 'statePath': str(state_path), 'databasePath': plan['databasePath'],
            'keys': {k: {'beforeExists': k in original, 'before': original.get(k), 'after': state.get(k)} for k in keys},
            'rows': rows, 'projectCount': len(plan['projects']), 'assignmentCount': len(plan['assignments']),
        }
        atomic_write(directory / 'receipt.json', encoded(receipt))
        atomic_write(directory / 'global-state-after.json', encoded(state))
        try:
            commit_changes(db, state_path, original_bytes, state, rows)
        except BaseException:
            receipt['status'] = 'rolled_back_after_error'
            atomic_write(directory / 'receipt.json', encoded(receipt))
            raise
        receipt['status'] = 'applied'
        atomic_write(directory / 'receipt.json', encoded(receipt))
    print(f'已写入 {len(plan["projects"])} 个项目、{len(plan["assignments"])} 条任务的恢复配置。')
    print(f'回滚备份：{directory}')
    print('现在请重新打开 Codex，检查项目列表和原聊天。界面是否恢复需重启后确认。')
    return directory


def rollback(backup_root, home=None):
    require_stopped()
    receipts = []
    for path in sorted(Path(backup_root).glob('*/receipt.json'), reverse=True):
        receipt = load_json(path)
        if receipt.get('status') in ('applied', 'prepared'):
            receipts.append((path, receipt))
    if not receipts:
        raise RecoveryError('没有可撤销的恢复记录。')
    path, receipt = receipts[0]
    if home is not None:
        home = Path(home).resolve()
        if (norm(receipt['statePath']) != norm(str(home / '.codex-global-state.json')) or
                norm(receipt['databasePath']) != norm(str(home / 'state_5.sqlite'))):
            raise RecoveryError('最近的备份属于另一个数据目录，已停止撤销。')
    state_path = Path(receipt['statePath'])
    original_bytes = state_path.read_bytes()
    state = json.loads(original_bytes.decode('utf-8-sig'))
    for key, change in receipt['keys'].items():
        already_before = (key in state) == change['beforeExists'] and state.get(key) == change['before']
        if already_before:
            continue
        if key not in state or state[key] != change['after']:
            raise RecoveryError('应用已继续改写项目配置，自动撤销已停止。请保留备份，人工核对差异；不要覆盖整个数据库。')
        if change['beforeExists']:
            state[key] = change['before']
        else:
            state.pop(key, None)
    with closing(sqlite3.connect(Path(receipt['databasePath']).as_uri() + '?mode=rw', uri=True, timeout=10)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        rows = []
        for change in receipt['rows']:
            current = db.execute('SELECT project_id FROM threads WHERE id=?', (change['id'],)).fetchone()
            if current is None or current[0] not in (change['before'], change['after']):
                raise RecoveryError('任务归属已发生新的变化，自动回滚已停止。')
            if current[0] != change['before']:
                rows.append({'id': change['id'], 'before': change['after'], 'after': change['before']})
        commit_changes(db, state_path, original_bytes, state, rows)
    receipt['status'] = 'undone'
    atomic_write(path, encoded(receipt))
    print('最近一次恢复已撤销；聊天内容和项目文件未做改动。')


def check_state(state):
    if not isinstance(state, dict):
        raise RecoveryError('状态文件不是 JSON 对象。')
    fields = {
        'local-projects': dict, 'thread-project-assignments': dict,
        'project-order': list, 'projectless-thread-ids': list,
        'app-server-project-id-by-legacy-project-id-by-host': dict,
        'app-server-projects-migration-by-host': dict,
    }
    for key, kind in fields.items():
        if state.get(key) is not None and not isinstance(state[key], kind):
            raise RecoveryError(f'状态字段格式不兼容：{key}')
    for value in (state.get('local-projects') or {}).values():
        if not isinstance(value, dict) or not isinstance(value.get('rootPaths'), list):
            raise RecoveryError('项目登记格式不兼容。')
    for value in (state.get('thread-project-assignments') or {}).values():
        if not isinstance(value, dict):
            raise RecoveryError('聊天归属格式不兼容。')
    for field in ('app-server-project-id-by-legacy-project-id-by-host', 'app-server-projects-migration-by-host'):
        if any(not isinstance(v, dict) for v in (state.get(field) or {}).values()):
            raise RecoveryError(f'主机配置格式不兼容：{field}')


def prepare(home, snapshot=None):
    """只读本机数据，生成可审阅的恢复计划；不读聊天正文。"""
    home = Path(home).resolve()
    state_path = home / '.codex-global-state.json'
    database = home / 'state_5.sqlite'
    current = load_json(state_path)
    old = load_json(snapshot) if snapshot else {}
    check_state(current)
    check_state(old)
    projects = []
    aliases = {}
    registries = [current.get('local-projects') or {}, old.get('local-projects') or {}]
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        required = {
            'projects': {'id', 'name', 'created_at_ms', 'updated_at_ms', 'position'},
            'project_roots': {'project_id', 'path', 'position'},
            'threads': {'id', 'cwd', 'project_id', 'source', 'archived'},
        }
        for table, columns in required.items():
            actual = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
            if not columns <= actual:
                raise RecoveryError(f'数据库结构不兼容：{table}。本工具只支持已验证的 state_5.sqlite 结构。')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RecoveryError('数据库检查未通过。')
        for row in db.execute('SELECT * FROM projects ORDER BY position,created_at_ms'):
            roots = [r[0] for r in db.execute(
                'SELECT path FROM project_roots WHERE project_id=? ORDER BY position', (row['id'],))]
            if not roots:
                raise RecoveryError(f'项目缺少根目录：{row["name"]}')
            root_set = {norm(p) for p in roots}
            if any(root_set == {norm(p) for p in q['registry']['rootPaths']} for q in projects):
                raise RecoveryError('多个数据库项目具有相同根目录，无法确定归属。请人工核对。')
            matches = []
            for registry in registries:
                found = [(key, value) for key, value in registry.items()
                         if {norm(p) for p in value['rootPaths']} == root_set]
                if len(found) > 1:
                    raise RecoveryError(f'状态文件中存在重复项目目录：{row["name"]}')
                matches.extend(found)
            legacy_id, registry = matches[0] if matches else (row['id'], {})
            registry = copy.deepcopy(registry)
            registry.update(id=legacy_id, name=row['name'], rootPaths=roots)
            registry.setdefault('createdAt', row['created_at_ms'])
            registry.setdefault('updatedAt', row['updated_at_ms'])
            projects.append({'canonicalId': row['id'], 'legacyId': legacy_id, 'registry': registry})
            for key in {row['id'], legacy_id} | {key for key, _ in matches}:
                if key in aliases and aliases[key] != row['id']:
                    raise RecoveryError('同一项目 ID 对应多个不同目录。')
                aliases[key] = row['id']
        by_id = {p['canonicalId']: p for p in projects}
        historical = dict(old.get('thread-project-assignments') or {})
        historical.update(current.get('thread-project-assignments') or {})
        old_projectless = set(old.get('projectless-thread-ids') or [])
        assignments = []
        skipped = Counter()
        for row in db.execute('SELECT id,cwd,project_id,source,archived FROM threads'):
            if row['source'] not in ('vscode', 'cli', 'app'):
                skipped['internal_or_unknown_source'] += 1
                continue
            explicit = historical.get(row['id'])
            target, reason = None, None
            if explicit:
                if explicit.get('projectKind') != 'local' or explicit.get('projectId') not in aliases:
                    skipped['unresolved_explicit_assignment'] += 1
                    continue
                target = aliases[explicit['projectId']]
                reason = 'historical_assignment'
            elif row['project_id'] in by_id:
                target, reason = row['project_id'], 'existing_database_assignment'
            elif row['project_id'] is not None:
                skipped['unknown_database_project'] += 1
                continue
            elif row['id'] in old_projectless:
                skipped['historically_projectless'] += 1
                continue
            else:
                matches = [p['canonicalId'] for p in projects
                           if norm(row['cwd']) in {norm(r) for r in p['registry']['rootPaths']}]
                if len(matches) == 1:
                    target, reason = matches[0], 'unique_exact_working_directory'
            if target is None:
                skipped['no_unique_project_match'] += 1
                continue
            if row['project_id'] not in (None, target):
                raise RecoveryError(f'历史归属与数据库冲突：{row["id"]}')
            project = by_id[target]
            # 使用已有的 UI 归属时保留它的旧 ID，避免把正常归属误判成冲突。
            legacy_id = explicit['projectId'] if explicit else project['legacyId']
            if legacy_id != project['legacyId']:
                raise RecoveryError('当前归属使用了不同的旧项目 ID，请人工核对快照和当前状态。')
            assignments.append({
                'threadId': row['id'], 'canonicalId': target, 'legacyId': legacy_id,
                'cwd': row['cwd'], 'source': row['source'], 'archived': bool(row['archived']), 'reason': reason,
            })
    plan = {
        'schemaVersion': 1, 'preparedAt': datetime.now(timezone.utc).isoformat(),
        'codexHome': str(home), 'statePath': str(state_path), 'databasePath': str(database),
        'hostKey': 'local:' + str(home), 'projects': projects, 'assignments': assignments,
        'skipped': dict(skipped), 'snapshotUsed': bool(snapshot),
    }
    # 在生成阶段发现冲突，避免将不可执行的计划交给用户。
    patch_state(current, plan)
    return plan


def save_plan(plan, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data = encoded(plan)
    atomic_write(directory / 'recovery-plan.json', data)
    atomic_write(directory / 'recovery-plan.sha256', (hashlib.sha256(data).hexdigest() + '\n').encode('ascii'))


def read_plan(directory, home):
    directory = Path(directory)
    data = (directory / 'recovery-plan.json').read_bytes()
    digest = (directory / 'recovery-plan.sha256').read_text(encoding='ascii').strip()
    if hashlib.sha256(data).hexdigest() != digest:
        raise RecoveryError('恢复计划校验失败，请重新生成计划。')
    plan = json.loads(data.decode('utf-8-sig'))
    home = Path(home).resolve()
    expected = {'codexHome': home, 'statePath': home / '.codex-global-state.json',
                'databasePath': home / 'state_5.sqlite'}
    if plan.get('schemaVersion') != 1 or any(norm(plan.get(k, '')) != norm(str(v)) for k, v in expected.items()):
        raise RecoveryError('计划与当前 Codex 数据目录不一致，请重新生成。')
    if plan.get('hostKey') != 'local:' + str(home):
        raise RecoveryError('计划中的主机标识不一致。')
    return plan


def preview(plan):
    print(f'预览：{len(plan["projects"])} 个项目，{len(plan["assignments"])} 条普通聊天（含归档）。')
    for project in plan['projects']:
        group = [a for a in plan['assignments'] if a['canonicalId'] == project['canonicalId']]
        print(f'  {project["registry"]["name"]}: {len(group)} 条；ID={project["canonicalId"]}')
        for root in project['registry']['rootPaths']:
            print(f'    {root}')
    print('归属依据：', dict(Counter(a['reason'] for a in plan['assignments'])))
    print('跳过原因：', plan['skipped'])
    if not plan['snapshotUsed']:
        print('未提供旧快照：按唯一且完全相同的工作目录推断缺失归属。请核对是否有原本刻意无项目的聊天。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'preview', 'apply', 'undo'])
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')),
                        help='Codex 数据目录，默认 CODEX_HOME 或用户目录下 .codex')
    parser.add_argument('--work-dir', type=Path, default=LOCAL, help='本地计划和备份目录')
    parser.add_argument('--snapshot', type=Path, help='prepare 时可选的完整旧界面状态 JSON')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--project', help='试恢复一个项目：名称或预览中的 ID')
    scope.add_argument('--all', action='store_true', help='明确选择所有项目')
    args = parser.parse_args()
    if args.snapshot and args.action != 'prepare':
        parser.error('--snapshot 仅用于 prepare')
    if args.action == 'apply' and not (args.project or args.all):
        parser.error('apply 必须指定 --project 或 --all，建议先试一个项目')
    if args.action == 'prepare':
        plan = prepare(args.home, args.snapshot)
        save_plan(plan, args.work_dir)
        preview(plan)
        print(f'只生成了本地计划，没有修改 Codex。计划目录：{args.work_dir.resolve()}')
    elif args.action == 'undo':
        # 核对当前数据目录，避免撤销另一个用户/目录的备份。
        read_plan(args.work_dir, args.home)
        rollback(args.work_dir / 'backups', args.home)
    else:
        plan = select_scope(read_plan(args.work_dir, args.home), args.project)
        if args.action == 'preview':
            preview(plan)
        else:
            apply(plan, args.work_dir / 'backups')


if __name__ == '__main__':
    try:
        main()
    except (RecoveryError, OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f'\n未完成：{error}')
        raise SystemExit(1)
