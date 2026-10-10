"""A managed Soulseek share tree, with library files shared by default."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

AUDIO = {'.mp3', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wav', '.aiff', '.aif', '.alac', '.wma'}


class Sharing:
    def __init__(self, library, root, state, scan=None, link_library=None):
        self.library = Path(library).resolve()
        self.root = Path(root).resolve()
        self.link_library = Path(link_library or library).resolve()
        if self.root.is_relative_to(self.library) or self.library.is_relative_to(self.root):
            raise ValueError('Share directory must be separate from the library')
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = Path(state) / 'sharing.json'
        self.lock = threading.RLock()
        self.scan = scan
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'enabled': True, 'excluded': [], 'managed': [], 'scan_pending': True,
        }
        self.error = None

    def _save(self):
        temporary = self.path.with_suffix('.tmp')
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(self.data, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def _relative(self, path):
        path = Path(path)
        if not path.is_absolute():
            path = self.library / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.library) or path.is_symlink():
            raise ValueError('Sharing only supports library files')
        return str(resolved.relative_to(self.library))

    def selected(self, path):
        with self.lock:
            return self._relative(path) not in self.data['excluded']

    def status(self):
        with self.lock:
            return {'default_shared': True, 'enabled': self.data['enabled'],
                    'excluded_count': len(self.data['excluded']),
                    'shared_count': len(self.data['managed']),
                    'scan_pending': self.data['scan_pending'], 'error': self.error}

    def configure(self, *, enabled=None, path=None, shared=None):
        with self.lock:
            if enabled is not None:
                self.data['enabled'] = enabled
            if path is not None:
                name = self._relative(path)
                excluded = set(self.data['excluded'])
                excluded.discard(name) if shared else excluded.add(name)
                self.data['excluded'] = sorted(excluded)
            self.data['scan_pending'] = True
            self._save()
            self.reconcile()
            return self.status()

    def _destination(self, name):
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Invalid managed share path')
        target = self.root / relative
        if not target.resolve().is_relative_to(self.root) or target.is_symlink():
            raise ValueError('Invalid managed share destination')
        return target

    def reconcile(self):
        """Link accepted audio and remove only links recorded in our manifest."""
        with self.lock:
            try:
                desired = {}
                if self.data['enabled']:
                    excluded = set(self.data['excluded'])
                    for source in self.library.rglob('*'):
                        if source.suffix.lower() not in AUDIO or not source.is_file() or source.is_symlink():
                            continue
                        name = self._relative(source)
                        if name not in excluded:
                            desired[name] = source
                managed = set(self.data['managed'])
                changed = False
                for name in sorted(managed - desired.keys()):
                    target = self._destination(name)
                    target.unlink(missing_ok=True)
                    managed.remove(name)
                    parent = target.parent
                    while parent != self.root:
                        try:
                            parent.rmdir()
                        except OSError:
                            break
                        parent = parent.parent
                    changed = True
                # Persist additions as they happen so restart recovery owns every link.
                self.data['managed'] = sorted(managed)
                self._save()
                for name, source in desired.items():
                    if not source.exists():
                        continue  # replaced or removed while scanning; the next refresh sees it
                    target = self._destination(name)
                    if target.exists() and os.path.samefile(target, source):
                        if name not in managed:
                            managed.add(name)
                            changed = True
                        continue
                    if target.exists():
                        if name not in managed:
                            raise ValueError('Share directory contains an unmanaged file')
                        target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Journal ownership before linking, allowing interrupted additions to retry.
                    managed.add(name)
                    self.data['managed'] = sorted(managed)
                    self.data['scan_pending'] = True
                    self._save()
                    link_source = self.link_library / name
                    if not os.path.samefile(source, link_source):
                        raise ValueError("Sharing source mount does not match the library")
                    os.link(link_source, target)
                    changed = True
                self.data['managed'] = sorted(managed)
                if changed:
                    self.data['scan_pending'] = True
                self._save()
                self.error = None
            except Exception:
                self.error = 'Could not update Soulseek shares. Your library is unchanged; the app will retry.'
                raise

    def refresh(self):
        with self.lock:
            self.reconcile()
            if self.data['scan_pending'] and self.scan:
                try:
                    if self.scan():
                        self.data['scan_pending'] = False
                        self._save()
                    else:
                        self.error = 'Soulseek is scanning. Sharing changes will be retried.'
                except Exception:
                    self.error = 'Soulseek is unavailable. Sharing changes are saved and will be retried.'
