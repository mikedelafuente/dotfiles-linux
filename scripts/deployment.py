#!/usr/bin/env python3
"""Source-owned installed copy and one previous backup. See docs/deployment.md for the state contract."""
import argparse
import ast
from contextlib import contextmanager, nullcontext
import fcntl
import io
import json
import os
from pathlib import Path
import re
import pwd
import shutil
import stat
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit, urlunsplit
import uuid

sys.dont_write_bytecode = True

HOME_FILES = {".bashrc", ".inputrc", ".profile", ".gitignore_global", ".nvim-cheatsheet.md",
              ".welcome.md", ".packages.md", ".tmux.conf"}
# App-written configs: copied once when absent, then owned by the app and allowed to drift.
SEEDED_CONFIGS = {"zed/settings.json", "zed/keymap.json", "Code/User/settings.json"}
SECRET_NAMES = {"auth.json", "models-store.json", "user_credentials.json", ".env",
                ".dotfiles_bootstrap_config", ".git-credentials", ".netrc", ".npmrc",
                "credentials.json", "auth.toml", ".ssh", ".claude.json"}


class Pending(ValueError):
    pass


def git(root, *args, check=True):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if check and result.returncode:
        # Git diagnostics can contain credentials from remote URLs.
        raise Pending(f"Source git operation failed: {args[0]}; inspect the source checkout locally")
    return result


def clean_url(raw):
    raw = raw.strip()
    if raw.startswith("git@"):
        raw = "https://" + raw[4:].replace(":", "/", 1)
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http", "ssh", "file"}:
        raise Pending("Unsupported source URL; configure a credential-free origin")
    host = parsed.hostname or ""
    if parsed.port:
        host += f":{parsed.port}"
    return urlunsplit(("https" if parsed.scheme == "ssh" else parsed.scheme,
                       host, parsed.path.removesuffix(".git"), "", ""))


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_text(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".dfa-state-" + uuid.uuid4().hex)
    with temporary.open("x") as file:
        temporary.chmod(mode)
        file.write(text)
        file.flush(); os.fsync(file.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, data):
    write_text(path, json.dumps(data, sort_keys=True, indent=2) + "\n")


def lexical(path):
    return Path(os.path.abspath(path))


def link_value(path):
    return str(lexical(path.parent / os.readlink(path))) if path.is_symlink() else None


def copy_file(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        dest.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, dest)


def safe_parents(path, boundary):
    for parent in [path.parent, *path.parent.parents]:
        if parent == boundary:
            break
        if parent.is_symlink():
            raise Pending(f"Redirected parent preserved: {parent}")


def json_value(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Pending(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(Pending(f"Invalid JSON: {value}")))


def parsed_json(path, data):
    # JSONC is accepted only for known comment-capable app paths and .jsonc.
    comments = path.suffix == ".jsonc" or str(path).endswith(("config/zed/settings.json", "config/zed/keymap.json"))
    if comments:
        pattern = r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/|,\s*(?=[}\]])'
        data = re.sub(pattern, lambda m: m[0] if m[0].startswith('"') else " " if m[0].startswith("/") else "",
                      data.decode() if isinstance(data, bytes) else data, flags=re.S)
    return json_value(data)


def validate(path):
    if path.is_symlink():
        return
    data = path.read_bytes()
    if path.suffix in {".json", ".jsonc"}:
        parsed_json(path, data)
    elif path.suffix == ".toml":
        tomllib.loads(data.decode())
    elif path.suffix == ".py":
        ast.parse(data, filename=str(path))
    elif data.startswith((b"#!/bin/bash", b"#!/usr/bin/env bash")) or path.suffix == ".sh":
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True)
        if result.returncode:
            raise Pending("Bash syntax validation failed")
    # Format parsing checks syntax. Application schemas/runtime remain unverified.

