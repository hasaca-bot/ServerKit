"""Render-style deploy controls for the git auto-deploy path.

Two independent switches decide whether a *webhook-triggered* deploy runs:

* a skip marker in the head commit message (``[skip deploy]``, ``[deploy skip]``,
  ``[skip cd]``, ``[cd skip]``, case-insensitive), and
* a per-app build filter (``paths`` include globs, ``ignored_paths`` exclude
  globs, plus ``root_dir``) matched against the files changed by the push.

Manual deploys never consult this module. Pull-request previews have their own
marker (``[skip preview]`` / ``[preview skip]``) read from the PR title.

Everything here is pure (no DB, no Flask) so it is cheap to test.
"""
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SKIP_DEPLOY_RE = re.compile(r'\[\s*(?:skip\s+deploy|deploy\s+skip|skip\s+cd|cd\s+skip)\s*\]', re.I)
_SKIP_PREVIEW_RE = re.compile(r'\[\s*(?:skip\s+preview|preview\s+skip)\s*\]', re.I)

MAX_PATTERNS = 50
MAX_PATTERN_LEN = 255

# GitHub and GitLab cap the commits listed in a push payload (20); past that the
# file lists are incomplete, so we cannot prove a filter excludes the push.
_PAYLOAD_COMMIT_CAP = 20


def has_skip_deploy_marker(message: Optional[str]) -> bool:
    return bool(message and _SKIP_DEPLOY_RE.search(message))


def has_skip_preview_marker(title: Optional[str]) -> bool:
    return bool(title and _SKIP_PREVIEW_RE.search(title))


# ---------------------------------------------------------------- globs

def _norm_path(path: str) -> str:
    path = (path or '').replace('\\', '/').strip()
    while path.startswith('./'):
        path = path[2:]
    return path.lstrip('/')


def _glob_to_regex(pattern: str) -> 're.Pattern':
    """``**`` spans directories, ``*`` and ``?`` stay within one segment."""
    pattern = _norm_path(pattern)
    if pattern.endswith('/'):
        pattern += '**'
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == '*':
            if pattern[i:i + 2] == '**':
                i += 2
                if pattern[i:i + 1] == '/':
                    i += 1
                    out.append('(?:.*/)?')   # "a/**/b" also matches "a/b"
                else:
                    out.append('.*')
                continue
            out.append('[^/]*')
        elif c == '?':
            out.append('[^/]')
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile('^' + ''.join(out) + '$')


def glob_match(pattern: str, path: str) -> bool:
    """Match a repo-relative path. A pattern without ``/`` also matches the
    file name at any depth (``*.md`` ignores docs everywhere)."""
    path = _norm_path(path)
    norm = _norm_path(pattern)
    if not norm:
        return False
    regex = _glob_to_regex(norm)
    if regex.match(path):
        return True
    if '/' not in norm:
        return bool(regex.match(path.rsplit('/', 1)[-1]))
    return False


# ---------------------------------------------------------------- filter config

def validate_build_filter(value: Any) -> Tuple[Optional[Dict[str, List[str]]], Optional[str]]:
    """``(clean, error)``. ``None``/empty clears the filter (``clean`` is None)."""
    if value in (None, '', {}):
        return None, None
    if not isinstance(value, dict):
        return None, 'build_filter must be an object'
    unknown = set(value) - {'paths', 'ignored_paths', 'ignoredPaths'}
    if unknown:
        return None, f'Unknown build_filter key: {sorted(unknown)[0]}'
    clean: Dict[str, List[str]] = {}
    raw_map = {'paths': value.get('paths'),
               'ignored_paths': value.get('ignored_paths', value.get('ignoredPaths'))}
    for key, raw in raw_map.items():
        if raw is None:
            raw = []
        if isinstance(raw, str):
            raw = raw.splitlines()
        if not isinstance(raw, list):
            return None, f'build_filter.{key} must be a list of globs'
        items = []
        for item in raw:
            if not isinstance(item, str):
                return None, f'build_filter.{key} entries must be text'
            item = item.strip()
            if not item:
                continue
            if len(item) > MAX_PATTERN_LEN:
                return None, f'build_filter.{key} entries must be at most {MAX_PATTERN_LEN} characters'
            if '..' in _norm_path(item).split('/'):
                return None, f'build_filter.{key} entries cannot contain ".."'
            items.append(item)
        if len(items) > MAX_PATTERNS:
            return None, f'build_filter.{key} allows at most {MAX_PATTERNS} entries'
        clean[key] = items
    if not clean['paths'] and not clean['ignored_paths']:
        return None, None
    return clean, None


