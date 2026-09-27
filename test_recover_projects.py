"""全部使用虚构项目和临时数据，绝不访问真实 .codex。"""

import io
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import recover_projects as recovery


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'codex-data'
        self.home.mkdir()
        self.project = self.root / 'demo-project'
        self.project.mkdir()
        self.state_path = self.home / '.codex-global-state.json'
        self.state = {'local-projects': {}, 'projectless-thread-ids': ['chat-demo', 'chat-internal'],
                      'unrelated-setting': {'theme': 'demo'}}
        self.state_path.write_bytes(recovery.encoded(self.state))
        self.database = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript('''
                CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,created_at_ms INTEGER,
                    updated_at_ms INTEGER,position INTEGER);
                CREATE TABLE project_roots(project_id TEXT,path TEXT,position INTEGER);
                CREATE TABLE threads(id TEXT PRIMARY KEY,cwd TEXT,project_id TEXT,source TEXT,
                    archived INTEGER,title TEXT,updated_at INTEGER);
            ''')
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)', ('project-demo', '示例项目', 1, 2, 0))
            db.execute('INSERT INTO project_roots VALUES(?,?,?)', ('project-demo', str(self.project), 0))
            db.executemany('INSERT INTO threads VALUES(?,?,?,?,?,?,?)', [
                ('chat-demo', str(self.project), None, 'app', 0, '虚构聊天', 100),
                ('chat-archived', str(self.project), None, 'vscode', 1, '虚构归档', 101),
                ('chat-internal', str(self.project), None, '{"subagent":{}}', 0, '虚构内部', 102),
                ('chat-outside', str(self.root / 'outside'), None, 'cli', 0, '虚构其他', 103),
            ])
            db.commit()
        self.work = self.root / 'recovery'

    def rows(self):
        with closing(sqlite3.connect(self.database)) as db:
            return db.execute('SELECT * FROM threads ORDER BY id').fetchall()

    def apply(self, plan):
        with patch.object(recovery, 'require_stopped'), redirect_stdout(io.StringIO()):
            return recovery.apply(plan, self.work / 'backups')

    def undo(self):
        with patch.object(recovery, 'require_stopped'), redirect_stdout(io.StringIO()):
            recovery.rollback(self.work / 'backups', self.home)

    def test_prepare_is_read_only_and_skips_internal(self):
        before = self.state_path.read_bytes(), self.rows()
        plan = recovery.prepare(self.home)
        self.assertEqual({a['threadId'] for a in plan['assignments']}, {'chat-demo', 'chat-archived'})
        self.assertEqual(plan['skipped']['internal_or_unknown_source'], 1)
        self.assertEqual((self.state_path.read_bytes(), self.rows()), before)

    def test_snapshot_exact_assignment_and_intentional_projectless(self):
        snapshot = self.root / 'old-state.json'
        snapshot.write_bytes(recovery.encoded({
            'local-projects': {'legacy-demo': {'id': 'legacy-demo', 'name': '旧名称',
                                              'rootPaths': [str(self.project)]}},
            'thread-project-assignments': {'chat-demo': {'projectKind': 'local', 'projectId': 'legacy-demo'}},
            'projectless-thread-ids': ['chat-archived'],
        }))
        plan = recovery.prepare(self.home, snapshot)
        self.assertEqual(len(plan['assignments']), 1)
        self.assertEqual(plan['assignments'][0]['reason'], 'historical_assignment')
        self.assertEqual(plan['projects'][0]['legacyId'], 'legacy-demo')

    def test_apply_preserves_other_fields_idempotent_and_undo(self):
        plan = recovery.prepare(self.home)
        before = self.rows()
        directory = self.apply(plan)
        self.assertTrue((directory / 'state-before.sqlite').is_file())
        after = self.rows()
        for old, new in zip(before, after):
            self.assertEqual(old[:2] + old[3:], new[:2] + new[3:])
        state = recovery.load_json(self.state_path)
        self.assertEqual(state['projectless-thread-ids'], ['chat-internal'])
        self.assertEqual(state['unrelated-setting'], self.state['unrelated-setting'])
        self.assertIsNone(self.apply(plan))
        self.undo()
        self.assertEqual(self.rows(), before)
        self.assertEqual(recovery.load_json(self.state_path), self.state)

    def test_scope_requires_unambiguous_project(self):
        plan = recovery.prepare(self.home)
        self.assertEqual(recovery.select_scope(plan, '示例项目')['projects'], plan['projects'])
        with self.assertRaises(recovery.RecoveryError):
            recovery.select_scope(plan, '不存在')

    def test_plan_digest_and_home_binding(self):
        recovery.save_plan(recovery.prepare(self.home), self.work)
        self.assertEqual(recovery.read_plan(self.work, self.home)['schemaVersion'], 1)
        with self.assertRaises(recovery.RecoveryError):
            recovery.read_plan(self.work, self.root / 'other-home')
        with (self.work / 'recovery-plan.json').open('ab') as handle:
            handle.write(b' ')
        with self.assertRaises(recovery.RecoveryError):
            recovery.read_plan(self.work, self.home)

    def test_database_drift_stops_before_writes(self):
        plan = recovery.prepare(self.home)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("UPDATE threads SET project_id='other-project' WHERE id='chat-demo'")
            db.commit()
        before = self.rows(), self.state_path.read_bytes()
        with self.assertRaises(recovery.RecoveryError):
            self.apply(plan)
        self.assertEqual((self.rows(), self.state_path.read_bytes()), before)

    def test_undo_refuses_later_changes(self):
        self.apply(recovery.prepare(self.home))
        state = recovery.load_json(self.state_path)
        state['project-order'].append('new-project')
        self.state_path.write_bytes(recovery.encoded(state))
        before = self.rows(), self.state_path.read_bytes()
        with self.assertRaises(recovery.RecoveryError):
            self.undo()
        self.assertEqual((self.rows(), self.state_path.read_bytes()), before)

    def test_failure_after_json_write_rolls_back(self):
        plan = recovery.prepare(self.home)
        before = self.rows(), self.state_path.read_bytes()
        # 模拟 JSON 已替换后数据库提交失败，验证两处改动都回退。
        with closing(sqlite3.connect(self.database)) as db:
            class FailCommit:
                def execute(self, *args):
                    return db.execute(*args)

                def commit(self):
                    raise sqlite3.OperationalError('模拟提交失败')

                def rollback(self):
                    db.rollback()

            with patch.object(recovery, 'require_stopped'):
                with self.assertRaises(sqlite3.OperationalError):
                    recovery.commit_changes(FailCommit(), self.state_path, before[1],
                        recovery.patch_state(self.state, plan),
                        [{'id': 'chat-demo', 'before': None, 'after': 'project-demo'}])
        self.assertEqual((self.rows(), self.state_path.read_bytes()), before)

    def test_process_running_blocks_apply(self):
        before = self.rows(), self.state_path.read_bytes()
        with patch.object(recovery, 'require_stopped', side_effect=recovery.RecoveryError('运行中')):
            with self.assertRaises(recovery.RecoveryError):
                recovery.apply(recovery.prepare(self.home), self.work / 'backups')
        self.assertEqual((self.rows(), self.state_path.read_bytes()), before)

    def test_unknown_schema_and_duplicate_roots_rejected(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)', ('project-duplicate', '重复示例', 1, 2, 1))
            db.execute('INSERT INTO project_roots VALUES(?,?,?)', ('project-duplicate', str(self.project), 0))
            db.commit()
        with self.assertRaises(recovery.RecoveryError):
            recovery.prepare(self.home)

    def test_unknown_schema_rejected(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('DROP TABLE project_roots')
            db.commit()
        with self.assertRaises(recovery.RecoveryError):
            recovery.prepare(self.home)

    def test_single_project_then_all_can_be_undone_separately(self):
        second = self.root / 'second-demo'
        second.mkdir()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute('INSERT INTO projects VALUES(?,?,?,?,?)', ('project-second', '第二示例', 1, 2, 1))
            db.execute('INSERT INTO project_roots VALUES(?,?,?)', ('project-second', str(second), 0))
            db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?)',
                       ('chat-second', str(second), None, 'app', 0, '虚构第二聊天', 104))
            db.commit()
        plan = recovery.prepare(self.home)
        before = self.rows(), recovery.load_json(self.state_path)
        self.apply(recovery.select_scope(plan, '示例项目'))
        pilot = self.rows(), recovery.load_json(self.state_path)
        self.apply(plan)
        self.undo()
        self.assertEqual((self.rows(), recovery.load_json(self.state_path)), pilot)
        self.undo()
        self.assertEqual((self.rows(), recovery.load_json(self.state_path)), before)

    def test_missing_source_folder_stops_without_changes(self):
        plan = recovery.prepare(self.home)
        self.project.rmdir()
        before = self.rows(), self.state_path.read_bytes()
        with self.assertRaises(recovery.RecoveryError):
            self.apply(plan)
        self.assertEqual((self.rows(), self.state_path.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