class Deployment:
    def __init__(self, home, working_tree=False):
        self.working_tree = working_tree
        self.home = lexical(home)
        self.root = self.home / ".local/share/workstation"
        self.active = self.root / "config"
        self.previous = self.root / "previous"
        self.stage = self.root / "staging"
        self.journal = self.root / "transaction.json"
        self.seeds = {str(self.home / ".config" / rel): "config/" + rel for rel in SEEDED_CONFIGS}

    @contextmanager
    def locked(self, recovery=False):
        safe_parents(self.root / "lock", self.home)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        for name in ("lock", "blue", "green", "staging", "transaction.json"):
            if (self.root / name).is_symlink():
                raise Pending(f"Redirected deployment path preserved: {name}")
        with (self.root / "lock").open("a") as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise Pending("Another deployment is running; retry when it finishes") from error
            if self.previous.exists() and not self.previous.is_symlink():
                raise Pending("Previous backup link is a foreign real target")
            if self.previous.is_symlink() and link_value(self.previous) not in {str(self.root / "blue"), str(self.root / "green")}:
                raise Pending("Previous backup link is foreign")
            if self.journal.exists() and not recovery:
                raise Pending("Interrupted activation; run dfa-deploy recover before retrying")
            yield

    def state(self, folder):
        if (folder / ".dfa").is_symlink() or any((folder / ".dfa" / name).is_symlink() for name in ("sources.json", "links.json")):
            raise Pending("Installed source/link records are redirected")
        sources = read_json(folder / ".dfa/sources.json")
        recorded = read_json(folder / ".dfa/links.json")
        if not isinstance(sources, list) or not sources or not isinstance(recorded, dict):
            raise Pending("Missing installed source/link records; restore the installation")
        if not all(isinstance(s, dict) for s in sources) or sources[0].get("id") != "primary" or len({s.get("id") for s in sources}) != len(sources):
            raise Pending("Invalid installed source list")
        for source in sources:
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", source.get("id", "")) or not Path(source.get("path", "")).is_absolute():
                raise Pending("Invalid source record")
        links = {}
        for target, artifact in recorded.items():
            if not Path(target).is_absolute() or not isinstance(artifact, str) or not artifact or Path(artifact).is_absolute() or ".." in Path(artifact).parts:
                raise Pending("Invalid installed link record")
            links[target] = {"artifact": artifact}
        return {"sources": sources, "links": links}

    def current(self):
        if not self.active.is_symlink():
            if self.active.exists():
                raise Pending("Deployment config path is a foreign real directory")
            return None, {"sources": [], "links": {}}
        folder = self.active.resolve()
        if folder in (self.root / "blue", self.root / "green"):
            return folder, self.state(folder)
        # Only migration reads the old manifest; no fingerprints or merge state survive.
        if folder.name == "tree" and folder.parent.parent == self.root / "generations":
            legacy = read_json(folder.parent / "manifest.json")
            if not legacy or legacy.get("version") != 1 or not legacy.get("sources") or not isinstance(legacy.get("links"), dict):
                raise Pending("Invalid legacy deployment; restore it before migration")
            if not all(isinstance(s, dict) and re.fullmatch(r"primary|[0-9a-f]{16}", s.get("id", "")) and Path(s.get("path", "")).is_absolute() for s in legacy["sources"]):
                raise Pending("Invalid legacy source records")
            state = {"sources": [{k: s.get(k) for k in ("id", "path", "type", "overwritable", "url", "commit")}
                                 for s in legacy["sources"]], "links": legacy["links"], "legacy": True}
            for info in state["links"].values():
                key = info.get("artifact", "")
                if not key or Path(key).is_absolute() or ".." in Path(key).parts:
                    raise Pending("Invalid legacy link record")
            return folder, state
        raise Pending("Deployment config link is foreign")

    def source_name(self, path, used):
        base = re.sub(r"[^a-zA-Z0-9_-]+", "-", path.name).strip("-") or "source"
        name = base
        number = 2
        while name == "primary" or name in used:
            name = f"{base}-{number}"
            number += 1
        return name

    def save_state(self, folder, sources, links):
        write_json(folder / ".dfa/sources.json", sources)
        write_json(folder / ".dfa/links.json", {target: info["artifact"] for target, info in links.items()})

    def source(self, explicit=None):
        _, state = self.current()
        previous = state["sources"][0] if state["sources"] else None
        path = Path(explicit).resolve() if explicit else Path(previous["path"]) if previous else None
        if path is None or not (path / "scripts/sync.sh").is_file():
            raise Pending("Source checkout unavailable; restore it or run dfa-deploy rebind /actual/checkout. Installed tools remain usable")
        if self.root == path or self.root in path.parents:
            raise Pending("Installed copies cannot be used as the editable source")
        url = clean_url(git(path, "remote", "get-url", "origin").stdout.decode())
        commit = git(path, "rev-parse", "HEAD").stdout.decode().strip()
        if previous and (str(path) != previous["path"] or url != previous["url"]):
            raise Pending("Source provenance differs; explicitly use dfa-deploy rebind PATH [--accept-origin-change]")
        if previous and git(path, "merge-base", "--is-ancestor", previous["commit"], commit,
                            check=False).returncode:
            raise Pending("Recorded source commit is not in this checkout history; restore the recorded source")
        return path

    def sources(self, primary):
        # One source: this checkout. Nothing is linked into an agent's folders (skills,
        # rules, AGENTS.md, Pi data), so there are no extra sources to register.
        return [(primary, "standard", False, "primary")]

    def inventory(self, primary, incoming):
        artifacts, links, sources = {}, {}, []
        def add_link(target, relative, legacy):
            links[str(target)] = {"artifact": relative, "legacy": str(lexical(legacy))}
        for path, kind, can_replace, name in self.sources(primary):
            git_source = git(path, "rev-parse", "--show-toplevel", check=False)
            is_git = git_source.returncode == 0
            source = {"id": name, "path": str(path), "type": kind, "overwritable": can_replace,
                      "url": clean_url(git(path, "remote", "get-url", "origin").stdout.decode()) if is_git else None,
                      "commit": git(path, "rev-parse", "HEAD").stdout.decode().strip() if is_git else None}
            sources.append(source)
            working_tree = is_git and self.working_tree and name == "primary"
            prefix = "" if name == "primary" else f"extras/{name}/"
            committed = {}
            if is_git and not working_tree:
                prefix_in_git = git(path, "rev-parse", "--show-prefix").stdout.decode().strip().rstrip("/")
                revision = source["commit"] + (":" + prefix_in_git if prefix_in_git else "")
                entries = []
                for record in git(path, "ls-tree", "--full-tree", "-rz", revision).stdout.split(b"\0"):
                    if not record:
                        continue
                    metadata, filename = record.split(b"\t", 1)
                    mode, kind_in_git, object_id = metadata.split()
                    relative = Path(os.fsdecode(filename))
                    if kind_in_git != b"blob" or any(part in SECRET_NAMES or part in {".git", "node_modules", "__pycache__"}
                                                   or part.startswith(".env.") for part in relative.parts):
                        continue
                    entries.append((relative, mode, object_id))
                # Raw blobs ignore export-ignore/export-subst attributes and avoid reading excluded secrets.
                batch = subprocess.run(["git", "-C", str(path), "cat-file", "--batch"],
                                       input=b"".join(oid + b"\n" for _, _, oid in entries), capture_output=True)
                if batch.returncode:
                    raise Pending("Cannot read committed source blobs")
                stream = io.BytesIO(batch.stdout)
                for relative, mode, object_id in entries:
                    header = stream.readline().split()
                    if len(header) != 3 or header[0] != object_id or header[1] != b"blob":
                        raise Pending("Invalid committed source blob response")
                    data = stream.read(int(header[2]))
                    if stream.read(1) != b"\n":
                        raise Pending("Incomplete committed source blob")
                    committed[relative] = {"target": data.decode() if mode == b"120000" else None,
                                           "data": None if mode == b"120000" else data,
                                           "mode": 0o755 if mode == b"100755" else 0o644}
                files = list(committed)
            elif working_tree:
                files = {Path(os.fsdecode(p)) for p in git(path, "ls-files", "-z", "--cached", "--others",
                                                          "--exclude-standard").stdout.split(b"\0") if p}
            else:
                files = [p.relative_to(path) for p in path.rglob("*") if p.is_file() or p.is_symlink()]
            for relative in sorted(files):
                if relative.is_absolute() or ".." in relative.parts:
                    raise Pending("Unsafe source artifact path")
                if any(p in SECRET_NAMES or p in {".git", "node_modules", "__pycache__"}
                       or p.startswith(".env.") for p in relative.parts):
                    continue
                if name == "primary" and relative.parts[0] == ".dfa":
                    raise Pending("Source .dfa folder conflicts with installed bookkeeping")
                src = path / relative
                if (not is_git or working_tree) and not src.exists() and not src.is_symlink():
                    continue
                if (not is_git or working_tree) and src.is_dir() and not src.is_symlink():
                    continue
                key = prefix + relative.as_posix()
                dest = incoming / key
                if working_tree:
                    safe_parents(src, path)
                link = committed[relative]["target"] if is_git and not working_tree else os.readlink(src) if src.is_symlink() else None
                if link is not None:
                    resolved = lexical(src.parent / link)
                    # Explicit rebind retains the mapping for old absolute internal source links.
                    if not resolved.is_relative_to(path):
                        _, previous = self.current()
                        old_source = next((entry for entry in previous["sources"] if entry["id"] == name), None)
                        if old_source and resolved.is_relative_to(old_source["path"]):
                            resolved = path / resolved.relative_to(old_source["path"])
                    if not resolved.is_relative_to(path):
                        raise Pending(f"External source symlink: {src}")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    target = incoming / prefix / resolved.relative_to(path)
                    dest.symlink_to(os.path.relpath(target, dest.parent))
                elif is_git and not working_tree:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(committed[relative]["data"])
                    dest.chmod(committed[relative]["mode"])
                else:
                    copy_file(src, dest)
                artifacts[key] = {"source": name, "relative": relative.as_posix(),
                                  "origin": str(src)}
                if name == "primary":
                    if relative.parts[0] == "home":
                        sub = relative.relative_to("home")
                        if str(sub) in HOME_FILES or str(sub).startswith(".local/bin/"):
                            add_link(self.home / sub, key, src)
                    elif relative.parts[0] == "config" and str(self.home / ".config" / relative.relative_to("config")) not in self.seeds:
                        add_link(self.home / ".config" / relative.relative_to("config"), key, src)
        alias_key = "home/.local/bin/rebind-window-push"
        if alias_key in artifacts:
            add_link(self.home / ".local/bin/rebind-monitor-moves", alias_key, primary / alias_key)
        key = "DEPLOYMENT-README.md"
        output = incoming / key
        output.write_text(guidance_readme(primary))
        artifacts[key] = {"source": None, "origin": "installed-copy guidance"}
        return artifacts, links, sources

    def link_snapshot(self, target, info, old):
        path = Path(target)
        safe_parents(path, self.home)
        value = link_value(path)
        previous = old.get(target)
        if value == str(self.active / info["artifact"]) or (previous and value == str(self.active / previous["artifact"])):
            return os.readlink(path)
        if info.get("legacy") and value == info["legacy"] and value != str(path):
            if not path.exists():
                raise Pending(f"Broken legacy link preserved: {path}; restore its source")
            return os.readlink(path)
        if path.exists() or path.is_symlink():
            raise Pending(f"Unmanaged target preserved: {path}; relocate it explicitly")
        return None

    def preserve_legacy_directories(self, links, snapshots, artifacts):
        known = {lexical(item["origin"]) for item in artifacts.values() if item.get("source")}
        for target, info in links.items():
            legacy = Path(info["legacy"])
            if snapshots[target] and link_value(Path(target)) == str(legacy) and legacy.is_dir():
                for path in legacy.rglob("*"):
                    if (path.is_file() or path.is_symlink()) and lexical(path) not in known:
                        raise Pending(f"Unmanaged legacy descendant preserved: {path}")

    def swap(self, path, target):
        safe_parents(path, self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.parent / (".dfa-link-" + uuid.uuid4().hex)
        try:
            temp.symlink_to(target)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def claude_snapshot(self):
        path = self.home / ".claude/CLAUDE.md"
        safe_parents(path, self.home)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise Pending("Claude instructions are redirected; preserve and relocate them explicitly")
        return {"text": path.read_text() if path.exists() else None,
                "mode": stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600}

    def claude_text(self, text):
        # The file is the user's own. Only drop the import lines older deployments added.
        lines = []
        for line in text.splitlines():
            imported = self.home / ".claude" / line[1:] if line.startswith("@dfa-rules-") else None
            owned = imported and imported.is_symlink() and (link_value(imported) or "").startswith(str(self.home / ".config/dotfiles-arch/rules-build") + "/")
            if not owned and line != "@dfa-deployment.md":
                lines.append(line)
        if len(lines) == len(text.splitlines()):
            return text
        return "\n".join(lines) + "\n" if lines else ""

    def write_claude(self, text, mode=0o600):
        path = self.home / ".claude/CLAUDE.md"
        safe_parents(path, self.home)
        write_text(path, text, mode)

    def snapshots(self, links, old):
        # Seeded targets leave link management; only an old managed link is still replaced.
        return {target: self.link_snapshot(target, links.get(target, old.get(target)), old)
                for target in links.keys() | old.keys()
                if target not in self.seeds or (link_value(Path(target)) or "").startswith(str(self.root) + "/")}

    def seed(self):
        for target, key in self.seeds.items():
            path, source = Path(target), self.active / key
            safe_parents(path, self.home)
            if not path.exists() and not path.is_symlink() and source.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, path)

    def same_copy(self, left, right):
        def entries(root):
            return {p.relative_to(root): p for p in root.rglob("*")
                    if p.relative_to(root).parts[0] != ".dfa" and (p.is_file() or p.is_symlink())}
        a, b = entries(left), entries(right)
        if a.keys() != b.keys():
            return False
        for key, path in a.items():
            other = b[key]
            if path.is_symlink() or other.is_symlink():
                if not (path.is_symlink() and other.is_symlink() and os.readlink(path) == os.readlink(other)):
                    return False
            elif stat.S_IMODE(path.stat().st_mode) != stat.S_IMODE(other.stat().st_mode) or path.read_bytes() != other.read_bytes():
                return False
        return True

    def deploy(self, primary, locked=False):
        with nullcontext() if locked else self.locked():
            tree, old = self.current()
            if self.stage.exists():
                shutil.rmtree(self.stage)
            self.stage.mkdir(mode=0o700)
            incoming = self.stage / "new"
            incoming.mkdir()
            try:
                artifacts, links, sources = self.inventory(primary, incoming)
                snapshots = self.snapshots(links, old["links"])
                if not tree:
                    self.preserve_legacy_directories(links, snapshots, artifacts)
                for key in artifacts:
                    path = incoming / key
                    if path.is_symlink() and (not path.exists() or not path.resolve().is_relative_to(incoming)):
                        raise Pending(f"Incomplete or external installed symlink: {key}")
                    validate(path)
                self.save_state(incoming, sources, links)
                claude = self.claude_snapshot()
                new_claude = self.claude_text(claude["text"] or "")
                if tree and not old.get("legacy") and sources == old["sources"] and {k: v['artifact'] for k,v in links.items()} == {k: v['artifact'] for k,v in old['links'].items()} and self.same_copy(incoming, tree) and (claude["text"] or "") == new_claude:
                    self.seed()
                    print("Installed copy already current")
                    return
                next_folder = self.root / ("green" if tree == self.root / "blue" or old.get("legacy") else "blue")
                if next_folder.exists():
                    self.state(next_folder)  # Never remove a foreign blue/green folder.
                backup = tree
                if old.get("legacy"):
                    # Preserve old installed bytes (including override effects), not baselines/hashes.
                    backup = self.root / "blue"
                    if backup.exists():
                        raise Pending("Existing blue folder blocks migration; inspect it explicitly")
                    shutil.copytree(tree, self.stage / "backup", symlinks=True)
                    backup_sources = []
                    backup_links = {k: dict(v) for k, v in old["links"].items()}
                    for source in old["sources"]:
                        entry = dict(source)
                        if source["id"] != "primary":
                            entry["id"] = self.source_name(Path(source["path"]), [s["id"] for s in backup_sources])
                            for prefix in ("extras", "generated/rules"):
                                old_path = self.stage / "backup" / prefix / source["id"]
                                if old_path.exists():
                                    old_path.rename(old_path.parent / entry["id"])
                            for info in backup_links.values():
                                for prefix in ("extras", "generated/rules"):
                                    info["artifact"] = info["artifact"].replace(f"{prefix}/{source['id']}/", f"{prefix}/{entry['id']}/", 1)
                        backup_sources.append(entry)
                    self.save_state(self.stage / "backup", backup_sources, backup_links)
                    manager = self.stage / "backup/scripts/deployment.py"
                    current_manager = Path(__file__).resolve()
                    if manager.is_file() and manager.read_bytes() != current_manager.read_bytes():
                        # v5 cannot read blue/green after rollback. Keep its original as backup data.
                        shutil.copy2(manager, self.stage / "backup/.dfa/legacy-deployment.py")
                        manager.unlink()
                        copy_file(current_manager, manager)
                transaction = {"previous": str(tree) if tree else None, "next": str(next_folder),
                               "backup_before": link_value(self.previous), "backup_after": str(backup) if backup else None,
                               "links": snapshots, "desired": {k: v["artifact"] for k,v in links.items()},
                               "claude": claude, "claude_new": new_claude, "legacy": bool(old.get("legacy")),
                               "phase": "prepared"}
                write_json(self.journal, transaction)
                try:
                    if next_folder.exists():
                        next_folder.rename(self.stage / "retired")
                    incoming.rename(next_folder)
                    if old.get("legacy"):
                        (self.stage / "backup").rename(backup)
                    self.check_links(transaction)
                    self.activate(transaction)
                    self.detach_models_store(primary)
                    transaction["phase"] = "committed"
                    write_json(self.journal, transaction)
                    self.finish(transaction)
                    self.seed()
                except BaseException:
                    # A hard interruption leaves the journal for recover; ordinary failures undo activation.
                    if transaction["phase"] == "prepared":
                        self.restore(transaction)
                    raise
                print(f"Deployed {next_folder.name}; installed copy: {self.active}; previous backup: {self.previous if backup else 'none'}")
                if old.get("legacy"):
                    print("Migrated to blue/green. Installed edits and override effects are in the backup; future deploys replace them from source.")
                    print("The backup uses the blue/green manager; its original controller, when different, is archived in .dfa/legacy-deployment.py.")
            finally:
                if not self.journal.exists() and self.stage.exists():
                    shutil.rmtree(self.stage)

    def check_links(self, transaction):
        if link_value(self.active) not in {transaction["previous"], transaction["next"], None} or (self.active.exists() and not self.active.is_symlink()):
            raise Pending("Activation link drift preserved; inspect transaction.json")
        for target, before in transaction["links"].items():
            path = Path(target)
            safe_parents(path, self.home)
            after = str(self.active / transaction["desired"][target]) if target in transaction["desired"] else None
            if (path.exists() and not path.is_symlink()) or (os.readlink(path) if path.is_symlink() else None) not in {before, after}:
                raise Pending(f"Link drift preserved: {target}; inspect transaction.json")
        if self.previous.exists() and not self.previous.is_symlink():
            raise Pending("Backup pointer drift preserved")
        if link_value(self.previous) not in {transaction["backup_before"], transaction["backup_after"]}:
            raise Pending("Backup pointer drift preserved")
        text = self.claude_snapshot()["text"]
        if (text or "") not in {transaction["claude"]["text"] or "", transaction["claude_new"]}:
            raise Pending("Later Claude instruction edits preserved; inspect transaction.json")

    def activate(self, transaction):
        self.swap(self.active, transaction["next"])
        for target, artifact in transaction["desired"].items():
            self.swap(Path(target), str(self.active / artifact))
        for target in transaction["links"].keys() - transaction["desired"].keys():
            Path(target).unlink(missing_ok=True)
        if transaction["claude_new"] != (transaction["claude"]["text"] or ""):
            self.write_claude(transaction["claude_new"], transaction["claude"]["mode"])
        if transaction["backup_after"]:
            self.swap(self.previous, transaction["backup_after"])
        else:
            self.previous.unlink(missing_ok=True)

    def restore(self, transaction):
        self.check_links(transaction)
        for target, before in transaction["links"].items():
            if before is None:
                Path(target).unlink(missing_ok=True)
            else:
                self.swap(Path(target), before)
        if transaction["previous"]:
            self.swap(self.active, transaction["previous"])
        else:
            self.active.unlink(missing_ok=True)
        if transaction["backup_before"]:
            self.swap(self.previous, transaction["backup_before"])
        else:
            self.previous.unlink(missing_ok=True)
        if transaction["claude"]["text"] is None:
            (self.home / ".claude/CLAUDE.md").unlink(missing_ok=True)
        else:
            self.write_claude(transaction["claude"]["text"], transaction["claude"]["mode"])
        if transaction.get("operation") != "rollback":
            next_folder = Path(transaction["next"])
            if next_folder.exists() and not (self.stage / "new").exists():
                shutil.rmtree(next_folder)
            if (self.stage / "retired").exists():
                (self.stage / "retired").rename(next_folder)
            if transaction["legacy"] and Path(transaction["backup_after"]).exists():
                shutil.rmtree(transaction["backup_after"])
        self.journal.unlink()
        if self.stage.exists():
            shutil.rmtree(self.stage)

    def finish(self, transaction):
        if transaction["legacy"]:
            generations = self.root / "generations"
            for folder in list(generations.iterdir()) if generations.exists() else []:
                if folder.is_symlink() or not folder.is_dir():
                    continue
                metadata = read_json(folder / "manifest.json", {})
                if metadata.get("version") == 1 and (folder / "tree").is_dir():
                    shutil.rmtree(folder)
            if generations.exists() and not any(generations.iterdir()):
                generations.rmdir()
            overrides = self.root / "overrides"
            if overrides.is_dir() and not overrides.is_symlink():
                shutil.rmtree(overrides)
        if self.stage.exists():
            shutil.rmtree(self.stage)
        self.journal.unlink()

    def rollback(self):
        with self.locked():
            tree, old = self.current()
            backup = link_value(self.previous)
            if not tree or backup not in {str(self.root / "blue"), str(self.root / "green")} or backup == str(tree):
                raise Pending("No previous installed copy")
            desired = self.state(Path(backup))
            desired["links"] = {k: v for k, v in desired["links"].items() if k not in self.seeds}
            claude = self.claude_snapshot()
            transaction = {"previous": str(tree), "next": backup, "backup_before": backup,
                           "backup_after": str(tree), "links": self.snapshots(desired["links"], old["links"]),
                           "desired": {k: v["artifact"] for k,v in desired["links"].items()},
                           "claude": claude, "claude_new": self.claude_text(claude["text"] or ""),
                           "phase": "prepared", "legacy": False, "operation": "rollback"}
            write_json(self.journal, transaction)
            try:
                self.activate(transaction)
                transaction["phase"] = "committed"
                write_json(self.journal, transaction)
                self.finish(transaction)
            except BaseException:
                if transaction["phase"] == "prepared":
                    self.restore(transaction)
                raise
            print(f"Rolled back to {Path(backup).name}")

    def recover(self):
        with self.locked(recovery=True):
            if not self.journal.exists():
                print("No interrupted activation")
                return
            transaction = read_json(self.journal)
            if transaction.get("next") not in {str(self.root / "blue"), str(self.root / "green")}:
                raise Pending("Recover legacy activation using its installed v5 manager before migrating")
            if transaction.get("phase") == "committed":
                self.check_links(transaction)
                if link_value(self.active) != transaction["next"] or link_value(self.previous) != transaction["backup_after"]:
                    raise Pending("Committed activation links changed; inspect transaction.json")
                self.finish(transaction)
                print("Finished committed deployment cleanup")
            else:
                self.restore(transaction)
                print("Recovered previous installed copy; retry deployment")

    def detach_models_store(self, primary):
        pi = lexical(os.environ.get("PI_CODING_AGENT_DIR", self.home / ".pi/agent"))
        path = pi / "models-store.json"
        source = primary / "pi/models-store.json"
        if link_value(path) == str(source):
            safe_parents(path, self.home)
            if not source.is_file():
                raise Pending(f"Broken models-store link preserved: {path}; restore its source")
            temporary = path.parent / (".dfa-models-" + uuid.uuid4().hex)
            try:
                copy_file(source.resolve(), temporary)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)

    def rebind(self, path, accept):
        with self.locked():
            _, old = self.current()
            path = Path(path).resolve()
            previous = old["sources"][0] if old["sources"] else None
            if not previous or not path.is_dir() or path.is_relative_to(self.root):
                raise Pending("Rebind requires a recorded source and the actual source directory")
            if previous["commit"]:
                url = clean_url(git(path, "remote", "get-url", "origin").stdout.decode())
                if url != previous["url"] and not accept:
                    raise Pending("Repository URL changed; use --accept-origin-change after verification")
                if git(path, "merge-base", "--is-ancestor", previous["commit"], "HEAD", check=False).returncode:
                    raise Pending("Replacement checkout does not descend from the recorded source commit")
            if not (path / "scripts/sync.sh").is_file():
                raise Pending("Rebind requires the actual checkout")
            self.deploy(path, locked=True)