def validate_root_dir(value: Any) -> Tuple[Optional[str], Optional[str]]:
    if value in (None, ''):
        return None, None
    if not isinstance(value, str):
        return None, 'root_dir must be text'
    norm = _norm_path(value).rstrip('/')
    if len(norm) > 500:
        return None, 'root_dir must be at most 500 characters'
    if '..' in norm.split('/'):
        return None, 'root_dir must be a relative path inside the repository'
    return norm or None, None


def load_build_filter(raw: Any) -> Dict[str, List[str]]:
    """Stored JSON text (or dict) -> ``{'paths': [...], 'ignored_paths': [...]}``."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, dict):
        return {'paths': [], 'ignored_paths': []}
    return {
        'paths': [p for p in (raw.get('paths') or []) if isinstance(p, str)],
        'ignored_paths': [p for p in (raw.get('ignored_paths') or []) if isinstance(p, str)],
    }


# ---------------------------------------------------------------- payload parsing

def head_commit_message(source: str, payload: Optional[Dict]) -> Optional[str]:
    """Full (untruncated) message of the pushed head commit, or None."""
    if not isinstance(payload, dict):
        return None
    try:
        if source == 'bitbucket':
            changes = (payload.get('push') or {}).get('changes') or []
            if changes:
                return ((changes[0].get('new') or {}).get('target') or {}).get('message')
            return None
        head = payload.get('head_commit')
        if isinstance(head, dict) and head.get('message') is not None:
            return head.get('message')
        commits = payload.get('commits') or []
        if not commits:
            return None
        if source == 'gitlab':
            # GitLab lists oldest first; the head is the checkout commit.
            after = payload.get('checkout_sha') or payload.get('after')
            for c in commits:
                if c.get('id') == after:
                    return c.get('message')
        return commits[-1].get('message')
    except (AttributeError, TypeError):
        return None


def changed_files(source: str, payload: Optional[Dict]) -> Optional[set]:
    """Union of added/modified/removed over every commit in the push.

    ``None`` means "unknown" (no file lists, truncated list, empty push): the
    caller must fail open.
    """
    if not isinstance(payload, dict) or source == 'bitbucket':
        return None      # Bitbucket push payloads carry no file lists
    commits = payload.get('commits')
    if not isinstance(commits, list) or not commits:
        return None
    if len(commits) >= _PAYLOAD_COMMIT_CAP:
        return None
    total = payload.get('total_commits_count')
    if isinstance(total, int) and total > len(commits):
        return None
    files = set()
    for commit in commits:
        if not isinstance(commit, dict):
            return None
        for key in ('added', 'modified', 'removed'):
            for f in commit.get(key) or []:
                if isinstance(f, str):
                    files.add(_norm_path(f))
    # A merge commit or empty push lists no files even though content changed.
    return files or None


def _under(root: str, path: str) -> bool:
    return path == root or path.startswith(root + '/')


def evaluate_build_filter(root_dir: Optional[str], build_filter: Any,
                          files: Optional[Iterable[str]]) -> Tuple[bool, str]:
    """``(should_deploy, reason)``.

    A file triggers a deploy when it is under ``root_dir`` (if set), matches an
    include glob (if any are set) and matches no ignore glob. With no filter
    configured, or no file list, the answer is always yes (fail open).
    """
    cfg = load_build_filter(build_filter)
    root = _norm_path(root_dir or '').rstrip('/')
    if not (cfg['paths'] or cfg['ignored_paths'] or root):
        return True, ''
    if files is None:
        return True, 'push payload has no file list; deploying (fail-open)'
    for path in files:
        if root and not _under(root, path):
            continue
        if cfg['paths'] and not any(glob_match(p, path) for p in cfg['paths']):
            continue
        if any(glob_match(p, path) for p in cfg['ignored_paths']):
            continue
        return True, ''
    return False, 'no changed file matches the build filter'
