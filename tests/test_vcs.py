"""Git versioning tests.

Every repository here is a temporary directory (plus a temporary bare
repository as the push remote). git never runs against the application root;
the user's global git config is replaced by a temporary file.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from notebook import vcs

APP_ROOT = Path(__file__).resolve().parents[1]
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
IDENTITY = ('测试者', 'tester@example.invalid')


def git(root, *args, data=None, check=True):
    """Raw git for test setup and inspection, always pointed at a temporary repository."""
    return subprocess.run(['git', '-C', str(root), *args], input=data, capture_output=True, check=check,
                          creationflags=NO_WINDOW)


def tracked(root, rev=None):
    args = ['ls-tree', '-r', '-z', '--name-only', rev] if rev else ['ls-files', '-z']
    return {name for name in git(root, *args).stdout.decode('utf-8').split('\0') if name}


def messages(root):
    out = git(root, 'log', '--format=%B%x1e').stdout.decode('utf-8')
    return [part.strip() for part in out.split('\x1e') if part.strip()]


def commit_count(root):
    return int(git(root, 'rev-list', '--count', 'HEAD').stdout)


class VcsCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vcs 测试 ', ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        if self.base == APP_ROOT or APP_ROOT in self.base.parents:
            self.fail('临时目录位于应用目录内，测试拒绝在应用自己的仓库里运行 git。')
        config = self.base / 'global.gitconfig'
        config.write_text(f'[user]\n\tname = {IDENTITY[0]}\n\temail = {IDENTITY[1]}\n', encoding='utf-8')
        environment = patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(config),
                                              'GIT_CEILING_DIRECTORIES': str(self.base.parent)})
        environment.start()
        self.addCleanup(environment.stop)

    def make_repo(self, name='研究 目录', init=True, settings=None, **options):
        root = self.base / name
        root.mkdir(parents=True, exist_ok=True)
        if init:
            git(root, 'init', '-q', '-b', 'main')
            git(root, 'config', 'user.name', IDENTITY[0])
            git(root, 'config', 'user.email', IDENTITY[1])
        settings = settings or {'auto_commit': True, 'auto_push': False, 'remote': 'origin', 'branch': 'main'}
        repo = vcs.Repo(root, settings_getter=lambda: settings, **options)
        self.addCleanup(repo.close)
        return root, repo

    def wait_for(self, predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail('等待超时')


class EnsureTests(VcsCase):
    def test_ensure_creates_repository_with_attributes_ignores_and_initial_commit(self):
        root, repo = self.make_repo(init=False)
        (root / '笔记.md').write_text('正文\n', encoding='utf-8')
        (root / '.gitignore').write_text('自定义缓存/\n*.pyc\n', encoding='utf-8')
        for name in ('.runtime/state.json', '.history/a.md.bak', '__pycache__/m.pyc', '数据.mat', '验收截图/x.tmp'):
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text('x', encoding='utf-8')
        status = repo.ensure()
        self.assertTrue(status['is_repo'])
        self.assertTrue(status['initialized'])
        self.assertEqual(status['branch'], 'main')
        self.assertFalse(status['dirty'])
        self.assertEqual(status['last_commit']['message'], vcs.INIT_MESSAGE)
        self.assertEqual(status['message'], '')
        self.assertEqual((root / '.gitattributes').read_bytes(), b'* -text\n')
        lines = (root / '.gitignore').read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines[0], '自定义缓存/')
        for line in vcs.REQUIRED_IGNORES:
            self.assertEqual(lines.count(line), 1, line)
        self.assertEqual(tracked(root), {'.gitattributes', '.gitignore', '笔记.md'})
        head, ignore = git(root, 'rev-parse', 'HEAD').stdout, (root / '.gitignore').read_bytes()
        again = repo.ensure()
        self.assertFalse(again['initialized'])
        self.assertEqual(git(root, 'rev-parse', 'HEAD').stdout, head)
        self.assertEqual((root / '.gitignore').read_bytes(), ignore)

    def test_ensure_keeps_other_attributes_and_moves_text_rule_last(self):
        root, repo = self.make_repo()
        (root / '.gitattributes').write_bytes(b'*.png binary\r\n* -text\r\n*.md text eol=crlf\r\n')
        repo.ensure()
        self.assertEqual((root / '.gitattributes').read_bytes(), b'*.png binary\r\n*.md text eol=crlf\r\n* -text\r\n')
        self.assertIn(b'text: unset', git(root, 'check-attr', 'text', '--', 'a.md').stdout)
        repo.ensure()
        self.assertEqual((root / '.gitattributes').read_bytes(), b'*.png binary\r\n*.md text eol=crlf\r\n* -text\r\n')

    def test_nested_root_is_left_to_the_outer_repository(self):
        outer = self.base / '应用'
        outer.mkdir()
        git(outer, 'init', '-q', '-b', 'main')
        child = outer / '示例研究'
        child.mkdir()
        (child / 'a.md').write_text('示例\n', encoding='utf-8')
        repo = vcs.Repo(child)
        self.addCleanup(repo.close)
        self.assertFalse(repo.is_repo())
        status = repo.ensure()
        self.assertFalse(status['is_repo'])
        self.assertIn('另一个 git 仓库', status['message'])
        self.assertFalse((child / '.git').exists())
        self.assertFalse((child / '.gitignore').exists())
        with self.assertRaises(vcs.GitError):
            repo.commit('不应提交到外层仓库')
        self.assertEqual(repo.history('a.md'), [])
        with self.assertRaises(FileNotFoundError):
            repo.show('1234567', 'a.md')
        self.assertNotEqual(git(outer, 'rev-parse', '--verify', '-q', 'HEAD', check=False).returncode, 0)
        self.assertEqual(git(outer, 'ls-files').stdout, b'')

    def test_missing_git_is_reported_without_raising(self):
        root, repo = self.make_repo(init=False)
        with patch.object(vcs, 'find_git', return_value=None):
            self.assertFalse(repo.available())
            status = repo.ensure()
            self.assertFalse(status['available'])
            self.assertFalse(status['is_repo'])
            self.assertIn('git', status['message'])
            self.assertFalse(repo.is_repo())
            with self.assertRaises(vcs.GitError):
                repo.commit('无 git')
            self.assertEqual(repo.history('a.md'), [])
            with self.assertRaises(FileNotFoundError):
                repo.show('1234567', 'a.md')
            self.assertFalse(repo.push()['ok'])
        self.assertFalse((root / '.git').exists())


class CommitTests(VcsCase):
    def test_crlf_bytes_survive_commit_checkout_and_clone(self):
        root, repo = self.make_repo()
        git(root, 'config', 'core.autocrlf', 'true')  # Git for Windows' usual default
        repo.ensure()
        files = {'crlf.md': b'line1\r\nline2\r\n', 'lf.md': b'line1\nline2\n', '混合 换行.txt': b'a\r\nb\nc\r'}
        for name, data in files.items():
            (root / name).write_bytes(data)
        result = repo.commit('第1轮：换行测试')
        self.assertEqual(result['files'], 3)
        for name, data in files.items():
            self.assertEqual(repo.show(result['sha'], name), data)
            self.assertEqual(git(root, 'cat-file', 'blob', f'HEAD:{name}').stdout, data)
            (root / name).unlink()
        git(root, 'checkout', '--', '.')  # a manual checkout that still has autocrlf=true
        for name, data in files.items():
            self.assertEqual((root / name).read_bytes(), data)
        self.assertFalse(repo.status()['dirty'])
        clone = self.base / '克隆 副本'
        subprocess.run(['git', '-c', 'core.autocrlf=true', 'clone', '-q', str(root), str(clone)],
                       capture_output=True, check=True, creationflags=NO_WINDOW)
        for name, data in files.items():
            self.assertEqual((clone / name).read_bytes(), data)

    def test_large_files_stay_out_of_git(self):
        root, repo = self.make_repo(large_file_bytes=1000)
        repo.ensure()
        big_name = '数据/大 文件[1].bin'
        big = root / big_name
        big.parent.mkdir()
        big.write_bytes(b'\x01' * 1500)
        (root / '数据/小.txt').write_bytes(b'small')
        result = repo.commit('加入数据')
        self.assertEqual(result['skipped_large'], [big_name])
        self.assertEqual(result['files'], 1)
        self.assertNotIn(big_name, tracked(root))
        self.assertIn('数据/小.txt', tracked(root))
        self.assertTrue(big.is_file())
        oid = git(root, '--literal-pathspecs', 'hash-object', '--', big_name).stdout.decode().strip()
        self.assertNotEqual(git(root, 'cat-file', '-e', oid, check=False).returncode, 0, '大文件内容不应进入对象库')
        status = repo.status()
        self.assertFalse(status['dirty'])
        self.assertEqual([item['path'] for item in status['skipped_large']], [big_name])
        # A tracked file that grows past the limit stops being tracked (git keeps no stale copy) and is reported.
        (root / '表.csv').write_bytes(b'1,2\n' * 100)
        repo.commit('加入表格')
        (root / '表.csv').write_bytes(b'1,2\n' * 500)
        grown = repo.commit('表格变大')
        self.assertEqual(grown['untracked_large'], ['表.csv'])
        self.assertNotIn('表.csv', tracked(root))
        self.assertTrue((root / '表.csv').is_file())
        self.assertIn('表.csv', repo.status()['untracked_large'])
        # A large file staged by hand is taken out again before the commit.
        git(root, '--literal-pathspecs', 'add', '--', big_name)
        (root / '说明.md').write_text('说明\n', encoding='utf-8')
        third = repo.commit('手动暂存的大文件')
        self.assertEqual(third['files'], 1)
        self.assertIn(big_name, third['skipped_large'])
        self.assertIn('表.csv', third['skipped_large'])
        self.assertNotIn(big_name, tracked(root, 'HEAD'))
        self.assertNotIn(big_name, tracked(root))
        self.assertEqual(vcs.LARGE_FILE_BYTES, 50 * 1024 * 1024)
        self.assertEqual(vcs.Repo(root).large_file_bytes, vcs.LARGE_FILE_BYTES)

    def test_nothing_to_commit_returns_none_and_message_keeps_chinese(self):
        root, repo = self.make_repo()
        repo.ensure()
        self.assertIsNone(repo.commit('没有变化'))
        (root / '推导笔记.md').write_text('推导\n', encoding='utf-8')
        message = 'C04-Q01 第3轮：推进（作者 GPT；审查 Gemini）'
        result = repo.commit(message)
        self.assertEqual(result['message'], message)
        self.assertEqual(len(result['sha']), 40)
        self.assertTrue(result['sha'].startswith(result['short']))
        raw = git(root, 'log', '-1', '--format=%B').stdout
        self.assertEqual(raw.decode('utf-8').strip(), message)
        self.assertEqual(repo.history('推导笔记.md')[0]['message'], message)
        self.assertEqual(repo.status()['last_commit']['message'], message)
        with self.assertRaises(ValueError):
            repo.commit('  ')

    def test_first_async_commit_without_ensure_adds_required_files(self):
        root, repo = self.make_repo(settings={'auto_commit': True, 'auto_push': True})
        (root / '.runtime').mkdir()
        (root / '.runtime/server.log').write_text('日志', encoding='utf-8')
        (root / '00_项目卡.md').write_text('项目\n', encoding='utf-8')
        repo.commit_async('F01 手动：新建框架')
        repo.close()
        self.assertEqual(messages(root), ['F01 手动：新建框架'])
        self.assertEqual(tracked(root), {'.gitattributes', '.gitignore', '00_项目卡.md'})
        record = json.loads((root / '.runtime/git_push.json').read_text(encoding='utf-8'))
        self.assertFalse(record['ok'])
        self.assertEqual(record['code'], 'no_remote')
        self.assertTrue(record['at'] and record['message'])

    def test_commit_pauses_on_detached_head_and_unfinished_merge(self):
        root, repo = self.make_repo()
        repo.ensure()
        head = git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
        git(root, 'checkout', '-q', '--detach')
        (root / 'a.md').write_text('a\n', encoding='utf-8')
        with self.assertRaisesRegex(vcs.GitError, '分离'):
            repo.commit('分离状态')
        status = repo.status()
        self.assertEqual(status['branch'], '')
        self.assertIn('分离', status['message'])
        git(root, 'checkout', '-q', 'main')
        (root / '.git/MERGE_HEAD').write_text(head + '\n', encoding='utf-8')
        repo.commit_async('合并中')
        repo.close()
        self.assertEqual(commit_count(root), 1)
        self.assertIn('合并', repo.status()['last_error'])
        (root / '.git/MERGE_HEAD').unlink()
        self.assertEqual(repo.commit('恢复正常')['files'], 1)
        self.assertEqual(repo.status()['last_error'], '')

    @unittest.skipUnless(os.name == 'nt', 'Windows byte-range lock')
    def test_file_locked_by_another_program_waits_for_the_next_commit(self):
        import msvcrt
        root, repo = self.make_repo()
        repo.ensure()
        (root / 'a.md').write_text('a\n', encoding='utf-8')
        locked = root / '占用.docx'
        locked.write_bytes(b'0123456789')
        with open(locked, 'r+b') as handle:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                result = repo.commit('部分提交')
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        self.assertEqual(result['files'], 1)
        self.assertTrue(any('占用.docx' in warning for warning in result['warnings']))
        self.assertNotIn('占用.docx', tracked(root))
        self.assertEqual(repo.commit('补交')['files'], 1)
        self.assertIn('占用.docx', tracked(root))


class AsyncTests(VcsCase):
    def test_commit_async_merges_messages_inside_the_window(self):
        root, repo = self.make_repo(coalesce_seconds=0.5)
        repo.ensure()
        started = time.monotonic()
        (root / 'a.md').write_text('a\n', encoding='utf-8')
        repo.commit_async('C04 手动：保存正文')
        (root / 'b.md').write_text('b\n', encoding='utf-8')
        repo.commit_async('C04 手动：保存锚点')
        repo.commit_async('C04 手动：保存正文')
        self.assertTrue(repo.status()['pending'])
        self.wait_for(lambda: commit_count(root) == 2)
        self.assertGreaterEqual(time.monotonic() - started, 0.45)
        (root / 'c.md').write_text('c\n', encoding='utf-8')
        repo.commit_async('C04-Q01 第1轮：推进\n（作者 GPT）')
        repo.close()
        self.assertEqual(messages(root)[:2], ['C04-Q01 第1轮：推进 （作者 GPT）', 'C04 手动：保存正文；C04 手动：保存锚点'])
        self.assertFalse(repo.status()['pending'])

    def test_close_commits_queued_messages_without_waiting_for_the_window(self):
        root, repo = self.make_repo(coalesce_seconds=60)
        repo.ensure()
        (root / 'a.md').write_text('a\n', encoding='utf-8')
        repo.commit_async('保存正文')
        started = time.monotonic()
        repo.close(timeout=30)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(messages(root)[0], '保存正文')
        repo.close()

    def test_commit_async_respects_auto_commit_off(self):
        root, repo = self.make_repo(settings={'auto_commit': False, 'auto_push': False})
        repo.ensure()
        (root / 'a.md').write_text('a\n', encoding='utf-8')
        repo.commit_async('不应提交')
        repo.close()
        self.assertEqual(commit_count(root), 1)
        self.assertTrue(repo.status()['dirty'])

    def test_push_async_runs_once_more_when_requested_during_a_push(self):
        root, repo = self.make_repo()
        started, release, calls = threading.Event(), threading.Event(), []

        def slow_push(timeout=120):
            calls.append(timeout)
            started.set()
            release.wait(10)
            return {'ok': True}

        with patch.object(repo, 'push', side_effect=slow_push):
            repo.push_async()
            self.assertTrue(started.wait(5))
            self.assertTrue(repo.status()['pushing'])
            for _ in range(3):
                repo.push_async()
            release.set()
            repo.close(timeout=10)
        self.assertEqual(len(calls), 2)
        self.assertFalse(repo.status()['pushing'])


class PushTests(VcsCase):
    def setUp(self):
        super().setUp()
        self.remote = self.base / '远程 仓库.git'
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(self.remote)], capture_output=True, check=True,
                       creationflags=NO_WINDOW)

    def remote_head(self):
        return git(self.remote, 'rev-parse', 'main').stdout.decode().strip()

    def test_push_to_local_bare_remote_and_status(self):
        root, repo = self.make_repo(settings={'auto_commit': True, 'auto_push': True, 'remote': 'origin', 'branch': 'main'})
        repo.ensure()
        status = repo.set_remote(str(self.remote))
        self.assertEqual(status['remote_url'], str(self.remote))
        self.assertEqual(status['ahead'], 1)
        result = repo.push()
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['code'], 'ok')
        self.assertEqual(self.remote_head(), git(root, 'rev-parse', 'HEAD').stdout.decode().strip())
        record = json.loads((root / '.runtime/git_push.json').read_text(encoding='utf-8'))
        self.assertTrue(record['ok'])
        self.assertTrue(record['at'] and record['message'])
        status = repo.status()
        self.assertEqual(status['ahead'], 0)
        self.assertTrue(status['last_push']['ok'])
        git(root, 'branch', '--unset-upstream')  # counted against the remote's branches instead
        self.assertEqual(repo.status()['ahead'], 0)
        (root / '新文件.md').write_text('新\n', encoding='utf-8')
        repo.commit_async('C01 手动：保存正文')
        repo.close()
        self.assertEqual(self.remote_head(), git(root, 'rev-parse', 'HEAD').stdout.decode().strip())
        self.assertEqual(repo.status()['ahead'], 0)

    def test_push_is_never_forced_when_the_remote_moved_on(self):
        root, repo = self.make_repo()
        repo.ensure()
        repo.set_remote(str(self.remote))
        self.assertTrue(repo.push()['ok'])
        other = self.base / '另一台电脑'
        subprocess.run(['git', 'clone', '-q', str(self.remote), str(other)], capture_output=True, check=True,
                       creationflags=NO_WINDOW)
        (other / '别处.md').write_text('别处\n', encoding='utf-8')
        git(other, 'add', '-A')
        git(other, 'commit', '-q', '-m', 'elsewhere')
        git(other, 'push', '-q', 'origin', 'main')
        theirs = self.remote_head()
        (root / '本机.md').write_text('本机\n', encoding='utf-8')
        repo.commit('本机修改')
        result = repo.push()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'rejected')
        self.assertIn('不会强制推送', result['message'])
        self.assertEqual(self.remote_head(), theirs)
        record = json.loads((root / '.runtime/git_push.json').read_text(encoding='utf-8'))
        self.assertFalse(record['ok'])
        self.assertFalse(repo.status()['last_push']['ok'])

    def test_push_without_remote_is_recorded(self):
        root, repo = self.make_repo()
        repo.ensure()
        result = repo.push()
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'no_remote')
        self.assertEqual(repo.status()['last_push']['code'], 'no_remote')

    def test_remote_credentials_are_rejected_and_never_shown(self):
        root, repo = self.make_repo()
        repo.ensure()
        for url in ('https://user:secret@example.invalid/r.git', 'https://ghp_token@example.invalid/r.git',
                    '-oProxyCommand=calc', 'ext::sh -c touch% x', 'https://example.invalid/a\nb', '', 42):
            with self.assertRaises(ValueError, msg=url):
                repo.set_remote(url)
        git(root, 'remote', 'add', 'origin', 'https://user:secret-token@example.invalid/r.git')
        status = repo.status()
        self.assertEqual(status['remote_url'], 'https://example.invalid/r.git')
        self.assertNotIn('secret-token', json.dumps(status, ensure_ascii=False))
        line = "fatal: unable to access 'https://user:p@ss@github.com/x.git/': Could not resolve host"
        self.assertNotIn('p@ss', vcs.redact(line))
        self.assertIn('https://github.com/x.git/', vcs.redact(line))
        self.assertEqual(vcs.redact('git@github.com:me/r.git'), 'git@github.com:me/r.git')


class HistoryTests(VcsCase):
    def test_history_follows_renames_and_show_returns_old_bytes(self):
        root, repo = self.make_repo()
        self.assertEqual(repo.history('a.md'), [])  # no commit yet
        repo.ensure()
        old, new = '02_研究内容/C04_理论/推导笔记.md', '02_研究内容/C04_理论/新 名字[1].md'
        (root / old).parent.mkdir(parents=True)
        (root / old).write_bytes('第一版\r\n'.encode('utf-8') * 20)
        first = repo.commit('第一版')
        (root / old).write_bytes('第二版\r\n'.encode('utf-8') * 20)
        repo.commit('第二版')
        git(root, '--literal-pathspecs', 'mv', old, new)
        repo.commit('改名')
        commits = repo.history(new)
        self.assertEqual([c['message'] for c in commits], ['改名', '第二版', '第一版'])
        self.assertTrue({'sha', 'short', 'date', 'message'} <= set(commits[0]))
        self.assertEqual([c['path'] for c in commits], [new, old, old])
        self.assertEqual(commits[2]['sha'], first['sha'])
        self.assertRegex(commits[0]['date'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d')
        self.assertEqual(repo.show(first['sha'], old), '第一版\r\n'.encode('utf-8') * 20)
        self.assertEqual(repo.show(commits[0]['short'], new), '第二版\r\n'.encode('utf-8') * 20)
        # The current name also reaches versions from before the rename.
        self.assertEqual(repo.show(first['short'], new), '第一版\r\n'.encode('utf-8') * 20)
        self.assertEqual(len(repo.history(new, limit=1)), 1)
        self.assertEqual(repo.history('从未提交.md'), [])
        with self.assertRaises(FileNotFoundError):
            repo.show(first['sha'], '从未提交.md')
        with self.assertRaises(FileNotFoundError):
            repo.show('1234567', new)

    def test_history_and_show_validate_paths_and_revisions(self):
        root, repo = self.make_repo()
        repo.ensure()
        sha = git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
        for path in ('', '../x.md', 'a/../b.md', '/abs.md', 'C:/x.md', 'a\\b.md', './a.md', 'a//b.md', 'a/',
                     'a\nb.md', ':(glob)*', None):
            with self.assertRaises(ValueError, msg=repr(path)):
                repo.history(path)
            with self.assertRaises(ValueError, msg=repr(path)):
                repo.show(sha, path)
        for rev in ('', 'abc', 'xyz1234', 'HEAD', 'a' * 41, '1234567~1', '-123456', sha + ' ', 1234567):
            with self.assertRaises(ValueError, msg=repr(rev)):
                repo.show(rev, '.gitignore')
        with self.assertRaises(ValueError):
            repo.history('.gitignore', limit='5')
        self.assertEqual(repo.show(sha.upper()[:12], '.gitattributes'), b'* -text\n')


class SafetyTests(VcsCase):
    def test_every_git_call_uses_required_options_without_a_shell(self):
        root, repo = self.make_repo()
        calls, real_popen = [], subprocess.Popen

        def spy(args, *rest, **kwargs):
            calls.append((list(args), kwargs))
            return real_popen(args, *rest, **kwargs)

        with patch.object(vcs.subprocess, 'Popen', side_effect=spy):
            repo.ensure()
            (root / 'a.md').write_text('a\n', encoding='utf-8')
            repo.commit('检查参数')
            repo.status()
            repo.history('a.md')
            repo.push()
        git_calls = [(args, kwargs) for args, kwargs in calls if args[1:2] != ['--version'] and 'taskkill' not in args[0]]
        self.assertGreater(len(git_calls), 10)
        for args, kwargs in git_calls:
            self.assertEqual(args[1:9], ['-C', str(root), '-c', 'core.autocrlf=false', '-c', 'core.safecrlf=false',
                                         '-c', 'core.quotepath=false'], args)
            self.assertFalse(kwargs.get('shell'))
            self.assertEqual(kwargs.get('creationflags', 0) & NO_WINDOW, NO_WINDOW)

    def test_write_lock_is_held_while_git_reads_the_work_tree(self):
        class TrackingLock:
            def __init__(self):
                self.lock, self.depth = threading.RLock(), 0

            def acquire(self):
                self.lock.acquire()
                self.depth += 1

            def release(self):
                self.depth -= 1
                self.lock.release()

            __enter__ = acquire

            def __exit__(self, *exc):
                self.release()

        store_lock = TrackingLock()
        root, repo = self.make_repo(write_lock=store_lock)
        repo.ensure()
        seen, real_execute = [], vcs._execute

        def spy(cmd, data, timeout, idle=None):
            rest = cmd[3 + 2 * len(vcs.GIT_OPTIONS):]
            if rest[:1] == ['--literal-pathspecs']:
                rest = rest[1:]
            seen.append((rest[0] if rest else '', store_lock.depth))
            return real_execute(cmd, data, timeout, idle=idle)

        (root / 'a.md').write_text('a\n', encoding='utf-8')
        with patch.object(vcs, '_execute', side_effect=spy):
            repo.commit('写锁')
            during_commit, seen[:] = list(seen), []
            repo.status()
        self.assertTrue({'status', 'add', 'commit'} <= {command for command, _ in during_commit}, during_commit)
        self.assertTrue(all(depth == 1 for _, depth in during_commit), during_commit)
        self.assertEqual(next(depth for command, depth in seen if command == 'status'), 1)  # the work-tree scan runs under the write lock
        self.assertEqual(dict(seen)['log'], 0)  # released once git no longer reads the work tree
        self.assertEqual(store_lock.depth, 0)
        # While the store holds its lock, a commit waits; the holder can still read history.
        (root / 'b.md').write_text('b\n', encoding='utf-8')
        done = threading.Event()
        with store_lock:
            worker = threading.Thread(target=lambda: (repo.commit('等待写锁'), done.set()))
            worker.start()
            self.assertFalse(done.wait(0.5))
            self.assertEqual(len(repo.history('a.md')), 1)
            self.assertTrue(repo.status()['is_repo'])
        worker.join(30)
        self.assertTrue(done.is_set())
        self.assertEqual(messages(root)[0], '等待写锁')

    @unittest.skipUnless(os.name == 'nt', 'taskkill /T is Windows-only')
    def test_timeout_stops_the_whole_process_tree(self):
        # The child starts a grandchild that holds the output pipe, like cmd\git.exe and its real git.
        code = ("import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=sys.stdout, stderr=sys.stderr); "
                "time.sleep(60)")
        started = time.monotonic()
        with self.assertRaises(vcs.GitTimeout):
            vcs._execute([sys.executable, '-c', code], None, 2)
        self.assertLess(time.monotonic() - started, 9)

    def test_inherited_git_location_variables_cannot_redirect_commits(self):
        other = self.base / '别的仓库'
        other.mkdir()
        git(other, 'init', '-q', '-b', 'main')
        root, repo = self.make_repo()
        with patch.dict(os.environ, {'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(other)}):
            repo.ensure()
        self.assertEqual(messages(root), [vcs.INIT_MESSAGE])
        self.assertNotEqual(git(other, 'rev-parse', '--verify', '-q', 'HEAD', check=False).returncode, 0)

    def test_concurrent_commits_and_status_do_not_collide(self):
        root, repo = self.make_repo()
        repo.ensure()
        errors = []

        def work(index):
            try:
                (root / f'并发{index}.md').write_text(f'{index}\n', encoding='utf-8')
                repo.commit(f'并发 {index}')
                repo.status()
            except Exception as exc:  # noqa: BLE001 - collected for the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(errors, [])
        self.assertTrue({f'并发{index}.md' for index in range(4)} <= tracked(root, 'HEAD'))
        self.assertLessEqual(commit_count(root), 5)
        self.assertFalse(repo.status()['dirty'])


if __name__ == '__main__':
    unittest.main()


class ReviewRegressionTests(VcsCase):
    """Problems reproduced by the v2 review of version management."""

    def test_embedded_repository_is_excluded_and_reported(self):
        root, repo = self.make_repo()
        repo.ensure()
        sub = root / '02_研究内容' / '仿真代码'
        sub.mkdir(parents=True)
        git(sub, 'init', '-q', '-b', 'main')
        (sub / 'solver.py').write_text('print(1)\n', encoding='utf-8')
        (root / '说明.md').write_text('x\n', encoding='utf-8')
        result = repo.commit('加入说明')
        self.assertEqual(result['embedded_repos'], ['02_研究内容/仿真代码'])
        self.assertNotIn('02_研究内容/仿真代码', tracked(root))
        self.assertEqual(repo.status()['embedded_repos'], ['02_研究内容/仿真代码'])

    def test_files_still_being_written_wait_for_a_follow_up_commit(self):
        root, repo = self.make_repo(recent_seconds=60)
        repo.ensure()
        (root / '结果.csv').write_text('1,2\n', encoding='utf-8')
        (root / '说明.md').write_text('x\n', encoding='utf-8')
        result = repo.commit('一起保存')
        self.assertTrue(result['deferred'])
        self.assertIn('说明.md', tracked(root), 'files the store writes are committed at once')
        self.assertNotIn('结果.csv', tracked(root))
        old = time.time() - 120
        os.utime(root / '结果.csv', (old, old))
        repo.commit('补充')
        self.assertIn('结果.csv', tracked(root))

    def test_failed_auto_commit_is_retried_with_its_message(self):
        root, repo = self.make_repo(coalesce_seconds=0.1)
        repo.ensure()
        lock = root / '.git' / 'index.lock'
        lock.write_text('', encoding='utf-8')
        (root / '正文.md').write_text('x\n', encoding='utf-8')
        with patch.object(vcs, 'RETRY_DELAYS', (0.5, 0.5, 0.5)):
            repo.commit_async('保存正文')
            self.wait_for(lambda: 'index.lock' in repo.status()['last_error'])
            lock.unlink()
            self.wait_for(lambda: '保存正文' in git(root, 'log', '--format=%B').stdout.decode())
        self.wait_for(lambda: repo.status()['last_error'] == '')

    def test_info_attributes_overrides_nested_gitattributes(self):
        root, repo = self.make_repo()
        repo.ensure()
        tool = root / '工具箱'
        tool.mkdir()
        (tool / '.gitattributes').write_text('* text=auto\n', encoding='utf-8')
        (tool / 'a.m').write_bytes(b'x = 1;\r\ny = 2;\r\n')
        repo.commit('加入工具箱')
        self.assertEqual(git(root, 'cat-file', 'blob', 'HEAD:工具箱/a.m').stdout, b'x = 1;\r\ny = 2;\r\n')

    def test_push_uses_the_current_branch_and_survives_slow_progress(self):
        root, repo = self.make_repo(name='分支 目录')
        bare = self.base / 'remote.git'
        git(self.base, 'init', '-q', '--bare', str(bare))
        git(root, 'remote', 'add', 'origin', str(bare))
        git(root, 'checkout', '-q', '-b', 'master')
        (root / 'a.md').write_text('a\n', encoding='utf-8')
        repo.commit('第一版')
        record = repo.push()
        self.assertTrue(record['ok'], record)
        self.assertIn(b'refs/heads/master', git(bare, 'show-ref').stdout)