def guidance_readme(primary):
    return (f"# DFA installed copy\n\nShared source: {primary}\n"
            "Run dfa-deploy source before shared edits. Restore/rebind unavailable sources.\n"
            "Deploy replaces managed installed files. Keep machine-local settings outside this copy.\n"
            "config points to blue or green; the other folder is the previous backup.\n"
            "Use dfa-deploy rollback for the previous copy and recover after interruption.\n")
def main():
    # Root execution must create user-owned copies without recursive chown.
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        user = pwd.getpwnam(os.environ["SUDO_USER"])
        os.initgroups(user.pw_name, user.pw_gid)
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
        os.environ["USER_HOME_DIR"] = user.pw_dir
        os.environ["HOME"] = user.pw_dir
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("deploy", "update", "source"):
        p = sub.add_parser(command)
        p.add_argument("--source", type=Path)
        p.add_argument("--dry-run", action="store_true")
        if command == "deploy":
            p.add_argument("--committed", action="store_true", help="Deploy HEAD instead of local working files")
    p = sub.add_parser("rebind"); p.add_argument("path", type=Path)
    p.add_argument("--accept-origin-change", action="store_true")
    for command in ("rollback", "recover", "status", "help"):
        sub.add_parser(command)
    args = parser.parse_args()
    deployment = Deployment(os.environ.get("USER_HOME_DIR", os.environ["HOME"]),
                            working_tree=args.command == "deploy" and not args.committed)
    try:
        if args.command in {"deploy", "update", "rebind", "rollback", "recover"}:
            # Use the same read-only host gate as shell entrypoints; never a saved distro preference.
            check = subprocess.run(["bash", "-c",
                'set -e; DF_SCRIPT_DIR="$1"; source "$DF_SCRIPT_DIR/fn-lib.sh"; '
                'distro="$(detect_workstation_distro)"; require_workstation_entrypoint "$distro" deployment.py',
                "dfa-deploy", str(Path(__file__).resolve().parent)], capture_output=True, text=True)
            if check.returncode:
                raise Pending(check.stderr.strip() or "Unsupported workstation host")
        if args.command in {"deploy", "update", "source"}:
            source = deployment.source(args.source)
            if args.command == "source":
                print(source)
            elif args.dry_run:
                print(f"Would copy, validate and deploy from {source}; no writes")
            else:
                if args.command == "update":
                    if git(source, "status", "--porcelain").stdout:
                        raise Pending("Source is dirty; commit/stash/review it before update, or deploy explicitly without fetching")
                    git(source, "pull", "--ff-only")
                deployment.deploy(source)
        elif args.command == "rebind":
            deployment.rebind(args.path, args.accept_origin_change)
        elif args.command == "rollback":
            deployment.rollback()
        elif args.command == "recover":
            deployment.recover()
        elif args.command == "status":
            if (deployment.root / "transaction.json").exists():
                raise Pending("Interrupted activation; run dfa-deploy recover before migration/stamping")
            tree, state = deployment.current()
            print(json.dumps({"tree": str(tree) if tree else None, "sources": state["sources"],
                              "previous": link_value(deployment.previous), "layout": "legacy" if state.get("legacy") else "blue-green"}, indent=2))
        else:
            print("dfa-deploy deploy|update|source [--source PATH] [--dry-run]\n"
                  "dfa-deploy deploy [--committed] (default: local working files; no fetch)\n"
                  "dfa-deploy status|rollback|recover\n"
                  "dfa-deploy rebind PATH [--accept-origin-change]\n"
                  "Policy and inventory: ~/.local/share/workstation/config/docs/deployment.md")
    except (Pending, OSError, ValueError, SyntaxError) as error:
        print(f"dfa-deploy: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
